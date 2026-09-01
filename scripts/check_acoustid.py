"""Verify that ACOUSTID_API_KEY is present and actually works against the real AcoustID service.

Run this after putting a key in services/api/.env -- it isolates "is the key good?" from every
other reason an upload might be held, which otherwise can only be tested by uploading a track
through the UI and guessing at the cause.

    python scripts/check_acoustid.py

Exits 0 only if a real lookup round-trips. Never prints the key itself.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "services" / "api"))


def load_env_file() -> None:
    """Mirror what scripts/start-api.ps1 does, so this script works standalone."""
    env_file = REPO_ROOT / "services" / "api" / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8-sig").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        os.environ.setdefault(name.strip(), value.strip().strip('"').strip("'"))


def main() -> int:
    load_env_file()

    key = os.environ.get("ACOUSTID_API_KEY")
    if not key:
        print("FAIL: ACOUSTID_API_KEY is not set.")
        print("  Put it in services/api/.env as ACOUSTID_API_KEY=...")
        print("  Get one free at https://acoustid.org/new-application")
        return 1
    # Length only -- never echo the secret itself.
    print(f"key present ({len(key)} chars)")

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        print("FAIL: ffmpeg is not on PATH, so no fingerprint can be generated.")
        return 1

    # A high-entropy signal, not a pure tone: a sine produces a near-all-zero fingerprint that
    # exercises almost none of the encoding and tells you nothing about real behavior.
    with tempfile.TemporaryDirectory() as tmpdir:
        wav = pathlib.Path(tmpdir) / "probe.wav"
        subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi",
             "-i", "anoisesrc=d=30:c=pink:a=0.5", "-ar", "44100", "-ac", "2", str(wav)],
            check=True, capture_output=True,
        )

        from app.acoustid.client import HTTPAcoustIDClient  # noqa: PLC0415
        from app.fingerprint import fingerprint_audio  # noqa: PLC0415

        fp = fingerprint_audio(wav)
        print(f"fingerprint generated ({len(fp.value)} chars, {fp.duration_seconds:.1f}s)")

        result = HTTPAcoustIDClient().lookup(fp.value, fp.duration_seconds)

    if result.error:
        print(f"FAIL: AcoustID lookup returned an error: {result.error}")
        print("  An invalid key usually surfaces here as status=error / 'invalid API key'.")
        return 1

    # Zero matches is the CORRECT answer for random noise -- it proves the round-trip worked
    # (the service accepted the key and parsed the fingerprint) without needing real music.
    print(f"OK: lookup succeeded, {len(result.matches)} match(es) for random noise (0 expected).")
    print("The rights gate's fingerprint check is live; uploads can now reach 'passed'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
