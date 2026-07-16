"""
Canonical Xing/Info + LAME gapless-header parsing, shared by convert_lossless
(encode-time verification) and album_stream (gapless trim math).

These two modules previously carried duplicate copies of this parse with
*different* dummy-value tables, so they could disagree about whether the same
file had a usable gapless header. The parse and the union of both tables now
live here once.
"""

from pathlib import Path

# ffmpeg's libmp3lame muxer writes a LAME tag but fills the encoder
# delay/padding fields with dummy bit-patterns instead of the real values, so
# players cannot trim them and gapless playback breaks. The standalone `lame`
# encoder writes the true values (delay 576 + computed padding). This is the
# union of the patterns each module had observed independently.
DUMMY_DELAY_PADDING = {
    (0xAAA, 0xAAA), (0x555, 0x555),          # convert_lossless's table
    (0x756, 0x554), (0x756, 0x555),          # album_stream's table
}


def lame_delay_padding(path: Path) -> tuple[int, int] | None:
    """Encoder (delay, padding) in samples from the Xing/Info+LAME header, or
    None when the header is absent or carries only dummy fill values."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(10)
            skip = 0
            if head[:3] == b"ID3":
                skip = 10 + ((head[6] << 21) | (head[7] << 14)
                             | (head[8] << 7) | head[9])
            fh.seek(skip)
            buf = fh.read(2048)  # the Xing/LAME info frame is the first audio frame
        if not (b"Xing" in buf or b"Info" in buf):
            return None
        j = buf.find(b"LAME")            # 9-byte encoder-version string
        if j < 0 or j + 24 > len(buf):
            return None
        # delay (12 bits) + padding (12 bits) live 21 bytes into the LAME tag
        v = int.from_bytes(buf[j + 21:j + 24], "big")
        delay, padding = v >> 12, v & 0xFFF
        if (delay, padding) in DUMMY_DELAY_PADDING:
            return None
        return delay, padding
    except Exception:
        return None


def has_lame_header(path: Path) -> bool:
    """True if the MP3 carries a Xing/Info + LAME gapless header with *real*
    encoder delay/padding. Dummy ffmpeg fill is treated as missing, since
    players can't use it. Absence means gapless playback breaks."""
    dp = lame_delay_padding(path)
    return dp is not None and dp[0] > 0
