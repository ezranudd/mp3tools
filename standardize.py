#!/usr/bin/env python3
"""
Standardize all MP3 files in a music library directory.

Runs every standardization step in order. After completion all files
should comply with the style requirements in standard.md.

Steps
  1.  Merge disc subfolders into parent album folders
  2.  Prompt for any missing required ID3 tags
  3.  Enforce ID3v2.3: strip ID3v1 tags, downgrade ID3v2.4 to ID3v2.3, convert TDRC→TYER,
      fill TYER from album folder name when absent
  4.  Strip extraneous ID3 tags (keep only the required tags)
  5.  Normalize special characters in tags and filenames
  6.  Normalize year tags to 4-digit format
  7.  Zero-pad track numbers
  8.  Set total track counts in TRCK tag
  9.  Rename album folders to "YEAR - Album Title"
  10. Deduplicate album titles → retag/rename duplicates as "Title (2)", etc.
  11. Rename album artist folders to match album artist tag
  12. Rename MP3 files to "XX. Artist - Title.mp3"
  13. Remove non-MP3 files; keep exactly one cover image named cover.*

Usage
  python standardize.py ~/Music                 # full library
  python standardize.py ~/Music/Johnny\\ Paycheck  # single artist
  python standardize.py -n ~/Music              # dry run – show only
"""

import argparse
import re
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path

from convert_lossless import step_convert_lossless

from mutagen.id3 import (
    ID3, ID3NoHeaderError,
    TPE1, TIT2, TALB, TYER, TCON, TRCK, TXXX,
    TPE2,
)


# ── Shared constants ──────────────────────────────────────────────────────────

KEEP_TAGS = {"TPE1", "TPE2", "TIT2", "TALB", "TYER", "TCON", "TRCK"}
ALBUM_ARTIST_DESC = "album artist"
ALBUM_ARTIST_KEYS = (
    "TXXX:album artist",
    "TXXX:ALBUMARTIST",
    "TXXX:ALBUM ARTIST",
    "TXXX:AlbumArtist",
    "TXXX:Album Artist",
    "TPE2",
)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

_ALBUM_NAME_RE = re.compile(r'^\d{4}\s*-\s*')
_CD_NUM_RE     = re.compile(r'\d+')

# Character replacements (mirrors normalize_characters.py)
CHAR_REPLACEMENTS = {
    "‘": "'",  "’": "'",  "‚": "'",  "‛": "'",
    "`": "'",
    "“": '"',  "”": '"',  "„": '"',  "‟": '"',
    "«": '"',  "»": '"',
    "–": "-",  "—": "-",  "−": "-",
    "‐": "-",  "‑": "-",  "⁃": "-",
    "…": "...",
    " ": " ",  " ": " ",  " ": " ",
    " ": " ",  " ": " ",  "​": "",
    "×": "x",  "⁄": "/",  "∕": "/",
    "№": "No.", "℗": "(P)",
    "℃": "C",  "℉": "F",
    "™": "",   "®": "",   "©": "(C)",
    "•": "-",  "·": "-",
    "†": "+",  "‡": "++",
    "′": "'",  "″": '"',  "‴": "'''",
    "⁊": "&",
}

REQUIRED_TAG_NAMES = {
    "TPE1": "Artist",
    "ALBUMARTIST": "Album Artist",
    "TIT2": "Title",
    "TALB": "Album",
    "YEAR": "Year",   # virtual: TYER (step 3 converts any TDRC before this matters)
    "TCON": "Genre",
    "TRCK": "Track",
}
ALBUM_LEVEL_TAGS = {"TALB", "YEAR", "TCON", "TPE1", "ALBUMARTIST"}
TRACK_LEVEL_TAGS = {"TIT2", "TRCK"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_input(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)


def normalize_string(s: str) -> str:
    for old, new in CHAR_REPLACEMENTS.items():
        s = s.replace(old, new)
    return s


def has_special_chars(s: str) -> bool:
    return any(c in s for c in CHAR_REPLACEMENTS)


def sanitize_name(name: str) -> str:
    """Filesystem-safe name (used for both files and folders)."""
    name = normalize_string(name)
    for old, new in {"/": "-", "\\": "-", ":": " -", "*": "",
                     "?": "", '"': "'", "<": "", ">": "", "|": "-"}.items():
        name = name.replace(old, new)
    return name.rstrip(". ")


def extract_year(value: str) -> str | None:
    m = re.search(r'\b(19\d{2}|20\d{2})\b', str(value))
    return m.group(1) if m else None


def parse_track(s: str) -> tuple[int | None, int | None]:
    parts = s.split("/")
    try:
        n = int(parts[0]) if parts[0] else None
        t = int(parts[1]) if len(parts) > 1 and parts[1] else None
        return n, t
    except ValueError:
        return None, None


def album_folders(root: Path) -> list[Path]:
    """All folders that directly contain at least one MP3."""
    seen = set()
    result = []
    for mp3 in sorted(root.rglob("*.mp3")):
        p = mp3.parent
        if p not in seen:
            seen.add(p)
            result.append(p)
    return result


def _header(n: int, title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"Step {n}: {title}")
    print("=" * 60)


def load_id3(path: Path) -> ID3:
    """Load raw ID3 frames without mutagen's v2.4 translation layer."""
    return ID3(path, translate=False)


def album_artist_value(tags: ID3) -> str | None:
    for key in ALBUM_ARTIST_KEYS:
        frame = tags.get(key)
        if frame and hasattr(frame, "text") and frame.text:
            return str(frame.text[0])
    return None


def has_canonical_album_artist(tags: ID3) -> bool:
    return bool(tags.get(f"TXXX:{ALBUM_ARTIST_DESC}"))


def album_artist_mirror_needs_fix(tags: ID3, value: str) -> bool:
    tpe2 = tags.get("TPE2")
    if not tpe2 or not hasattr(tpe2, "text") or not tpe2.text:
        return True
    return str(tpe2.text[0]) != value


def set_album_artist(tags: ID3, value: str) -> None:
    canonical_key = f"TXXX:{ALBUM_ARTIST_DESC}"
    for key in ALBUM_ARTIST_KEYS:
        if key not in (canonical_key, "TPE2") and key in tags:
            del tags[key]
    tags["TPE2"] = TPE2(encoding=3, text=value)
    tags[canonical_key] = TXXX(
        encoding=3,
        desc=ALBUM_ARTIST_DESC,
        text=value,
    )


# ── Step 1: Merge disc subfolders ─────────────────────────────────────────────

def _subfolder_sort_key(p: Path) -> tuple:
    nums = _CD_NUM_RE.findall(p.name)
    return (0, int(nums[-1]), p.name.lower()) if nums else (1, 0, p.name.lower())


def _music_subfolders(parent: Path) -> list[Path]:
    return sorted(
        [d for d in parent.iterdir()
         if d.is_dir()
         and not d.name.startswith(".")
         and not _ALBUM_NAME_RE.match(d.name)
         and any(d.glob("*.mp3"))],
        key=_subfolder_sort_key,
    )


def _merge_one(album: Path, subfolders: list[Path], dry_run: bool) -> dict:
    """Merge subfolders into album folder. Returns stats."""
    all_mp3s: list[tuple[Path, int]] = []  # (path, sort_key)
    for sf in subfolders:
        files = []
        for mp3 in sf.glob("*.mp3"):
            sort_key = 9999
            try:
                tags = load_id3(mp3)
                trck = tags.get("TRCK")
                if trck:
                    n, _ = parse_track(str(trck.text[0]))
                    if n is not None:
                        sort_key = n
            except Exception:
                m = re.match(r'^(\d+)', mp3.stem)
                if m:
                    sort_key = int(m.group(1))
            files.append((mp3, sort_key))
        files.sort(key=lambda x: x[1])
        all_mp3s.extend(files)

    total = len(all_mp3s)
    width = 3 if total >= 100 else 2

    # Determine album title: most common across all subfolders
    album_titles: list[str] = []
    for mp3, _ in all_mp3s:
        try:
            tags = load_id3(mp3)
            talb = tags.get("TALB")
            if talb:
                album_titles.append(str(talb.text[0]))
        except Exception:
            pass
    album_title = Counter(album_titles).most_common(1)[0][0] if album_titles else None

    print(f"  Merging {len(subfolders)} subfolder(s), {total} tracks")
    print(f"  Order: {', '.join(sf.name for sf in subfolders)}")
    if album_title:
        print(f"  Album: {album_title}")

    stats = {"moved": 0, "errors": 0}
    for new_num, (mp3, _) in enumerate(all_mp3s, 1):
        # Build new filename: keep rest-of-name, replace track prefix
        stem = mp3.stem
        m = re.match(r'^\d+[.\s-]+(.+)$', stem)
        rest = m.group(1) if m else stem
        new_name = f"{new_num:0{width}d}. {rest}.mp3"
        new_path = album / new_name

        print(f"  {mp3.parent.name}/{mp3.name}  ->  {new_name}")

        if not dry_run:
            try:
                tags = load_id3(mp3)
                tags["TRCK"] = TRCK(encoding=3, text=f"{new_num}/{total}")
                if album_title:
                    tags["TALB"] = TALB(encoding=3, text=album_title)
                tags.save(mp3, v2_version=3, v1=0)
                mp3.rename(new_path)
                stats["moved"] += 1
            except Exception as e:
                print(f"    ERROR: {e}")
                stats["errors"] += 1

    # Copy cover if present
    for sf in subfolders:
        for ext in [".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"]:
            cover_src = sf / f"cover{ext}"
            if cover_src.exists():
                cover_dst = album / cover_src.name
                if not cover_dst.exists():
                    print(f"  Copy cover: {sf.name}/{cover_src.name}")
                    if not dry_run:
                        shutil.copy2(cover_src, cover_dst)
                break

    # Delete subfolders
    for sf in subfolders:
        print(f"  Delete: {sf.name}/")
        if not dry_run:
            try:
                shutil.rmtree(sf)
            except Exception as e:
                print(f"  ERROR deleting {sf.name}: {e}")

    return stats


def step_merge_subfolders(root: Path, dry_run: bool) -> dict:
    _header(1, "Merge disc subfolders")
    total_stats = {"albums": 0, "moved": 0, "errors": 0}

    # Walk sorted so deepest paths come first (rglob collects all, then sort by depth)
    candidates: list[Path] = []
    for d in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if d.is_dir():
            subs = _music_subfolders(d)
            if subs:
                candidates.append(d)

    if not candidates:
        print("  Nothing to merge.")
        return total_stats

    for album in candidates:
        subs = _music_subfolders(album)
        if not subs:
            continue
        rel = album.relative_to(root)
        print(f"\n  Album: {rel}")
        stats = _merge_one(album, subs, dry_run)
        total_stats["albums"] += 1
        total_stats["moved"]  += stats["moved"]
        total_stats["errors"] += stats["errors"]

    print(f"\n  Albums merged: {total_stats['albums']}  "
          f"Files moved: {total_stats['moved']}")
    return total_stats


# ── Step 2: Fix missing tags ──────────────────────────────────────────────────

def _read_required_tags(mp3: Path) -> dict | None:
    """Return dict with required tag keys (value or None)."""
    try:
        tags = load_id3(mp3)
    except ID3NoHeaderError:
        return {k: None for k in REQUIRED_TAG_NAMES}
    except Exception as e:
        print(f"  ERROR reading {mp3.name}: {e}")
        return None

    result = {}
    for key in REQUIRED_TAG_NAMES:
        if key == "YEAR":
            tyer = tags.get("TYER")
            tdrc = tags.get("TDRC")
            val  = str(tyer.text[0]) if tyer else (str(tdrc.text[0])[:4] if tdrc else None)
            result["YEAR"] = val
        elif key == "ALBUMARTIST":
            result[key] = album_artist_value(tags)
        else:
            frame = tags.get(key)
            result[key] = str(frame.text[0]) if frame else None
    return result


def _save_tag(mp3: Path, key: str, value: str) -> bool:
    try:
        try:
            tags = load_id3(mp3)
        except ID3NoHeaderError:
            tags = ID3()
        cls_map = {
            "TPE1": TPE1, "TIT2": TIT2, "TALB": TALB,
            "YEAR": TYER, "TCON": TCON, "TRCK": TRCK,
        }
        if key == "ALBUMARTIST":
            set_album_artist(tags, value)
            tags.save(mp3, v2_version=3, v1=0)
            return True
        actual = "TYER" if key == "YEAR" else key
        tags[actual] = cls_map[key](encoding=3, text=value)
        tags.save(mp3, v2_version=3, v1=0)
        return True
    except Exception as e:
        print(f"    ERROR saving {key}: {e}")
        return False


def step_fix_missing_tags(root: Path, dry_run: bool) -> dict:
    _header(2, "Fix missing tags")
    stats = {"fixed": 0, "skipped": 0}

    # Group files by album folder
    by_folder: dict[Path, list[tuple[Path, dict, list[str]]]] = defaultdict(list)
    for mp3 in sorted(root.rglob("*.mp3")):
        tag_dict = _read_required_tags(mp3)
        if tag_dict is None:
            continue
        missing = [k for k, v in tag_dict.items() if not v]
        if missing:
            by_folder[mp3.parent].append((mp3, tag_dict, missing))

    if not by_folder:
        print("  All required tags present.")
        return stats

    for folder in sorted(by_folder):
        entries = by_folder[folder]
        rel = folder.relative_to(root)
        print(f"\n  {rel}  ({len(entries)} file(s) with missing tags)")

        all_missing = {t for _, _, m in entries for t in m}
        album_missing = all_missing & ALBUM_LEVEL_TAGS

        # Collect album-level values once
        album_values: dict[str, str] = {}

        # Auto-fill YEAR from folder name or fall back to 1900
        if "YEAR" in album_missing:
            year_from_folder = extract_year(folder.name)
            auto_year = year_from_folder or "1900"
            source = f"from folder name '{folder.name}'" if year_from_folder else "default"
            album_values["YEAR"] = auto_year
            print(f"  -- Auto-fill Year: {auto_year} ({source})")

        # Album Artist is required, but it is derived: when absent, use Artist.
        # If Artist is also missing, the prompt below fills TPE1 first.
        prompt_album = album_missing - {"YEAR", "ALBUMARTIST"}
        if prompt_album and not dry_run:
            print(f"  -- Album-level tags (applies to all {len(entries)} files) --")
            for key in sorted(prompt_album):
                name = REQUIRED_TAG_NAMES[key]
                # Suggest existing value from first file that has it
                suggestion = ""
                for mp3, td, _ in entries:
                    v = td.get(key)
                    if v:
                        suggestion = v
                        break
                prompt = f"    {name}"
                if suggestion:
                    prompt += f" [{suggestion}]"
                prompt += ": "
                val = get_input(prompt)
                if not val and suggestion:
                    val = suggestion
                    print(f"      -> Using: {val}")
                if val:
                    album_values[key] = val
                    print(f"      -> Set {name} = '{val}'")
        elif prompt_album and dry_run:
            print(f"  -- Missing album tags (dry run): "
                  f"{', '.join(REQUIRED_TAG_NAMES[k] for k in sorted(prompt_album))} --")

        if "ALBUMARTIST" in album_missing:
            if dry_run:
                print("  -- Missing Album Artist (dry run): would copy Artist")
            else:
                print("  -- Auto-fill Album Artist from Artist")

        # Process each file
        for mp3, tag_dict, missing in entries:
            file_changed = False

            # Apply album-level tags
            for key, val in album_values.items():
                if key in missing:
                    if _save_tag(mp3, key, val):
                        file_changed = True

            if "ALBUMARTIST" in missing and not dry_run:
                album_artist = tag_dict.get("TPE1") or album_values.get("TPE1")
                if album_artist and _save_tag(mp3, "ALBUMARTIST", album_artist):
                    file_changed = True

            # Track-level tags
            track_missing = [k for k in missing if k in TRACK_LEVEL_TAGS]
            if track_missing and not dry_run:
                print(f"\n  {mp3.name}")
                for key in track_missing:
                    name = REQUIRED_TAG_NAMES[key]
                    suggestion = ""
                    if key == "TIT2" and " - " in mp3.stem:
                        suggestion = mp3.stem.split(" - ", 1)[-1]
                    prompt = f"    {name}"
                    if suggestion:
                        prompt += f" [{suggestion}]"
                    prompt += ": "
                    val = get_input(prompt)
                    if not val and suggestion:
                        val = suggestion
                        print(f"      -> Using: {val}")
                    if val:
                        if _save_tag(mp3, key, val):
                            file_changed = True
            elif track_missing and dry_run:
                print(f"    {mp3.name}: missing "
                      f"{', '.join(REQUIRED_TAG_NAMES[k] for k in track_missing)}")

            if file_changed:
                stats["fixed"] += 1
            else:
                stats["skipped"] += 1

    print(f"\n  Fixed: {stats['fixed']}  Skipped: {stats['skipped']}")
    return stats


# ── Step 3: Enforce ID3v2.3 / strip ID3v1 ────────────────────────────────────

def step_enforce_id3v23(root: Path, dry_run: bool) -> dict:
    _header(3, "Enforce ID3v2.3 (strip ID3v1, downgrade ID3v2.4, convert TDRC→TYER)")
    stats = {"fixed": 0}

    for mp3 in sorted(root.rglob("*.mp3")):
        # Check for ID3v1 (last 128 bytes start with b'TAG')
        try:
            with open(mp3, "rb") as f:
                f.seek(-128, 2)
                has_v1 = f.read(3) == b"TAG"
        except OSError:
            has_v1 = False

        # Check ID3v2 version and presence of TDRC (ID3v2.4 relic)
        try:
            tags = load_id3(mp3)
            wrong_version = tags.version[1] != 3
            has_tdrc = "TDRC" in tags
            has_tyer = bool(tags.get("TYER"))
        except ID3NoHeaderError:
            tags = ID3()
            wrong_version = False
            has_tdrc = False
            has_tyer = False
        except Exception:
            continue

        # If TYER is absent, pre-compute a year from the folder name as a fallback.
        folder_year = extract_year(mp3.parent.name) if not has_tyer else None

        needs_fix = has_v1 or wrong_version or has_tdrc or bool(folder_year)
        if not needs_fix:
            continue

        desc = []
        if wrong_version:
            desc.append(f"ID3v2.{tags.version[1]}")
        if has_v1:
            desc.append("ID3v1")
        if has_tdrc:
            desc.append("TDRC")
        if folder_year and not has_tdrc:
            desc.append(f"TYER missing → {folder_year} from folder")
        print(f"  {mp3.name}: {' + '.join(desc)} -> ID3v2.3")
        stats["fixed"] += 1

        if not dry_run:
            if wrong_version:
                tags.update_to_v23()
            # Convert any remaining TDRC to TYER, then remove it.
            # update_to_v23() handles true v2.4 files; this catches files whose
            # version byte was already 2.3 but still contained a TDRC frame.
            tdrc = tags.get("TDRC")
            if tdrc:
                year = extract_year(str(tdrc.text[0]))
                if year and not tags.get("TYER"):
                    tags["TYER"] = TYER(encoding=3, text=year)
                del tags["TDRC"]
            # Final fallback: if TYER is still absent, use the folder name year.
            # Covers TDRC-with-unparseable-value and files with no year tag at all.
            if not tags.get("TYER") and folder_year:
                tags["TYER"] = TYER(encoding=3, text=folder_year)
            tags.save(mp3, v2_version=3, v1=0)

    if stats["fixed"] == 0:
        print("  All files already ID3v2.3 with no ID3v1 or TDRC frames and TYER present.")
    else:
        print(f"\n  Files fixed: {stats['fixed']}")
    return stats


# ── Step 4: Strip extraneous tags ─────────────────────────────────────────────

def step_strip_tags(root: Path, dry_run: bool) -> dict:
    _header(4, "Strip extraneous tags")
    stats = {"files": 0, "tags_removed": 0, "albumartist_fixed": 0}

    for mp3 in sorted(root.rglob("*.mp3")):
        try:
            tags = load_id3(mp3)
        except Exception:
            continue

        album_artist = album_artist_value(tags)
        needs_albumartist = (
            bool(album_artist)
            and (
                not has_canonical_album_artist(tags)
                or album_artist_mirror_needs_fix(tags, album_artist)
            )
        )

        to_remove = []
        for key in tags.keys():
            base = key[:4]
            if base == "TXXX":
                desc = key[5:] if len(key) > 5 else ""
                if desc.lower() == "numtracks" or desc == ALBUM_ARTIST_DESC:
                    continue
            if base not in KEEP_TAGS:
                to_remove.append(key)

        if to_remove or needs_albumartist:
            actions = []
            if needs_albumartist:
                actions.append(f"write TXXX:{ALBUM_ARTIST_DESC} + TPE2")
            if to_remove:
                actions.append(f"remove {', '.join(sorted(to_remove))}")
            print(f"  {mp3.name}: {'; '.join(actions)}")
            stats["files"] += 1
            stats["tags_removed"] += len(to_remove)
            if not dry_run:
                if needs_albumartist:
                    set_album_artist(tags, album_artist)
                    stats["albumartist_fixed"] += 1
                for key in to_remove:
                    if key in tags:
                        del tags[key]
                tags.save(mp3, v2_version=3, v1=0)
            elif needs_albumartist:
                stats["albumartist_fixed"] += 1

    if stats["files"] == 0:
        print("  No extraneous tags found.")
    else:
        print(f"\n  Files modified: {stats['files']}  "
              f"Tags removed: {stats['tags_removed']}  "
              f"Album artists fixed: {stats['albumartist_fixed']}")
    return stats


# ── Step 4: Normalize characters ──────────────────────────────────────────────

def step_normalize_chars(root: Path, dry_run: bool) -> dict:
    _header(5, "Normalize special characters")
    stats = {"tags": 0, "files": 0}

    for mp3 in sorted(root.rglob("*.mp3")):
        try:
            tags = load_id3(mp3)
        except Exception:
            continue

        tag_changed = False
        for key in list(tags.keys()):
            frame = tags[key]
            if not hasattr(frame, "text"):
                continue
            new_text = []
            changed  = False
            for t in frame.text:
                if isinstance(t, str) and has_special_chars(t):
                    new_text.append(normalize_string(t))
                    changed = True
                else:
                    new_text.append(t)
            if changed:
                old_val = str(frame.text[0])[:60]
                new_val = str(new_text[0])[:60]
                print(f"  {mp3.name}  {key}: {old_val!r}  ->  {new_val!r}")
                frame.text = new_text
                tag_changed = True

        if tag_changed:
            stats["tags"] += 1
            if not dry_run:
                tags.save(mp3, v2_version=3, v1=0)

        # Normalize filename
        if has_special_chars(mp3.name):
            new_name = normalize_string(mp3.name)
            new_path = mp3.parent / new_name
            print(f"  RENAME: {mp3.name}  ->  {new_name}")
            stats["files"] += 1
            if not dry_run:
                mp3.rename(new_path)

    # Normalize folder names too (artist and album folders)
    for folder in sorted(root.rglob("*")):
        if folder.is_dir() and folder != root and has_special_chars(folder.name):
            new_name = normalize_string(folder.name)
            new_path = folder.parent / new_name
            if new_path != folder:
                print(f"  RENAME DIR: {folder.name}  ->  {new_name}")
                if not dry_run and folder.exists():
                    folder.rename(new_path)

    if stats["tags"] == 0 and stats["files"] == 0:
        print("  Nothing to normalize.")
    else:
        print(f"\n  Tags normalized: {stats['tags']}  Files renamed: {stats['files']}")
    return stats


# ── Step 5: Normalize year tags ───────────────────────────────────────────────

def step_normalize_year(root: Path, dry_run: bool) -> dict:
    _header(6, "Normalize year tags")
    stats = {"fixed": 0}

    for mp3 in sorted(root.rglob("*.mp3")):
        try:
            tags = load_id3(mp3)
        except Exception:
            continue

        changed = False
        for frame_id, cls in (("TYER", TYER),):
            frame = tags.get(frame_id)
            if not frame:
                continue
            current = str(frame.text[0])
            year    = extract_year(current)
            if year and current != year:
                print(f"  {mp3.name}  {frame_id}: {current!r}  ->  {year!r}")
                if not dry_run:
                    tags[frame_id] = cls(encoding=3, text=year)
                changed = True

        if changed:
            stats["fixed"] += 1
            if not dry_run:
                tags.save(mp3, v2_version=3, v1=0)

    if stats["fixed"] == 0:
        print("  All year tags already normalized.")
    else:
        print(f"\n  Files fixed: {stats['fixed']}")
    return stats


# ── Step 6: Pad track numbers ─────────────────────────────────────────────────

def step_pad_tracks(root: Path, dry_run: bool) -> dict:
    _header(7, "Pad track numbers")
    stats = {"fixed": 0}

    # Determine per-folder width
    folder_width: dict[Path, int] = {}
    for mp3 in root.rglob("*.mp3"):
        f = mp3.parent
        folder_width[f] = folder_width.get(f, 0) + 1
    folder_width = {f: (3 if n >= 100 else 2) for f, n in folder_width.items()}

    for mp3 in sorted(root.rglob("*.mp3")):
        try:
            tags = load_id3(mp3)
        except Exception:
            continue

        trck = tags.get("TRCK")
        if not trck:
            continue

        original = str(trck.text[0])
        num, total = parse_track(original)
        if num is None:
            continue

        width   = folder_width.get(mp3.parent, 2)
        padded  = str(num).zfill(width)
        padded_total = str(total) if total is not None else None
        new_val = f"{padded}/{padded_total}" if padded_total else padded

        if original != new_val:
            print(f"  {mp3.name}  TRCK: {original}  ->  {new_val}")
            stats["fixed"] += 1
            if not dry_run:
                tags["TRCK"] = TRCK(encoding=3, text=new_val)
                tags.save(mp3, v2_version=3, v1=0)

    if stats["fixed"] == 0:
        print("  All track numbers already padded.")
    else:
        print(f"\n  Files fixed: {stats['fixed']}")
    return stats


# ── Step 7: Set total track counts ────────────────────────────────────────────

def step_set_total_tracks(root: Path, dry_run: bool) -> dict:
    _header(8, "Set total track counts")
    stats = {"fixed": 0}

    for folder in sorted(album_folders(root)):
        mp3s  = sorted(folder.glob("*.mp3"))
        total = len(mp3s)
        width = 3 if total >= 100 else 2

        for mp3 in mp3s:
            try:
                tags = load_id3(mp3)
            except Exception:
                continue
            trck = tags.get("TRCK")
            if not trck:
                continue
            original = str(trck.text[0])
            num, cur_total = parse_track(original)
            if num is None:
                continue
            new_val = f"{str(num).zfill(width)}/{total}"
            if original != new_val:
                print(f"  {mp3.name}  TRCK: {original}  ->  {new_val}")
                stats["fixed"] += 1
                if not dry_run:
                    tags["TRCK"] = TRCK(encoding=3, text=new_val)
                    tags.save(mp3, v2_version=3, v1=0)

    if stats["fixed"] == 0:
        print("  All track totals already correct.")
    else:
        print(f"\n  Files fixed: {stats['fixed']}")
    return stats


# ── Step 8: Rename album folders ──────────────────────────────────────────────

def _album_folder_name(folder: Path) -> str | None:
    """Compute the correct "YEAR - Album" name from folder contents."""
    years, albums = [], []
    for mp3 in folder.glob("*.mp3"):
        try:
            tags = load_id3(mp3)
            for fid in ("TYER", "TDRC"):
                frame = tags.get(fid)
                if frame:
                    y = extract_year(str(frame.text[0]))
                    if y:
                        years.append(y)
                        break
            talb = tags.get("TALB")
            if talb:
                albums.append(str(talb.text[0]))
        except Exception:
            continue
    year  = Counter(years).most_common(1)[0][0]  if years  else None
    album = Counter(albums).most_common(1)[0][0] if albums else None
    if not year or not album:
        return None
    return sanitize_name(f"{year} - {album}")


def step_rename_album_folders(root: Path, dry_run: bool) -> dict:
    _header(9, "Rename album folders")
    stats = {"renamed": 0, "skipped": 0, "errors": 0}

    for folder in sorted(album_folders(root)):
        new_name = _album_folder_name(folder)
        if not new_name:
            print(f"  SKIP (missing tags): {folder.relative_to(root)}")
            stats["skipped"] += 1
            continue
        if folder.name == new_name:
            stats["skipped"] += 1
            continue
        new_path = folder.parent / new_name
        print(f"  {folder.name}")
        print(f"    -> {new_name}")
        if new_path.exists() and new_path != folder:
            print(f"    ERROR: target already exists")
            stats["errors"] += 1
            continue
        if not dry_run:
            try:
                folder.rename(new_path)
                stats["renamed"] += 1
            except Exception as e:
                print(f"    ERROR: {e}")
                stats["errors"] += 1
        else:
            stats["renamed"] += 1

    if stats["renamed"] == 0 and stats["errors"] == 0:
        print("  All album folders already named correctly.")
    else:
        print(f"\n  Renamed: {stats['renamed']}  "
              f"Skipped: {stats['skipped']}  Errors: {stats['errors']}")
    return stats


# ── Step 9: Deduplicate album titles ──────────────────────────────────────────

def step_deduplicate_albums(root: Path, dry_run: bool) -> dict:
    _header(10, "Deduplicate album titles")
    stats = {"retagged": 0, "renamed": 0, "errors": 0}

    artist_candidates: set[Path] = set()
    for mp3 in root.rglob("*.mp3"):
        album = mp3.parent
        artist = album.parent
        if artist != root and album.parent.parent == root:
            artist_candidates.add(artist)

    for artist_folder in sorted(artist_candidates):
        # Map each album subfolder to its dominant TALB value
        folder_title: dict[Path, str] = {}
        for album_folder in sorted(f for f in artist_folder.iterdir() if f.is_dir()):
            titles = []
            for mp3 in album_folder.glob("*.mp3"):
                try:
                    tags = load_id3(mp3)
                    talb = tags.get("TALB")
                    if talb:
                        titles.append(str(talb.text[0]))
                except Exception:
                    pass
            if titles:
                folder_title[album_folder] = Counter(titles).most_common(1)[0][0]

        # Group folders by TALB value; skip groups with only one album
        by_title: dict[str, list[Path]] = defaultdict(list)
        for folder, title in folder_title.items():
            by_title[title].append(folder)

        for title, folders in sorted(by_title.items()):
            if len(folders) < 2:
                continue
            # First folder is canonical; subsequent ones get (2), (3), ...
            for i, folder in enumerate(folders[1:], 2):
                new_title = f"{title} ({i})"
                mp3_list = sorted(folder.glob("*.mp3"))

                # Get year from first available MP3
                year = None
                for mp3 in mp3_list:
                    try:
                        tags = load_id3(mp3)
                        for fid in ("TYER", "TDRC"):
                            frame = tags.get(fid)
                            if frame:
                                y = extract_year(str(frame.text[0]))
                                if y:
                                    year = y
                                    break
                        if year:
                            break
                    except Exception:
                        pass

                new_folder_name = sanitize_name(
                    f"{year} - {new_title}" if year else new_title
                )
                new_folder_path = folder.parent / new_folder_name

                print(f"\n  Duplicate TALB '{title}'")
                print(f"    {folder.name}  ->  {new_folder_name}  (TALB: '{new_title}')")

                if dry_run:
                    stats["retagged"] += len(mp3_list)
                    stats["renamed"] += 1
                    continue

                for mp3 in mp3_list:
                    try:
                        tags = load_id3(mp3)
                        tags["TALB"] = TALB(encoding=3, text=new_title)
                        tags.save(mp3, v2_version=3, v1=0)
                        stats["retagged"] += 1
                    except Exception as e:
                        print(f"    ERROR retagging {mp3.name}: {e}")
                        stats["errors"] += 1

                if new_folder_path.exists() and new_folder_path != folder:
                    print(f"    ERROR: target folder already exists")
                    stats["errors"] += 1
                    continue
                try:
                    folder.rename(new_folder_path)
                    stats["renamed"] += 1
                except Exception as e:
                    print(f"    ERROR renaming folder: {e}")
                    stats["errors"] += 1

    if stats["retagged"] == 0 and stats["renamed"] == 0 and stats["errors"] == 0:
        print("  No duplicate album titles found.")
    else:
        print(f"\n  Albums renamed: {stats['renamed']}  "
              f"Files retagged: {stats['retagged']}  "
              f"Errors: {stats['errors']}")
    return stats


# ── Step 10: Rename album artist folders ──────────────────────────────────────

def step_rename_artist_folders(root: Path, dry_run: bool) -> dict:
    _header(11, "Rename album artist folders")
    stats = {"renamed": 0, "retagged": 0, "moved": 0, "skipped": 0, "errors": 0}

    # Album artist folders: direct children of root that do NOT directly contain MP3s
    # but whose children do contain MP3s (standard 3-level structure)
    artist_candidates: set[Path] = set()
    for mp3 in root.rglob("*.mp3"):
        album = mp3.parent
        artist = album.parent
        if artist != root and album.parent.parent == root:
            artist_candidates.add(artist)

    for artist_folder in sorted(artist_candidates):
        if not artist_folder.exists():
            continue

        # Most common album artist across all MP3s in this subtree.
        # Missing Album Artist is repaired from TPE1 before folder comparisons.
        names: list[str] = []
        mp3_list = sorted(artist_folder.rglob("*.mp3"))
        for mp3 in mp3_list:
            try:
                tags = load_id3(mp3)
                album_artist = album_artist_value(tags)
                tpe1 = tags.get("TPE1")
                if not album_artist and tpe1:
                    value = normalize_string(str(tpe1.text[0]))
                    names.append(value)
                    if not dry_run:
                        set_album_artist(tags, value)
                        tags.save(mp3, v2_version=3, v1=0)
                        stats["retagged"] += 1
                    continue
                if album_artist:
                    names.append(normalize_string(album_artist))
            except Exception:
                pass
        if not names:
            stats["skipped"] += 1
            continue

        new_name = sanitize_name(Counter(names).most_common(1)[0][0])
        retagged_all = False

        if artist_folder.name != new_name:
            # Mismatch: folder name differs from dominant album artist tag value
            print(f"\n  Mismatch detected:")
            print(f"    Folder       : {artist_folder.name}")
            print(f"    Album Artist : {new_name}")

            if dry_run:
                print(f"    (dry run) Would prompt: retag album artist or rename folder")
                stats["renamed"] += 1
                # fall through to album-level checks below
            else:
                choice = ""
                while choice not in ("r", "m", "s"):
                    choice = get_input(
                        f"    [R]etag album artist to match folder  "
                        f"[M]ove/rename folder to match album artist  "
                        f"[S]kip: "
                    ).lower()

                if choice == "s":
                    print("    Skipped.")
                    stats["skipped"] += 1
                    continue

                if choice == "r":
                    folder_artist = artist_folder.name
                    retagged = 0
                    for mp3 in mp3_list:
                        try:
                            tags = load_id3(mp3)
                            set_album_artist(tags, folder_artist)
                            tags.save(mp3, v2_version=3, v1=0)
                            retagged += 1
                        except Exception as e:
                            print(f"    ERROR retagging {mp3.name}: {e}")
                            stats["errors"] += 1
                    print(f"    Retagged {retagged} file(s) -> Album Artist='{folder_artist}'")
                    stats["retagged"] += retagged
                    retagged_all = True

                elif choice == "m":
                    new_path = artist_folder.parent / new_name
                    if new_path.exists() and new_path != artist_folder:
                        print(f"    ERROR: target already exists")
                        stats["errors"] += 1
                        continue
                    try:
                        artist_folder.rename(new_path)
                        print(f"    Renamed folder to '{new_name}'")
                        artist_folder = new_path
                        stats["renamed"] += 1
                    except Exception as e:
                        print(f"    ERROR: {e}")
                        stats["errors"] += 1
                        continue
        else:
            stats["skipped"] += 1

        # After retagging everything to the folder name there can't be mismatches
        if retagged_all:
            continue

        # Check each album subfolder for a mismatched dominant album artist
        effective_name = artist_folder.name
        for album_subfolder in sorted(f for f in artist_folder.iterdir() if f.is_dir()):
            album_artists: list[str] = []
            album_mp3s = sorted(album_subfolder.glob("*.mp3"))
            for mp3 in album_mp3s:
                try:
                    tags = load_id3(mp3)
                    album_artist = album_artist_value(tags)
                    tpe1 = tags.get("TPE1")
                    if not album_artist and tpe1:
                        value = normalize_string(str(tpe1.text[0]))
                        album_artists.append(value)
                        if not dry_run:
                            set_album_artist(tags, value)
                            tags.save(mp3, v2_version=3, v1=0)
                            stats["retagged"] += 1
                    elif album_artist:
                        album_artists.append(normalize_string(album_artist))
                except Exception:
                    pass
            if not album_artists:
                continue
            album_dominant = sanitize_name(Counter(album_artists).most_common(1)[0][0])
            unique_album_artists = sorted({sanitize_name(v) for v in album_artists})
            if album_dominant == effective_name and len(unique_album_artists) > 1:
                print(f"\n  Mixed album artists detected:")
                print(f"    Album        : {album_subfolder.name}")
                print(f"    Folder       : {effective_name}/")
                print(f"    Values       : {', '.join(unique_album_artists)}")

                if dry_run:
                    print(f"    (dry run) Would retag album artist to match folder")
                    stats["retagged"] += len(album_mp3s)
                    continue

                choice = ""
                while choice not in ("r", "s"):
                    choice = get_input(
                        f"    [R]etag album artist to match folder  [S]kip: "
                    ).lower()

                if choice == "s":
                    print("    Skipped.")
                    stats["skipped"] += 1
                    continue

                retagged = 0
                for mp3 in album_mp3s:
                    try:
                        tags = load_id3(mp3)
                        set_album_artist(tags, effective_name)
                        tags.save(mp3, v2_version=3, v1=0)
                        retagged += 1
                    except Exception as e:
                        print(f"    ERROR retagging {mp3.name}: {e}")
                        stats["errors"] += 1
                print(f"    Retagged {retagged} file(s) -> Album Artist='{effective_name}'")
                stats["retagged"] += retagged
                continue

            if album_dominant == effective_name:
                continue

            print(f"\n  Misplaced album detected:")
            print(f"    Album        : {album_subfolder.name}")
            print(f"    In           : {effective_name}/")
            print(f"    Album Artist : {album_dominant}")
            dest_album = root / album_dominant / album_subfolder.name
            print(f"    Move to      : {album_dominant}/{album_subfolder.name}")

            if dry_run:
                print(f"    (dry run) Would prompt: retag album artist or move album")
                stats["moved"] += 1
                continue

            choice = ""
            while choice not in ("r", "m", "s"):
                choice = get_input(
                    f"    [R]etag album artist to match folder  "
                    f"[M]ove album to correct album artist folder  [S]kip: "
                ).lower()

            if choice == "s":
                print("    Skipped.")
                stats["skipped"] += 1
                continue

            if choice == "r":
                retagged = 0
                for mp3 in album_mp3s:
                    try:
                        tags = load_id3(mp3)
                        set_album_artist(tags, effective_name)
                        tags.save(mp3, v2_version=3, v1=0)
                        retagged += 1
                    except Exception as e:
                        print(f"    ERROR retagging {mp3.name}: {e}")
                        stats["errors"] += 1
                print(f"    Retagged {retagged} file(s) -> Album Artist='{effective_name}'")
                stats["retagged"] += retagged
                continue

            if dest_album.exists():
                print(f"    ERROR: target already exists")
                stats["errors"] += 1
                continue
            try:
                dest_album.parent.mkdir(exist_ok=True)
                album_subfolder.rename(dest_album)
                print(f"    Moved to {album_dominant}/{album_subfolder.name}")
                stats["moved"] += 1
            except Exception as e:
                print(f"    ERROR: {e}")
                stats["errors"] += 1

    if stats["renamed"] == 0 and stats["retagged"] == 0 and stats["moved"] == 0 and stats["errors"] == 0:
        print("  All album artist folders already named correctly.")
    else:
        print(f"\n  Renamed: {stats['renamed']}  Retagged: {stats['retagged']}  "
              f"Moved: {stats['moved']}  Skipped: {stats['skipped']}  Errors: {stats['errors']}")
    return stats


# ── Step 11: Rename MP3 files ─────────────────────────────────────────────────

def step_rename_files(root: Path, dry_run: bool) -> dict:
    _header(12, "Rename MP3 files")
    stats = {"renamed": 0, "skipped": 0, "errors": 0}

    # Pre-compute width per folder
    folder_width: dict[Path, int] = {}
    for mp3 in root.rglob("*.mp3"):
        f = mp3.parent
        folder_width[f] = folder_width.get(f, 0) + 1
    folder_width = {f: (3 if n >= 100 else 2) for f, n in folder_width.items()}

    for mp3 in sorted(root.rglob("*.mp3")):
        try:
            tags = load_id3(mp3)
        except Exception:
            continue

        artist = tags.get("TPE1")
        title  = tags.get("TIT2")
        trck   = tags.get("TRCK")

        if not artist or not title or not trck:
            missing = [n for t, n in [("TPE1", "artist"), ("TIT2", "title"), ("TRCK", "track")]
                       if not tags.get(t)]
            print(f"  SKIP {mp3.name} (missing: {', '.join(missing)})")
            stats["skipped"] += 1
            continue

        num, _ = parse_track(str(trck.text[0]))
        if num is None:
            print(f"  SKIP {mp3.name} (invalid track number)")
            stats["skipped"] += 1
            continue

        width    = folder_width.get(mp3.parent, 2)
        a_str    = sanitize_name(str(artist.text[0]))
        t_str    = sanitize_name(str(title.text[0]))
        new_name = f"{str(num).zfill(width)}. {a_str} - {t_str}.mp3"

        if mp3.name == new_name:
            continue

        new_path = mp3.parent / new_name
        print(f"  {mp3.name}")
        print(f"    -> {new_name}")

        if new_path.exists() and new_path != mp3:
            print(f"    ERROR: target already exists")
            stats["errors"] += 1
            continue

        if not dry_run:
            try:
                mp3.rename(new_path)
                stats["renamed"] += 1
            except Exception as e:
                print(f"    ERROR: {e}")
                stats["errors"] += 1
        else:
            stats["renamed"] += 1

    if stats["renamed"] == 0 and stats["errors"] == 0:
        print("  All files already named correctly.")
    else:
        print(f"\n  Renamed: {stats['renamed']}  "
              f"Skipped: {stats['skipped']}  Errors: {stats['errors']}")
    return stats


# ── Step 12: Clean non-MP3 files and cover images ─────────────────────────────

def _cover_stem(name: str) -> bool:
    return Path(name).stem.lower() == "cover"


def _is_image(name: str) -> bool:
    return Path(name).suffix.lower() in IMAGE_EXTENSIONS


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


def step_clean_files(root: Path, dry_run: bool) -> dict:
    _header(13, "Clean non-MP3 files and cover images")
    stats = {"deleted": 0, "renamed_covers": 0, "placeholders": 0, "errors": 0}

    for folder in sorted(album_folders(root)):
        all_files = [f for f in folder.iterdir() if f.is_file()]
        mp3s      = [f for f in all_files if f.suffix.lower() == ".mp3"]
        images    = [f for f in all_files if _is_image(f.name)]
        others    = [f for f in all_files if f not in mp3s and f not in images]

        cover_images  = [f for f in images if _cover_stem(f.name)]
        other_images  = [f for f in images if not _cover_stem(f.name)]

        rel = folder.relative_to(root)

        # ── Determine which cover to keep ────────────────────────────────────
        keep_cover: Path | None = None
        to_delete: list[Path]   = list(others)  # non-MP3 non-image files always go

        if cover_images:
            keep_cover  = cover_images[0]
            to_delete  += cover_images[1:]  # extra cover.* files
            to_delete  += other_images      # other images go
        elif other_images:
            # No cover.* exists — pick best candidate, rename it
            keep_cover    = other_images[0]
            to_delete    += other_images[1:]
            new_stem      = "cover"
            new_cover_name = new_stem + keep_cover.suffix.lower()
            new_cover_path = keep_cover.parent / new_cover_name
            print(f"  [{rel}] rename cover: {keep_cover.name}  ->  {new_cover_name}")
            if not dry_run:
                try:
                    keep_cover.rename(new_cover_path)
                    stats["renamed_covers"] += 1
                    keep_cover = new_cover_path
                except Exception as e:
                    print(f"    ERROR: {e}")
                    stats["errors"] += 1
            else:
                stats["renamed_covers"] += 1

        for f in to_delete:
            print(f"  [{rel}] delete: {f.name}")
            if not dry_run:
                try:
                    f.unlink()
                    stats["deleted"] += 1
                except Exception as e:
                    print(f"    ERROR: {e}")
                    stats["errors"] += 1
            else:
                stats["deleted"] += 1

        if keep_cover is None:
            placeholder = folder / "cover.jpg"
            print(f"  [{rel}] create placeholder cover.jpg")
            if not dry_run:
                if _create_placeholder_cover(placeholder):
                    stats["placeholders"] += 1
                else:
                    stats["errors"] += 1
            else:
                stats["placeholders"] += 1

    if stats["deleted"] == 0 and stats["renamed_covers"] == 0 and stats["placeholders"] == 0:
        print("  All folders already clean.")
    else:
        print(f"\n  Files deleted: {stats['deleted']}  "
              f"Covers renamed: {stats['renamed_covers']}  "
              f"Placeholders created: {stats['placeholders']}  "
              f"Errors: {stats['errors']}")
    return stats


# ── Main ──────────────────────────────────────────────────────────────────────

STEPS = [
    step_merge_subfolders,       # 1
    step_fix_missing_tags,       # 2
    step_enforce_id3v23,         # 3
    step_strip_tags,             # 4
    step_normalize_chars,        # 5
    step_normalize_year,         # 6
    step_pad_tracks,             # 7
    step_set_total_tracks,       # 8
    step_rename_album_folders,   # 9
    step_deduplicate_albums,     # 10
    step_rename_artist_folders,  # 11
    step_rename_files,           # 12
    step_clean_files,            # 13
]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Standardize a music library to comply with standard.md",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python standardize.py ~/Music
  python standardize.py ~/Music/Johnny\\ Paycheck
  python standardize.py -n ~/Music   # dry run
        """,
    )
    parser.add_argument(
        "directory",
        type=Path,
        help="Music library root (or artist/album folder)",
    )
    parser.add_argument(
        "-n", "--dry-run",
        action="store_true",
        help="Show what would be changed without modifying anything",
    )
    parser.add_argument(
        "--steps",
        metavar="N[,N...]",
        help="Run only specific step numbers (e.g. --steps 4,5)",
    )
    args = parser.parse_args()

    root = args.directory.resolve()
    if not root.is_dir():
        print(f"Error: not a directory: {root}", file=sys.stderr)
        sys.exit(1)

    # Parse optional step filter
    step_filter: set[int] | None = None
    if args.steps:
        try:
            step_filter = {int(s) for s in args.steps.split(",")}
        except ValueError:
            print("Error: --steps expects comma-separated integers", file=sys.stderr)
            sys.exit(1)

    print("Standardize music library")
    print(f"Directory : {root}")
    if args.dry_run:
        print("Mode      : DRY RUN – no files will be modified")
    print()

    if not step_filter or 0 in step_filter:
        step_convert_lossless(root, args.dry_run)

    for idx, fn in enumerate(STEPS, 1):
        if step_filter and idx not in step_filter:
            continue
        fn(root, args.dry_run)

    print("\n" + "=" * 60)
    if args.dry_run:
        print("Dry run complete. Run without -n to apply changes.")
    else:
        print("Done. Run audit.py to verify compliance.")


if __name__ == "__main__":
    main()
