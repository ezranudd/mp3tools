#!/usr/bin/env python3
"""
Import MP3s from a source directory into a music library.

Reads each source MP3's ID3 tags, prompts for any that are missing,
normalizes all tags, then copies each file into LIBRARY under:

  LIBRARY/Album Artist/YEAR - Album/XX. Artist - Title.mp3

Source files are never modified. All required tags are written to
each copy so that running audit.py on the library reports no issues.
"""

import argparse
import re
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from mutagen import File as _AudioFile

import settings as settings_mod
from convert_lossless import (
    LOSSLESS_EXTENSIONS, find_lossless, read_lossless_tags,
    read_cue_tracks,
)
from import_preview import run_preview
from mutagen.mp3 import MP3 as _MP3Info
from mutagen.id3 import (
    ID3, ID3NoHeaderError,
    TPE1, TIT2, TALB, TYER, TCON, TRCK,
    TPE2, APIC,
)


# ── Constants ─────────────────────────────────────────────────────────────────

KEEP_TAGS = {"TPE1", "TPE2", "TIT2", "TALB", "TYER", "TCON", "TRCK"}
# TPE2 is the canonical album artist frame. The TXXX variants are legacy —
# read them for migration but never write them.
ALBUM_ARTIST_KEYS = (
    "TPE2",
    "TXXX:album artist",
    "TXXX:ALBUMARTIST",
    "TXXX:ALBUM ARTIST",
    "TXXX:AlbumArtist",
    "TXXX:Album Artist",
)
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

CHAR_REPLACEMENTS: dict[str, str] = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "`": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "«": '"', "»": '"',
    "–": "-", "—": "-", "−": "-",
    "‐": "-", "‑": "-", "⁃": "-",
    "…": "...",
    " ": " ", " ": " ", " ": " ",
    " ": " ", " ": " ", "​": "",
    "×": "x", "⁄": "/", "∕": "/",
    "№": "No.", "℗": "(P)",
    "℃": "C", "℉": "F",
    "™": "", "®": "", "©": "(C)",
    "•": "-", "·": "-",
    "†": "+", "‡": "++",
    "′": "'", "″": '"', "‴": "'''",
    "⁊": "&",
}

TAG_NAMES = {
    "TPE1": "Artist", "ALBUMARTIST": "Album Artist", "TIT2": "Title", "TALB": "Album",
    "YEAR": "Year",   "TCON": "Genre", "TRCK": "Track",
}
ALBUM_TAGS = ("TPE1", "ALBUMARTIST", "TALB", "YEAR", "TCON")
TRACK_TAGS = ("TIT2",)   # TRCK is computed, not prompted


# ── Helpers ───────────────────────────────────────────────────────────────────

def normalize_string(s: str) -> str:
    for old, new in CHAR_REPLACEMENTS.items():
        s = s.replace(old, new)
    return s


def sanitize_name(name: str) -> str:
    name = normalize_string(name)
    for old, new in {"/": "-", "\\": "-", ":": " -", "*": "",
                     "?": "", '"': "'", "<": "", ">": "", "|": "-"}.items():
        name = name.replace(old, new)
    return name.rstrip(". ")


def extract_year(value: str) -> str | None:
    m = re.search(r"\b(19\d{2}|20\d{2})\b", str(value))
    return m.group(1) if m else None


def parse_track(s: str) -> tuple[int | None, int | None]:
    parts = s.split("/")
    try:
        n = int(parts[0].strip()) if parts[0].strip() else None
        t = int(parts[1].strip()) if len(parts) > 1 and parts[1].strip() else None
        return n, t
    except ValueError:
        return None, None


def get_input(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)


def load_id3(path: Path) -> ID3:
    """Load raw ID3 frames without mutagen's v2.4 translation layer."""
    return ID3(path, translate=False)


def _audio_duration(path: Path) -> float | None:
    try:
        audio = _AudioFile(path)
        if audio and audio.info and audio.info.length:
            return float(audio.info.length)
    except Exception:
        pass
    return None


def _progress_bar(done: float, total: float, width: int = 28) -> str:
    if total <= 0:
        filled = 0
    else:
        filled = min(width, int(width * done / total))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def _conversion_duration(src: Path, start_time: float | None, end_time: float | None) -> float | None:
    total = _audio_duration(src)
    if start_time is not None and end_time is not None:
        return max(0.01, end_time - start_time)
    if start_time is not None and total is not None:
        return max(0.01, total - start_time)
    return total


def convert_to_mp3_progress(src: Path, dst: Path, bitrate: int,
                            start_time: float | None = None,
                            end_time: float | None = None) -> bool:
    """Convert src to MP3 and print an in-place progress bar."""
    duration = _conversion_duration(src, start_time, end_time)
    try:
        cmd = ["ffmpeg", "-hide_banner", "-nostats", "-i", str(src)]
        if start_time is not None:
            cmd += ["-ss", f"{start_time:.6f}"]
        if end_time is not None:
            cmd += ["-to", f"{end_time:.6f}"]
        cmd += [
            "-acodec", "libmp3lame",
            "-b:a", f"{bitrate}k",
            "-map_metadata", "0",
            "-f", "mp3",
            "-progress", "pipe:1",
            "-y", str(dst),
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        last_secs = 0.0
        ffmpeg_output: list[str] = []
        if not duration:
            print(f"\r    Converting {_progress_bar(0, 1)}", end="", flush=True)
        if proc.stdout:
            for line in proc.stdout:
                line = line.strip()
                if line.startswith("out_time_ms="):
                    try:
                        last_secs = int(line.split("=", 1)[1]) / 1_000_000
                    except ValueError:
                        continue
                    if duration:
                        pct = min(100, int((last_secs / duration) * 100))
                        bar = _progress_bar(last_secs, duration)
                        print(f"\r    Converting {bar} {pct:3d}%", end="", flush=True)
                elif line == "progress=end":
                    suffix = " 100%" if duration else ""
                    print(f"\r    Converting {_progress_bar(1, 1)}{suffix}", end="", flush=True)
                elif line:
                    ffmpeg_output.append(line)
                    ffmpeg_output = ffmpeg_output[-20:]

        rc = proc.wait()
        print()
        if rc != 0:
            print(f"    ffmpeg error: {' '.join(ffmpeg_output)[-300:].strip()}")
            return False
        return True
    except FileNotFoundError:
        print("    ERROR: ffmpeg not found. Install it: sudo apt install ffmpeg")
        return False
    except Exception as e:
        print(f"    ERROR: {e}")
        return False


def album_artist_value(tags: ID3 | None) -> str | None:
    if tags is None:
        return None
    for key in ALBUM_ARTIST_KEYS:
        frame = tags.get(key)
        if frame and hasattr(frame, "text") and frame.text:
            return str(frame.text[0])
    return None


def set_album_artist(tags: ID3, value: str) -> None:
    for key in ALBUM_ARTIST_KEYS:
        if key != "TPE2" and key in tags:
            del tags[key]
    tags["TPE2"] = TPE2(encoding=1, text=value)


# ── Tag reading ────────────────────────────────────────────────────────────────

def read_tags(mp3: Path) -> dict | None:
    """Return a flat tag dict for import, or None on error."""
    try:
        audio = _MP3Info(mp3, ID3=lambda *a, **kw: ID3(*a, translate=False, **kw))
    except Exception as e:
        print(f"  ERROR reading {mp3.name}: {e}")
        return None

    try:
        tags = load_id3(mp3)
    except ID3NoHeaderError:
        tags = None

    def g(k: str) -> str | None:
        if tags is None:
            return None
        f = tags.get(k)
        return str(f.text[0]) if f and hasattr(f, "text") else None

    year_raw = g("TYER") or g("TDRC")
    bitrate  = int(audio.info.bitrate / 1000) if audio.info else None
    return {
        "TPE1": g("TPE1"),
        "ALBUMARTIST": album_artist_value(tags),
        "TIT2": g("TIT2"),
        "TALB": g("TALB"),
        "YEAR": extract_year(year_raw) if year_raw else None,
        "TCON": g("TCON"),
        "TRCK": g("TRCK"),
        "TPOS": g("TPOS"),
        "_MP3_BITRATE": bitrate,
    }


# ── Prompting ──────────────────────────────────────────────────────────────────

def fill_album_tags(group: list[tuple[Path, dict]], label: str, dry_run: bool) -> None:
    """Auto-fill YEAR/TCON/ALBUMARTIST; prompt for artist/album title if still missing."""
    # Year: try folder name for a 4-digit year, fall back to 1900.
    year_default  = extract_year(label) or "1900"
    needs_year    = any(not td.get("YEAR") for _, td in group)
    needs_genre   = any(not td.get("TCON") for _, td in group)
    missing_prompt = [k for k in ("TPE1", "TALB")
                      if any(not td.get(k) for _, td in group)]

    if dry_run:
        if needs_year:
            print(f"  (dry run) Would set Year to '{year_default}'  [{label}]")
        if needs_genre:
            print(f"  (dry run) Would set Genre to 'Unknown'  [{label}]")
        if any(not td.get("ALBUMARTIST") and td.get("TPE1") for _, td in group):
            print(f"  (dry run) Would set Album Artist from Artist  [{label}]")
        if missing_prompt:
            print(f"  (dry run) Would prompt for: "
                  f"{', '.join(TAG_NAMES.get(k, k) for k in missing_prompt)}  [{label}]")
        for _, td in group:
            if not td.get("ALBUMARTIST") and td.get("TPE1"):
                td["ALBUMARTIST"] = td["TPE1"]
        return

    if needs_year:
        print(f"  Auto-fill Year: '{year_default}'  [{label}]")
    if needs_genre:
        print(f"  Auto-fill Genre: 'Unknown'  [{label}]")

    for _, td in group:
        if not td.get("YEAR"):
            td["YEAR"] = year_default
        if not td.get("TCON"):
            td["TCON"] = "Unknown"

    if missing_prompt:
        print(f"\n  ── {label} ──")
        for key in ("TPE1", "TALB"):
            if key not in missing_prompt:
                continue
            suggestion = next((td[key] for _, td in group if td.get(key)), "")
            prompt = f"    {TAG_NAMES.get(key, key)}"
            if suggestion:
                prompt += f" [{suggestion}]"
            prompt += ": "
            val = get_input(prompt)
            if not val and suggestion:
                val = suggestion
            if val:
                for _, td in group:
                    if not td.get(key):
                        td[key] = val

    for _, td in group:
        if not td.get("ALBUMARTIST") and td.get("TPE1"):
            td["ALBUMARTIST"] = td["TPE1"]


def fill_track_tags(mp3: Path, td: dict, dry_run: bool) -> None:
    """Prompt for Title if missing; update td in place."""
    for key in TRACK_TAGS:
        if td.get(key):
            continue
        if dry_run:
            print(f"    (dry run) {mp3.name}: missing {TAG_NAMES.get(key, key)}")
            continue
        suggestion = ""
        if key == "TIT2":
            stem = mp3.stem
            if " - " in stem:
                suggestion = stem.split(" - ", 1)[-1]
            else:
                suggestion = re.sub(r"^\d+[\.\s\-]+", "", stem).strip() or stem
        prompt = f"    {mp3.name}  {TAG_NAMES.get(key, key)}"
        if suggestion:
            prompt += f" [{suggestion}]"
        prompt += ": "
        val = get_input(prompt)
        if not val and suggestion:
            val = suggestion
        if val:
            td[key] = val


# ── Core ──────────────────────────────────────────────────────────────────────

def _track_sort_key(mp3: Path, td: dict) -> tuple[int, int]:
    disc_raw  = (td.get("TPOS") or "1").split("/")[0].strip()
    trck_raw  = (td.get("TRCK") or "").split("/")[0].strip()
    try:
        disc = int(disc_raw)
    except ValueError:
        disc = 1
    try:
        track = int(trck_raw)
    except ValueError:
        m = re.match(r"^(\d+)", mp3.stem)
        track = int(m.group(1)) if m else 9999
    return (disc, track)


def _find_cover(folder: Path) -> Path | None:
    for f in sorted(folder.iterdir()):
        if f.is_file() and f.stem.lower() == "cover" and f.suffix.lower() in IMAGE_EXTENSIONS:
            return f
    for f in sorted(folder.iterdir()):
        if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS:
            return f
    return None


def _prepare_cover_data(image_path: Path, max_size: int) -> tuple[bytes, str] | None:
    try:
        suffix = image_path.suffix.lower()
        mime   = "image/jpeg" if suffix in (".jpg", ".jpeg") else "image/png"
        data   = image_path.read_bytes()
        if max_size > 0:
            try:
                import io
                from PIL import Image
                img = Image.open(image_path)
                if img.width > max_size or img.height > max_size:
                    img = img.convert("RGB")
                    img.thumbnail((max_size, max_size), Image.LANCZOS)
                    buf = io.BytesIO()
                    img.save(buf, "JPEG", quality=88)
                    data = buf.getvalue()
                    mime = "image/jpeg"
            except ImportError:
                pass
        return data, mime
    except Exception as e:
        print(f"  ERROR reading {image_path.name}: {e}")
        return None


def _create_placeholder_cover(path: Path) -> bool:
    """Write a 600x600 solid dark-grey JPEG. Returns True on success."""
    try:
        from PIL import Image
        img = Image.new("RGB", (600, 600), color=(30, 30, 30))
        img.save(path, "JPEG", quality=85)
        return True
    except ImportError:
        print("    (Pillow not installed — run: pip install Pillow)")
        return False
    except Exception as e:
        print(f"    ERROR creating placeholder: {e}")
        return False


def import_tracks(source: Path, library: Path, dry_run: bool,
                  cover_art: str = "folder", cover_art_size: int = 500) -> None:
    print(f"Source  : {source}")
    print(f"Library : {library}")
    if dry_run:
        print("Mode    : DRY RUN – no files will be modified")
    print()

    # ── Discover ──────────────────────────────────────────────────────────────
    all_mp3s     = sorted(source.rglob("*.mp3"))
    all_lossless = find_lossless(source)

    if not all_mp3s and not all_lossless:
        print("No MP3 or lossless files found in source directory.")
        return

    if all_mp3s:
        print(f"Found {len(all_mp3s)} MP3 file(s).")
    if all_lossless:
        ext_summary = ", ".join(
            f"{sum(1 for f in all_lossless if f.suffix.lower() == e)} "
            f"{e.upper().lstrip('.')}"
            for e in sorted({f.suffix.lower() for f in all_lossless})
        )
        print(f"Found {len(all_lossless)} lossless file(s): {ext_summary}.")

    print("Reading tags...\n")

    # ── Read tags ──────────────────────────────────────────────────────────────
    entries: list[tuple[Path, dict]] = []
    for mp3 in all_mp3s:
        td = read_tags(mp3)
        if td is None:
            continue
        entries.append((mp3, td))

    for lf in all_lossless:
        if lf.suffix.lower() == ".flac":
            cue_entries = read_cue_tracks(lf)
            if cue_entries:
                entries.extend(cue_entries)
                continue
        td = read_lossless_tags(lf)
        entries.append((lf, td))

    # ── Fill missing tags (grouped by source folder for prompting) ─────────────
    by_src: dict[Path, list[tuple[Path, dict]]] = defaultdict(list)
    for mp3, td in entries:
        by_src[mp3.parent].append((mp3, td))

    for src_folder in sorted(by_src):
        group = by_src[src_folder]
        label = src_folder.name if src_folder != source else source.name
        fill_album_tags(group, label, dry_run)
        for mp3, td in group:
            fill_track_tags(mp3, td, dry_run)

    # ── Normalize tags in memory ───────────────────────────────────────────────
    def _normalize_entries(elist):
        for _, td in elist:
            for key in ("TPE1", "ALBUMARTIST", "TIT2", "TALB", "TCON"):
                if td.get(key):
                    td[key] = normalize_string(td[key])
            if td.get("YEAR"):
                td["YEAR"] = extract_year(td["YEAR"]) or td["YEAR"]

    _normalize_entries(entries)

    # ── Import preview ─────────────────────────────────────────────────────────
    proceed = run_preview(entries, bool(all_lossless))
    if not proceed:
        print("\nImport aborted.")
        return

    # Drop lossless entries the user chose to skip in the preview
    if all_lossless:
        entries = [(src, td) for src, td in entries
                   if src.suffix.lower() not in LOSSLESS_EXTENSIONS
                   or td.get("_LOSSLESS_BITRATE") is not None]

    # Re-normalize in case the user edited tags in the preview
    _normalize_entries(entries)

    # ── Group by tag-derived destination folder ────────────────────────────────
    by_dest: dict[tuple[str, str], list[tuple[Path, dict]]] = defaultdict(list)
    skipped = 0
    for mp3, td in entries:
        if not td.get("TPE1") or not td.get("ALBUMARTIST") or not td.get("TALB") or not td.get("YEAR"):
            print(f"  SKIP (missing Artist/Album Artist/Album/Year after prompts): {mp3.name}")
            skipped += 1
            continue
        artist_dir = sanitize_name(td["ALBUMARTIST"])
        album_dir  = sanitize_name(f"{td['YEAR']} - {td['TALB']}")
        by_dest[(artist_dir, album_dir)].append((mp3, td))

    stats = {"copied": 0, "skipped": skipped, "errors": 0}

    # ── Copy each destination group ────────────────────────────────────────────
    for (artist_dir, album_dir), group in sorted(by_dest.items()):
        dest_folder = library / artist_dir / album_dir

        album_artist_tag = Counter(td["ALBUMARTIST"] for _, td in group).most_common(1)[0][0]
        artist_tag       = Counter(td["TPE1"] for _, td in group).most_common(1)[0][0]
        album_tag        = Counter(td["TALB"] for _, td in group).most_common(1)[0][0]
        year_tag         = Counter(td["YEAR"] for _, td in group).most_common(1)[0][0]

        print(f"{'─' * 60}")
        print(f"  Destination : {artist_dir}/{album_dir}")
        print(f"  Tracks      : {len(group)}")

        # ── Conflict check ─────────────────────────────────────────────────────
        offset = 0
        existing_mp3s: list[Path] = []

        if dest_folder.exists():
            existing_mp3s = sorted(dest_folder.glob("*.mp3"))
            if existing_mp3s:
                print(f"  Existing    : {len(existing_mp3s)} track(s) already in library")
                if dry_run:
                    print("  (dry run) Would prompt to add or skip")
                    offset = len(existing_mp3s)
                else:
                    choice = ""
                    while choice not in ("a", "s"):
                        choice = get_input("  [A]dd to existing album  [S]kip: ").lower()
                    if choice == "s":
                        print("  Skipped.\n")
                        stats["skipped"] += len(group)
                        continue
                    offset = len(existing_mp3s)

        # ── Sort and assign track numbers ──────────────────────────────────────
        group_sorted = sorted(group, key=lambda x: _track_sort_key(x[0], x[1]))
        total = offset + len(group_sorted)
        width = 3 if total >= 100 else 2

        if not dry_run:
            dest_folder.mkdir(parents=True, exist_ok=True)

        # Update TRCK totals on any existing tracks we're appending to
        if offset > 0 and not dry_run:
            for ex in existing_mp3s:
                try:
                    etags = load_id3(ex)
                    trck  = etags.get("TRCK")
                    if trck:
                        n, _ = parse_track(str(trck.text[0]))
                        if n is not None:
                            etags["TRCK"] = TRCK(encoding=1,
                                text=f"{str(n).zfill(width)}/{total}")
                            etags.save(ex, v2_version=3, v1=0)
                except Exception as e:
                    print(f"  ERROR updating existing TRCK ({ex.name}): {e}")

        # ── Locate cover source and pre-prepare embed data ─────────────────────
        src_folders = {src.parent for src, _ in group}
        cover_src   = None
        for sf in sorted(src_folders):
            if sf.is_dir():
                c = _find_cover(sf)
                if c:
                    cover_src = c
                    break

        cover_apic_data: tuple[bytes, str] | None = None
        if cover_art in ("embed", "both") and cover_src:
            cover_apic_data = _prepare_cover_data(cover_src, cover_art_size)
            if cover_apic_data:
                print(f"  Cover art  : embedding from {cover_src.name}")

        # ── Copy new files ─────────────────────────────────────────────────────
        for i, (src, td) in enumerate(group_sorted, offset + 1):
            artist_safe = sanitize_name(td.get("TPE1") or artist_tag)
            title_safe  = sanitize_name(td.get("TIT2") or src.stem)
            new_name    = f"{str(i).zfill(width)}. {artist_safe} - {title_safe}.mp3"
            dest_path   = dest_folder / new_name
            is_lossless = src.suffix.lower() in LOSSLESS_EXTENSIONS

            if dest_path.exists():
                print(f"  SKIP (file exists): {new_name}")
                stats["skipped"] += 1
                continue

            lossless_bitrate = td.get("_LOSSLESS_BITRATE")
            lossless_label = (f" [{lossless_bitrate} kbps]" if is_lossless and lossless_bitrate
                              else (" [lossless → MP3]" if is_lossless else ""))
            print(f"  {src.parent.name}/{src.name}{lossless_label}")
            print(f"    → {new_name}")

            if dry_run:
                stats["copied"] += 1
                continue

            tmp_path = dest_path.with_suffix(dest_path.suffix + ".part")
            try:
                if is_lossless:
                    cue_start = td.get("_CUE_START")
                    cue_end   = td.get("_CUE_END")
                    if not convert_to_mp3_progress(src, tmp_path, lossless_bitrate,
                                                   cue_start, cue_end):
                        stats["errors"] += 1
                        if tmp_path.exists():
                            tmp_path.unlink()
                        continue
                else:
                    shutil.copy2(src, tmp_path)

                try:
                    dtags = load_id3(tmp_path)
                except ID3NoHeaderError:
                    dtags = ID3()

                for key in list(dtags.keys()):
                    if key[:4] not in KEEP_TAGS:
                        del dtags[key]

                dtags["TPE1"] = TPE1(encoding=1, text=td.get("TPE1") or artist_tag)
                set_album_artist(dtags, td.get("ALBUMARTIST") or album_artist_tag)
                dtags["TIT2"] = TIT2(encoding=1, text=td.get("TIT2") or src.stem)
                dtags["TALB"] = TALB(encoding=1, text=album_tag)
                dtags["TYER"] = TYER(encoding=1, text=year_tag)
                dtags["TRCK"] = TRCK(encoding=1,
                    text=f"{str(i).zfill(width)}/{total}")
                if td.get("TCON"):
                    dtags["TCON"] = TCON(encoding=1, text=td["TCON"])
                if cover_apic_data:
                    apic_data, apic_mime = cover_apic_data
                    dtags["APIC:"] = APIC(
                        encoding=3, mime=apic_mime, type=3, desc="", data=apic_data,
                    )

                dtags.save(tmp_path, v2_version=3, v1=0)
                tmp_path.rename(dest_path)
                stats["copied"] += 1

            except BaseException as e:
                if tmp_path.exists():
                    tmp_path.unlink()
                if isinstance(e, Exception):
                    print(f"    ERROR: {e}")
                    stats["errors"] += 1
                else:
                    raise

        # ── Cover image file ───────────────────────────────────────────────────
        if cover_art in ("folder", "both"):
            if cover_src:
                dest_cover = dest_folder / ("cover" + cover_src.suffix.lower())
                if not dest_cover.exists():
                    print(f"  Cover       : {cover_src.name}  →  {dest_cover.name}")
                    if not dry_run:
                        try:
                            shutil.copy2(cover_src, dest_cover)
                        except Exception as e:
                            print(f"    ERROR copying cover: {e}")
            else:
                has_cover = dest_folder.exists() and any(
                    f.is_file() and f.stem.lower() == "cover"
                    and f.suffix.lower() in IMAGE_EXTENSIONS
                    for f in dest_folder.iterdir()
                )
                if not has_cover:
                    # Prefer renaming an existing image over generating a blank placeholder.
                    existing_img = None
                    if dest_folder.exists():
                        for f in sorted(dest_folder.iterdir()):
                            if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS:
                                existing_img = f
                                break

                    if existing_img:
                        dest_cover = dest_folder / ("cover" + existing_img.suffix.lower())
                        print(f"  Cover       : {existing_img.name}  →  {dest_cover.name}")
                        if not dry_run:
                            try:
                                existing_img.rename(dest_cover)
                            except Exception as e:
                                print(f"    ERROR renaming cover: {e}")
                    else:
                        placeholder = dest_folder / "cover.jpg"
                        print(f"  Cover       : creating placeholder cover.jpg")
                        if not dry_run:
                            _create_placeholder_cover(placeholder)

        print()

    # ── Summary ────────────────────────────────────────────────────────────────
    print("═" * 60)
    print(f"  Copied  : {stats['copied']}")
    print(f"  Skipped : {stats['skipped']}")
    print(f"  Errors  : {stats['errors']}")
    if not dry_run and stats["copied"] > 0:
        print()
        print("  Run Audit from mp3tools to verify compliance.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import MP3s from a source directory into a music library",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python import_tracks.py ~/Downloads/NewAlbum
  python import_tracks.py ~/Downloads/NewAlbum ~/Music
  python import_tracks.py ~/Downloads/NewAlbum -n
        """,
    )
    parser.add_argument("source",  type=Path, help="Source directory with MP3s to import")
    parser.add_argument("library", type=Path, nargs="?", default=Path.cwd(),
                        help="Music library root directory (default: current directory)")
    parser.add_argument(
        "-n", "--dry-run",
        action="store_true",
        help="Show what would be done without modifying anything",
    )
    parser.add_argument(
        "--cover-art",
        choices=["folder", "embed", "both"],
        default=None,
        help="Cover art mode (default: read from library settings)",
    )
    parser.add_argument(
        "--cover-art-size",
        type=int,
        default=None,
        metavar="PX",
        help="Max embed pixel width (0 = no resize; default: read from library settings)",
    )
    args = parser.parse_args()

    for path, name in [(args.source, "source"), (args.library, "library")]:
        if not path.is_dir():
            print(f"Error: not a directory: {path} ({name})", file=sys.stderr)
            sys.exit(1)

    src_resolved = args.source.resolve()
    lib_resolved = args.library.resolve()
    if src_resolved == lib_resolved or lib_resolved in src_resolved.parents:
        print("Error: source cannot be the same as or a subdirectory of the library", file=sys.stderr)
        sys.exit(1)

    sett           = settings_mod.load(lib_resolved)
    cover_art      = args.cover_art or sett["cover_art"]
    cover_art_size = (args.cover_art_size if args.cover_art_size is not None
                      else sett["cover_art_embed_size"])

    import_tracks(src_resolved, lib_resolved, args.dry_run,
                  cover_art=cover_art, cover_art_size=cover_art_size)


if __name__ == "__main__":
    main()
