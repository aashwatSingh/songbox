# M2: Hardened ingest — design

Status: approved. Date: 2026-08-20.

## Context

M1 (rights gate) is done and merged. Its `POST /tracks/upload` endpoint is explicitly, deliberately
unhardened — no magic-byte validation, no size/duration limits, no sandboxing beyond argument-array
ffmpeg calls — per M1's own scope boundary ("M2's job, applied to this exact endpoint"). M2 closes
that gap: `docs/PLAN.md`'s own "done when" is a malformed-file test suite (truncated headers, wrong
magic bytes, a playlist referencing a remote URL, a duration bomb) fully rejected.

Two scope decisions were made with the user before designing the rest:

1. **Sandboxing is process-level, not container-level, for M2.** No containerized worker pool exists
   yet (M0/M1 only built the API service — the real worker infra with network-egress-denial is M3's
   job). Building empty container infrastructure now, before M3's actual workload (Demucs) is known,
   would be premature. M2 hardens the ffprobe/ffmpeg invocations already in `services/api` directly:
   argument arrays (already true), protocol whitelist (already true for the fingerprint step; a real
   gap existed on `ffprobe` until M1's final review fixed it — this spec keeps it that way, doesn't
   reopen it), and adds the piece that was actually missing: **timeouts** on both subprocess calls.
2. **The upload flow stays a single multipart request, matching M1's shape.** The original external
   build prompt calls for presigned direct-to-storage upload as part of M2. The user chose to keep
   `POST /tracks/upload`'s existing single-request shape instead of restructuring into a
   presign/finalize two-phase flow. **This means M2 deliberately does NOT implement presigned upload**
   — that's a real, acknowledged deviation from the original spec, not an oversight, and is recorded
   here and in `docs/STATUS.md` so it doesn't get silently assumed done later.

## What M2 actually builds

All of it lands inside the existing `upload_track` handler in `services/api/app/routes/tracks.py`,
before the fingerprint/gate logic M1 already built (which is otherwise unchanged):

### 1. Storage key no longer includes the client filename

M1's `save_track_file(client, tenant_id, filename, data)` builds the key as
`f"{tenant_id}/{uuid.uuid4()}-{filename}"` — the filename suffix is raw, unsanitized client input
(flagged as a real, if low-risk, gap in M1's final review and explicitly deferred to "whenever M2's
hardened-ingest work lands"). Fixed here: the key becomes bare `f"{tenant_id}/{uuid.uuid4()}"`, no
filename component at all. `save_track_file`'s signature drops the `filename` parameter.

### 2. Magic-byte validation

Runs on the raw uploaded bytes, before anything touches a subprocess. Six accepted formats, checked
by binary signature — never by client-supplied extension or `Content-Type`:

| Format | Signature |
|---|---|
| WAV | bytes 0-3 `RIFF`, bytes 8-11 `WAVE` |
| FLAC | bytes 0-3 `fLaC` |
| MP3 | bytes 0-2 `ID3` (ID3v2-tagged), OR bytes 0-1 form an MPEG frame sync (`0xFF` followed by a byte with its top 3 bits set — `0xE0`-`0xFF` masked) |
| M4A | bytes 4-7 `ftyp` (ISO base media file format box) |
| OGG | bytes 0-3 `OggS` |
| AIFF | bytes 0-3 `FORM`, bytes 8-11 `AIFF` or `AIFC` |

A file matching none of these → `422` immediately. This is what rejects "wrong magic bytes" and
"truncated headers" (a truncated file is either too short to contain the signature bytes at all, or
its signature bytes don't match — both fail the same check) without ever invoking ffmpeg/ffprobe on
attacker-controlled bytes.

### 3. ffprobe gating

`fingerprint.py`'s existing `fingerprint_audio` already runs `ffprobe` to get duration. M2 adds
enforcement on top of the value it returns, plus a probe timeout:
- Reject if duration > 12 minutes (720 seconds).
- Reject if stream count > 2 (requires adding `-show_entries stream=index` or counting stream lines
  in ffprobe's output — currently only `format=duration` is queried).
- Reject if the ffprobe subprocess doesn't return within a timeout (30 seconds is generous for a
  12-minute-cap file; anything longer is itself suspicious).

This is what rejects "duration bomb": a file whose container metadata claims a short, acceptable
duration but that would actually take unbounded time/resources to process is caught by the timeout;
a file that honestly declares an excessive duration is caught by the 12-minute check before any
further processing happens.

### 4. Timeout on the fingerprint (chromaprint) subprocess too

M1's `fingerprint_audio` has no `timeout=` on either `subprocess.run` call — flagged as a real Minor
gap in M1's final review ("a file that makes ffprobe spin hangs a synchronous request thread
indefinitely"). Both the ffprobe and ffmpeg calls in `fingerprint.py` get a timeout (30s each),
raising `FingerprintError` on `subprocess.TimeoutExpired` the same way they already do on a nonzero
return code.

### 5. "Playlist with remote URL" — already closed, verified not reopened

`-protocol_whitelist file` is already present on both the `ffmpeg` and `ffprobe` calls in
`fingerprint.py` (the `ffprobe` one was added during M1's final review). This flag is the actual
load-bearing defense against a crafted playlist reaching the network: without it, ffmpeg's nested
demuxers default to `file,crypto,data` on the protocol whitelist, so `-protocol_whitelist file`
genuinely narrows what a probed file can make ffmpeg/ffprobe touch (verified directly). The concat
demuxer's own `safe=1` default additionally rejects non-local paths even within the `file` protocol.
Together these two are what close the SSRF class — not the magic-byte check.

The magic-byte check is a **hygiene filter, not a security boundary**: it cheaply rejects
obviously-wrong input before any subprocess is spawned, but it does not defend against a crafted
playlist reaching the network. The MP3 branch, for example, accepts any file starting with `0xFF`
followed by a byte with its top 3 bits set — trivial for an attacker to prepend to an
otherwise-arbitrary payload (a live-built ID3/frame-sync-prefixed `ffconcat` playlist passes
`detect_audio_format` as `"mp3"` and still causes ffmpeg to select the concat demuxer). It is not,
and should not be described as, an independent layer defending against this class of attack.

M2's test suite includes a real crafted playlist file (`test_upload_rejects_playlist_with_remote_url`
in `test_tracks_upload.py`), not just a code read — that payload is plain M3U8 text, so it's rejected
at the magic-byte stage without ever reaching ffmpeg.

Live probing during the final whole-branch review measured which layer actually stops what, against a
real local listener, across all four combinations of the two defenses. Recording it precisely, because
the earlier version of this section guessed and guessed wrong:

- A **pure `ffconcat` playlist** referencing `http://` is where the two-layer claim genuinely holds.
  With only `safe=1`: blocked (`Unsafe file name 'http://...'`). With only `-protocol_whitelist file`:
  blocked (`Impossible to open 'http://...'`). With **both** disabled: the listener received a real
  `GET /payload.wav`. So each layer blocks independently and egress requires defeating both. This
  payload never passes `detect_audio_format` anyway.
- An **ID3-prefixed `ffconcat` playlist** — which *does* pass `detect_audio_format` as `"mp3"` and
  does cause ffmpeg to select the concat demuxer — is rejected by neither of those layers. It dies in
  the concat *line parser* (`Line 1: unknown keyword 'ID3?'`) in all four configurations, including
  with both defenses fully disabled. The prefix that gets it past the magic-byte filter is the same
  prefix the parser chokes on: concat's probe skips the ID3 tag, but its parser re-reads from offset 0.

That second bullet is an implementation accident of this ffmpeg version, not a designed defense, and
no payload was found that both satisfies `detect_audio_format` and gets concat to parse a file
directive. Do not treat it as protection — it is exactly why the magic-byte check stays classified as
a hygiene filter above, and why `-protocol_whitelist file` must remain on both invocations.

## Data model / API surface changes

None. No new tables, no new endpoints. `POST /tracks/upload`'s request/response shape is unchanged;
it just rejects more inputs, earlier, with clearer 422 reasons.

## Testing strategy (test-first, per the working agreement)

1. `services/api/app/validation.py` (new) — magic-byte detection as a pure function
   (`detect_audio_format(data: bytes) -> str | None`), unit-tested against real minimal file headers
   for all six formats (generated via ffmpeg at test time, same pattern as M1's `synthetic_wav`
   fixture) plus deliberately malformed ones (truncated to under the signature length, garbage bytes,
   a real M3U8 playlist file's bytes).
2. `fingerprint.py` changes (timeouts, stream-count check) — unit-tested directly against
   `fingerprint_audio`, reusing M1's `synthetic_wav` fixture for the happy path. The timeout path is
   tested by monkeypatching `subprocess.run` to raise `subprocess.TimeoutExpired` directly, not by
   constructing a genuinely slow ffmpeg invocation — deterministic and doesn't slow down the suite.
3. `upload_track` wired end-to-end — reusing M1's `test_tracks_upload.py` patterns
   (`FixtureAcoustIDClient`, real Postgres/MinIO/ffmpeg), with the exact four malformed-file cases
   the plan's "done when" names, each asserting the correct rejection stage and status code:
   - Truncated header (e.g. a WAV file cut off after 4 bytes) → 422 from the magic-byte check.
   - Wrong magic bytes (e.g. a plain text file with a `.wav` extension) → 422 from the magic-byte
     check.
   - Playlist with a remote URL (a real M3U8 file referencing `http://` content) → 422 from the
     magic-byte check (never reaches ffmpeg at all).
   - Duration bomb (a file whose declared duration exceeds 12 minutes — synthesized via ffmpeg's
     `lavfi`/`sine` source at a duration just over the cap, which is fast to generate despite the
     long declared duration) → 422 from the ffprobe gating step.

## Out of scope for M2

Presigned direct-to-storage upload (deliberately deferred, see Context above — a real gap vs. the
original spec, recorded rather than silently assumed done). Container-level worker sandboxing
(no network egress, seccomp, read-only root — M3's job, once real GPU workers exist to sandbox).
Decompression-bomb protection beyond the duration/timeout checks above (the original spec also
mentions checking "declared-versus-actual duration and sample count before allocating buffers" —
M2's ffprobe-before-decode ordering already prevents allocating buffers for an oversized file, so
no separate sample-count check is added). Rate limiting / per-tenant queue caps (M7). Loudness
normalization to -14 LUFS and 44.1kHz stereo internal-format normalization (both explicitly listed
in the original spec's §4.1 but not in `docs/PLAN.md`'s M2 scope or its "done when" — deferred to
whichever later milestone actually needs a normalized internal format, since nothing consumes it yet).
