"""
mp3header.py is the single Xing/Info+LAME parser. convert_lossless and
album_stream previously carried duplicate copies with different dummy-value
tables; these identity assertions fail loudly if a local copy ever reappears
(mirror of tests/test_chars.py for the char helpers).
"""
import pytest

import album_stream
import convert_lossless
import mp3header

from conftest import make_flac


def test_single_parser_identity():
    assert convert_lossless.has_lame_header is mp3header.has_lame_header
    assert album_stream._lame_delay_padding is mp3header.lame_delay_padding


def test_dummy_table_is_union_of_both_modules():
    # convert_lossless's observed ffmpeg fill + album_stream's observed fill.
    assert {(0xAAA, 0xAAA), (0x555, 0x555),
            (0x756, 0x554), (0x756, 0x555)} <= mp3header.DUMMY_DELAY_PADDING


def test_garbage_and_missing_files(tmp_path):
    p = tmp_path / "x.mp3"
    p.write_bytes(b"garbage" * 400)
    assert mp3header.lame_delay_padding(p) is None
    assert mp3header.has_lame_header(p) is False
    assert mp3header.lame_delay_padding(tmp_path / "absent.mp3") is None


@pytest.mark.ffmpeg
@pytest.mark.lame
def test_real_lame_encode_has_real_delay_padding(tmp_path):
    from convert_lossless import _lame_pipe_convert
    src = tmp_path / "t.flac"
    make_flac(src)
    dst = tmp_path / "t.mp3"
    assert _lame_pipe_convert(src, dst, 192)
    dp = mp3header.lame_delay_padding(dst)
    assert dp is not None
    delay, padding = dp
    assert delay >= 576          # lame's true encoder delay
    assert padding >= 0
    assert mp3header.has_lame_header(dst) is True
