# Songbox — project invariants

These rules override default behavior. They exist because this pipeline accepts arbitrary audio from
the internet, touches heavily licensed content, and runs ML inference that costs real money per second.

## The rights gate

- There are exactly **three ingress lanes**: A (creator-owned), B (licensed catalog / B2B), C (public
  domain / Creative Commons). **There is no fourth lane.**
- **Never add `yt-dlp`, `youtube-dl`, `pytube`, or any equivalent to this project.** Never implement a
  "paste a link" field or any remote-media-fetch-from-a-third-party-platform path. If you find yourself
  writing code that downloads media from a URL the user doesn't control, stop.
- **Nothing reaches a GPU without a rights-gate PASS.** The gate (attestation + Chromaprint/AcoustID
  fingerprint check + license/PD-CC resolution) runs before any job is enqueued to the worker pool.
- **One documented exception: `SONGBOX_PERSONAL_MODE=1`** makes the gate record its findings and
  pass anyway. It exists because a hold on a single-user install is a dead end — there is no second
  human to escalate to, so every track stops forever. The fingerprint lookup still runs and its real
  result is still written to `fingerprint_matches`; only enforcement changes, and the stored reason
  says so explicitly. **Default OFF, and it must stay off for any deployment serving more than one
  person.** Do not widen this into a general bypass, and do not add a second one: if a new situation
  seems to need the gate turned off, that is a design discussion, not a flag.
- Lyric display rights are tracked **separately** from recording rights (`covers_recording` vs
  `covers_lyrics` booleans on `licenses`). Missing lyric clearance is a supported degraded state (no
  lyric text rendered), not an error.

## Pipeline order and format

- **Source separation always precedes transcription.** Whisper runs on the isolated vocal stem, never
  the full mix — this order is a measured accuracy win, not a preference.
- All internal audio is **44.1kHz stereo WAV**. Assert this at every stage boundary.
- `karaoke.json` is a versioned schema. Any shape change needs a migration path, not a silent bump.

## ffmpeg / sandboxing

- Invoke ffmpeg with an **explicit argument array** — never `shell=True`, never string-interpolate
  user-controlled data into a command line.
- Protocol whitelist is **`file` only** (`-protocol_whitelist file`). This blocks the SSRF class of bug
  where a crafted playlist makes ffmpeg issue outbound HTTP requests.
- Cloud GPU workers (Modal/RunPod backend) run with no network egress except to object storage and the
  queue. The **local dev GPU backend does not have this guarantee** — it's a plain subprocess with
  resource limits, not a network-isolated sandbox. Don't treat a clean local run as proof the sandbox
  constraint holds; that's only validated against the real cloud backend in M7.

## Data

- Every table must carry `tenant_id`, and every query must filter on it. Add the enforcing test the
  moment the first table lands — don't defer it.
- Never log raw audio, lyrics, or signed URLs.

## Measurement discipline

- **No fabricated accuracy, latency, or cost figure.** If it hasn't been measured, write
  `TODO: unmeasured` — never a plausible-looking placeholder number.
