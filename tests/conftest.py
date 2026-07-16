"""
Shared fixtures and factories for the mp3tools test suite.

The factories generate real audio files via ffmpeg — a fake-header or
zero-byte file makes the tag readers hit their except paths and return {},
which would silently pass a broken read. Import the plain helpers directly
(`from conftest import make_mp3`); tests needing ffmpeg must carry
@pytest.mark.ffmpeg (or a module-level pytestmark) so they auto-skip when
the binary is absent.
"""
import hashlib
import re
import shutil
import struct
import subprocess
import sys
import time
import zlib
from pathlib import Path

import pytest

# Flat module layout at the repo root; make it importable without an install.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mutagen.id3 import (
    APIC, ID3, TALB, TCON, TIT2, TPE1, TPE2, TPOS, TRCK, TYER,
)

_FRAMES = {"TIT2": TIT2, "TPE1": TPE1, "TPE2": TPE2,
           "TALB": TALB, "TYER": TYER, "TRCK": TRCK, "TPOS": TPOS, "TCON": TCON}

HAVE_FFMPEG = shutil.which("ffmpeg") is not None
HAVE_LAME = shutil.which("lame") is not None


def _ffmpeg_has_libopus() -> bool:
    if not HAVE_FFMPEG:
        return False
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                             capture_output=True, text=True, timeout=10)
        return " libopus " in out.stdout
    except Exception:
        return False


HAVE_LIBOPUS = _ffmpeg_has_libopus()


def pytest_collection_modifyitems(config, items):
    skip_ffmpeg = pytest.mark.skip(reason="ffmpeg not available")
    skip_lame = pytest.mark.skip(reason="lame not available")
    skip_opus = pytest.mark.skip(reason="ffmpeg libopus not available")
    for item in items:
        if not HAVE_FFMPEG and "ffmpeg" in item.keywords:
            item.add_marker(skip_ffmpeg)
        if not HAVE_LAME and "lame" in item.keywords:
            item.add_marker(skip_lame)
        if not HAVE_LIBOPUS and "opus" in item.keywords:
            item.add_marker(skip_opus)


# ── Audio factories ───────────────────────────────────────────────────────────

def _ffmpeg_silence(path, duration, extra=()):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
         "-t", str(duration), *extra, str(path)],
        check=True,
    )


def make_mp3(path, duration=1.0, **frames):
    """Create a real (ffmpeg) MP3 at *path* tagged with *frames* (defaults given)."""
    if not frames:
        frames = {"TIT2": "Silent Night", "TPE1": "Test Artist", "TPE2": "Test Artist",
                  "TALB": "Test Album", "TYER": "2024", "TRCK": "01/1"}
    _ffmpeg_silence(path, duration, ["-q:a", "9"])
    tags = ID3()
    for fid, val in frames.items():
        tags.add(_FRAMES[fid](encoding=3, text=val))
    tags.save(path, v2_version=3, v1=0)


def make_flac(path, duration=1.0, **frames):
    """Create a real (ffmpeg) FLAC at *path* with Vorbis-comment *frames*."""
    _ffmpeg_silence(path, duration)
    if frames:
        from mutagen.flac import FLAC
        f = FLAC(path)
        for k, v in frames.items():
            f[k] = v
        f.save()


def make_opus(path, duration=1.0, **fields):
    """Create a real (ffmpeg libopus) .opus file with Vorbis-comment *fields*.

    Field keys are Vorbis names (title, artist, albumartist, album, date, genre,
    tracknumber, tracktotal, discnumber, ...). Needs @pytest.mark.opus so it
    auto-skips when ffmpeg lacks libopus."""
    _ffmpeg_silence(path, duration, ["-c:a", "libopus"])
    if fields:
        from mutagen.oggopus import OggOpus
        f = OggOpus(str(path))
        for k, v in fields.items():
            f[k] = [str(v)]
        f.save()


def make_m4a(path, duration=1.0, codec="alac", **tags_):
    """Create an M4A at *path* (codec "alac" = lossless, "aac" = lossy).

    Tag keys use FLAC-style names (title, artist, albumartist, album, date,
    genre, tracknumber) and are mapped to MP4 atoms.
    """
    _ffmpeg_silence(path, duration, ["-c:a", codec])
    if tags_:
        from mutagen.mp4 import MP4
        m = MP4(path)
        keymap = {"title": "\xa9nam", "artist": "\xa9ART", "album": "\xa9alb",
                  "albumartist": "aART", "date": "\xa9day", "genre": "\xa9gen"}
        for k, v in tags_.items():
            if k == "tracknumber":
                num, _, total = str(v).partition("/")
                m["trkn"] = [(int(num), int(total) if total else 0)]
            else:
                m[keymap.get(k, k)] = [v]
        m.save()


# ── Tag/file mutators for planting defects ────────────────────────────────────

def _png_1px():
    def chunk(typ, data):
        body = struct.pack(">I", len(data)) + typ + data
        return body + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00\xff\x00\x00")
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", idat) + chunk(b"IEND", b""))


TINY_PNG = _png_1px()   # a valid 1×1 RGB PNG — PIL can open it


def add_id3v1(path):
    """Re-save with an ID3v1 tag appended (a defect audit must flag)."""
    tags = ID3(path, translate=False)
    tags.save(path, v2_version=3, v1=2)


def make_v24(path):
    """Rewrite the file's tags as ID3v2.4 (TYER becomes TDRC — a defect)."""
    tags = ID3(path)            # translate=True → v2.4 frames
    tags.update_to_v24()
    tags.save(path, v2_version=4, v1=0)


def embed_art(path, data=TINY_PNG, mime="image/png"):
    """Embed an APIC front-cover frame."""
    tags = ID3(path, translate=False)
    tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
    tags.save(path, v2_version=3, v1=0)


# ── Whole-tree snapshot for dry-run / idempotency assertions ─────────────────

def snapshot(root, include_mtime=True):
    """{relpath: (size, mtime_ns, sha1)} for every file under *root*.

    Equality of two snapshots proves the tree is byte-identical (and, with
    include_mtime, that no file was even rewritten in place).
    """
    out = {}
    root = Path(root)
    for p in sorted(root.rglob("*")):
        if p.is_file():
            st = p.stat()
            out[str(p.relative_to(root))] = (
                st.st_size,
                st.st_mtime_ns if include_mtime else None,
                hashlib.sha1(p.read_bytes()).hexdigest(),
            )
    return out


# ── Declarative library builder ───────────────────────────────────────────────

_ALBUM_DIR_RE = re.compile(r"^(\d{4}) - (.+)$")


@pytest.fixture
def library_factory(tmp_path):
    """Build a library in tmp_path from a declarative spec and return its root.

        lib = library_factory({
            "Test Artist/2024 - Test Album": [
                {"TIT2": "Song One"},
                {"TIT2": "Song Two", "TRCK": "2"},        # frame overrides
                {"_file": "cover.jpg", "_bytes": TINY_PNG},  # extra file
            ],
        })

    Track frames default from the folder path (TPE1/TPE2 from the artist part,
    TYER/TALB from a "YYYY - Title" album part) and position (TIT2 "Track N",
    TRCK "NN/T"). "_filename" overrides the derived "NN. Artist - Title.mp3".
    """
    def build(spec):
        for folder, items in spec.items():
            album_dir = tmp_path / folder
            album_dir.mkdir(parents=True, exist_ok=True)
            parts = Path(folder).parts
            artist = parts[0]
            year = album = None
            m = _ALBUM_DIR_RE.match(parts[-1]) if len(parts) > 1 else None
            if m:
                year, album = m.group(1), m.group(2)
            tracks = [it for it in items if "_file" not in it]
            total = len(tracks)
            n = 0
            for it in items:
                it = dict(it)
                if "_file" in it:
                    (album_dir / it["_file"]).write_bytes(it.pop("_bytes", b"x"))
                    continue
                n += 1
                filename = it.pop("_filename", None)
                frames = {"TPE1": artist, "TPE2": artist,
                          "TIT2": f"Track {n}", "TRCK": f"{n:02d}/{total}"}
                if year:
                    frames["TYER"] = year
                if album:
                    frames["TALB"] = album
                for key, val in it.items():
                    if val is None:
                        frames.pop(key, None)   # explicit None removes a default
                    else:
                        frames[key] = val
                if filename is None:
                    num = (frames.get("TRCK") or f"{n:02d}").split("/")[0]
                    filename = f"{num}. {frames.get('TPE1', artist)} - {frames['TIT2']}.mp3"
                make_mp3(album_dir / filename, **frames)
        return tmp_path
    return build


# ── Shared parametrize tables for the char/parse helpers ─────────────────────
# audit.py, standardize.py, import_tracks.py, and browse.py all alias the
# single chars.py implementation; the tables run against every module's name
# so an alias quietly turning back into a local copy fails loudly.

NORMALIZE_CASES = [
    ("plain ASCII", "plain ASCII"),
    ("", ""),
    ("don’t", "don't"),
    ("‘single‛ ‚low’", "'single' 'low'"),
    ("“double” „low‟", '"double" "low"'),
    ("café Zürich", "café Zürich"),        # accents pass through
    ("em—dash – en", "em—dash – en"),      # dashes are NOT in the table
    ("Cafe\u0301", "Caf\u00e9"),   # decomposed input composes to NFC
]

# Substitution-only cases (ASCII input) — valid for all four sanitize copies,
# including audit.sanitize which skips the CHAR_REPLACEMENTS pass.
SANITIZE_CASES = [
    ("AC/DC", "AC-DC"),
    ("back\\slash", "back-slash"),
    ("Album: Live", "Album - Live"),
    ("what?*<>", "what"),
    ('say "hi"', "say 'hi'"),
    ("pipe|pipe", "pipe-pipe"),
    ("end. ", "end"),
    ("end . .", "end"),
    ("  leading kept", "  leading kept"),  # only trailing ". " stripped
    ("", ""),
]

YEAR_CASES = [
    ("2024", "2024"),
    ("2024-05-01", "2024"),
    ("05/2024", "2024"),
    ("released 1999!", "1999"),
    ("1994-1996", "1994"),                 # first match wins
    ("2099", "2099"),
    ("1899", None),                        # only 19xx/20xx
    ("12024", None),                       # word boundary required
    ("garbage", None),
    ("", None),
]

TRACK_CASES = [
    ("5", (5, None)),
    ("05/12", (5, 12)),
    (" 5/12 ", (5, 12)),
    ("05/", (5, None)),
    ("/12", (None, 12)),
    ("", (None, None)),
    (" ", (None, None)),
    ("abc", (None, None)),
    ("5/x", (None, None)),
    ("5.5", (None, None)),
]


# ── Job polling (web tests) ───────────────────────────────────────────────────

def poll(client, jid, answer=None, max_iter=400):
    """Drive a job to a terminal state, answering prompts via answer(prompt)->value."""
    for _ in range(max_iter):
        j = client.get(f"/api/jobs/{jid}").json()
        if j["state"] == "waiting":
            val = answer(j["prompt"]) if answer else ""
            client.post(f"/api/jobs/{jid}/respond", json={"value": val})
        elif j["state"] in ("done", "error"):
            return j
        time.sleep(0.03)
    return client.get(f"/api/jobs/{jid}").json()
