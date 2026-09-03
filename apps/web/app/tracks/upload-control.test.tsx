import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import TracksPage from "./page";

// The page pulls in the real API client and auth context; both talk to a backend that does not
// exist under jsdom. Stub them at the module boundary so these tests stay about the upload
// control's behavior and nothing else.
vi.mock("@/lib/api", () => ({
  listTracks: vi.fn(async () => []),
  uploadTrack: vi.fn(async () => ({ track_id: "t1", status: "pending_review", reason: "held" })),
  deleteTrack: vi.fn(async () => undefined),
  toggleBookmark: vi.fn(async () => undefined),
  logout: vi.fn(async () => undefined),
}));

// The value must be referentially STABLE across renders. The page has a
// `useEffect(..., [user])` that reloads the track list, so returning a fresh object literal per
// call gives `user` a new identity every render, re-fires that effect forever, and the render
// loop exhausts the heap. The real AuthContext holds `user` in state, so it is stable there.
vi.mock("@/lib/AuthContext", () => {
  const value = { user: { email: "demo@example.com" }, loading: false, refresh: vi.fn() };
  return { useAuth: () => value };
});

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn(), replace: vi.fn() }) }));

// Render, then wait for the on-mount listTracks() promise to settle. Without this the initial
// load resolves after the test body has moved on, and React reports a state update outside act().
async function renderPage(): Promise<void> {
  // act() around the render flushes the on-mount effects AND the microtasks their promises
  // queue, so the list-load state update lands inside act rather than after the test body has
  // moved on (which React reports as an "update not wrapped in act(...)" warning).
  await act(async () => {
    render(<TracksPage />);
  });
  await waitFor(() => expect(screen.queryByText(/loading tracks/i)).toBeNull());
}

function fileInput(): HTMLInputElement {
  const input = document.querySelector<HTMLInputElement>('input[type="file"]');
  if (input === null) {
    throw new Error("no file input is mounted");
  }
  return input;
}

let clickSpy: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  clickSpy = vi.spyOn(HTMLInputElement.prototype, "click").mockImplementation(() => {});
});

afterEach(() => {
  clickSpy.mockRestore();
  vi.clearAllMocks();
});

describe("the upload control", () => {
  test("mounts the file input before the form is ever opened", async () => {
    await renderPage();

    // The regression this guards: when the input lived inside the collapsible form, it did not
    // exist yet at the moment "Upload track" was clicked. A browser only honors input.click()
    // inside a real user gesture, so mounting it as a side effect of opening the form and
    // clicking it afterwards loses the gesture and the picker silently never opens.
    expect(fileInput()).toBeTruthy();
    expect(document.querySelector("form")).toBeNull();
  });

  test("clicking 'Upload track' opens the OS file picker", async () => {
    await renderPage();
    const button = screen.getByRole("button", { name: /upload track/i });

    fireEvent.click(button);

    expect(clickSpy).toHaveBeenCalledTimes(1);
    expect(document.querySelector("form")).not.toBeNull();
  });

  test("keeps opening the picker on every click, not just the first", async () => {
    await renderPage();
    const button = screen.getByRole("button", { name: /upload track/i });

    // "Upload track" used to be a toggle, so a second click closed the form instead of
    // re-opening the picker -- which read as the button doing nothing every other press.
    fireEvent.click(button);
    fireEvent.click(button);
    fireEvent.click(button);

    expect(clickSpy).toHaveBeenCalledTimes(3);
    expect(document.querySelector("form")).not.toBeNull();
  });

  test("shows the chosen filename and prefills the title from it", async () => {
    await renderPage();
    fireEvent.click(screen.getByRole("button", { name: /upload track/i }));

    const file = new File(["audio"], "Midnight Avenue.wav", { type: "audio/wav" });
    fireEvent.change(fileInput(), { target: { files: [file] } });

    await waitFor(() => expect(screen.getByText("Midnight Avenue.wav")).toBeTruthy());
    const title = screen.getByPlaceholderText("Track title") as HTMLInputElement;
    expect(title.value).toBe("Midnight Avenue");
  });

  test("does not overwrite a title the user already typed", async () => {
    await renderPage();
    fireEvent.click(screen.getByRole("button", { name: /upload track/i }));

    const title = screen.getByPlaceholderText("Track title") as HTMLInputElement;
    fireEvent.change(title, { target: { value: "My Own Title" } });
    fireEvent.change(fileInput(), {
      target: { files: [new File(["audio"], "something-else.wav", { type: "audio/wav" })] },
    });

    await waitFor(() => expect(screen.getByText("something-else.wav")).toBeTruthy());
    expect(title.value).toBe("My Own Title");
  });

  test("accepts a file dropped onto the control", async () => {
    await renderPage();
    fireEvent.click(screen.getByRole("button", { name: /upload track/i }));

    const zone = screen.getByText(/choose files/i).closest("button");
    expect(zone).not.toBeNull();
    fireEvent.drop(zone as HTMLElement, {
      dataTransfer: { files: [new File(["audio"], "Dropped.wav", { type: "audio/wav" })] },
    });

    await waitFor(() => expect(screen.getByText("Dropped.wav")).toBeTruthy());
  });

  test("submit stays disabled until a file is chosen", async () => {
    await renderPage();
    fireEvent.click(screen.getByRole("button", { name: /upload track/i }));

    const submit = screen.getByRole("button", { name: /^upload$/i }) as HTMLButtonElement;
    expect(submit.disabled).toBe(true);

    fireEvent.change(fileInput(), {
      target: { files: [new File(["audio"], "Ready.wav", { type: "audio/wav" })] },
    });

    await waitFor(() => expect(submit.disabled).toBe(false));
  });
});

describe("multi-file upload", () => {
  test("accepts several files and reports the count", async () => {
    await renderPage();
    fireEvent.click(screen.getByRole("button", { name: /upload track/i }));

    fireEvent.change(fileInput(), {
      target: {
        files: [
          new File(["a"], "One.wav", { type: "audio/wav" }),
          new File(["b"], "Two.wav", { type: "audio/wav" }),
          new File(["c"], "Three.wav", { type: "audio/wav" }),
        ],
      },
    });

    await waitFor(() => expect(screen.getByText("3 files selected")).toBeTruthy());
    // The submit button names the batch, so it is obvious more than one song is going up.
    expect(screen.getByRole("button", { name: /upload 3 songs/i })).toBeTruthy();
  });

  test("a batch titles each song from its own filename instead of the shared Title box", async () => {
    await renderPage();
    fireEvent.click(screen.getByRole("button", { name: /upload track/i }));

    fireEvent.change(fileInput(), {
      target: {
        files: [
          new File(["a"], "One.wav", { type: "audio/wav" }),
          new File(["b"], "Two.wav", { type: "audio/wav" }),
        ],
      },
    });

    // A single Title field cannot name two songs, so it is replaced by an explanation.
    await waitFor(() =>
      expect(screen.getByText(/each is titled from its filename/i)).toBeTruthy(),
    );
    expect(screen.queryByPlaceholderText("Track title")).toBeNull();
  });

  test("a single file still uses the Title box", async () => {
    await renderPage();
    fireEvent.click(screen.getByRole("button", { name: /upload track/i }));

    fireEvent.change(fileInput(), {
      target: { files: [new File(["a"], "Only One.wav", { type: "audio/wav" })] },
    });

    await waitFor(() => expect(screen.getByText("Only One.wav")).toBeTruthy());
    const title = screen.getByPlaceholderText("Track title") as HTMLInputElement;
    expect(title.value).toBe("Only One");
  });

  test("accepts multiple files dropped at once", async () => {
    await renderPage();
    fireEvent.click(screen.getByRole("button", { name: /upload track/i }));

    const zone = screen.getByText(/choose files/i).closest("button");
    fireEvent.drop(zone as HTMLElement, {
      dataTransfer: {
        files: [
          new File(["a"], "D1.wav", { type: "audio/wav" }),
          new File(["b"], "D2.wav", { type: "audio/wav" }),
        ],
      },
    });

    await waitFor(() => expect(screen.getByText("2 files selected")).toBeTruthy());
  });
});
