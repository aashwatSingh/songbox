from __future__ import annotations


# NOTE: this is a hygiene filter, NOT a security boundary. An attacker who controls the file
# can trivially satisfy any of these signatures by prepending a few bytes (e.g. "\xFF\xFB" for
# the MP3 frame-sync branch). Its job is to reject obviously-wrong input cheaply, before any
# subprocess is spawned. The real defense against a crafted playlist reaching the network is
# ffmpeg/ffprobe's `-protocol_whitelist file` (see app/fingerprint.py) plus the concat demuxer's
# safe-mode default -- never this function.
def detect_audio_format(data: bytes) -> str | None:
    """Identify one of the six accepted audio formats by binary signature -- never by filename
    extension or client-supplied Content-Type, both of which are attacker-controlled. Returns
    the format name, or None if the bytes don't match any accepted signature (including if data
    is too short to contain one). Bytes slicing is used throughout instead of direct indexing
    for the multi-byte checks because slicing past the end of a short bytes object returns an
    empty/short result rather than raising -- only the two-byte MPEG frame-sync check needs an
    explicit length guard, since it indexes individual bytes.
    """
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if data[:4] == b"fLaC":
        return "flac"
    if data[:3] == b"ID3":
        return "mp3"
    if len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return "mp3"
    if data[4:8] == b"ftyp":
        return "m4a"
    if data[:4] == b"OggS":
        return "ogg"
    if data[:4] == b"FORM" and data[8:12] in (b"AIFF", b"AIFC"):
        return "aiff"
    return None
