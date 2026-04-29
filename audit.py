#!/usr/bin/env python3
"""
Scan a music directory for style compliance (read-only — no files modified).

Expected structure:
  root/
  └── Artist Name/           ← folder name must match Artist tag
      └── YEAR - Album Name/ ← folder name derived from Year + Album tags
          ├── 01. Artist Name - Track Title.mp3
          └── cover.jpg      ← exactly one cover, stem must be "cover"

Checks performed:
  1.  Required tags present (Artist, Title, Album, Year, Genre, Track)
  2.  No non-standard characters in tag values or filenames
  3.  Year tags normalized to 4-digit year only
  4.  TDRC frame absent (ID3v2.4 timestamp must not appear in ID3v2.3 files)
  5.  Track numbers zero-padded (01/9 not 1/9)
  6.  Only MP3 files + one "cover.*" image per album folder; no other files
  7.  Filename matches "XX. Artist - Title.mp3" derived from tags
  8.  Album folder name matches "YEAR - Album Title" derived from tags
  9.  Artist (parent) folder name matches Artist tag
 10.  No CD subfolders (CD1, CD2, …) — flag for merge_cds
 11.  No other subfolders containing music
"""

import argparse
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from mutagen.id3 import ID3, ID3NoHeaderError

# ─── Constants ────────────────────────────────────────────────────────────────

# From normalize_characters.py
CHAR_REPLACEMENTS: dict[str, str] = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "`": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "«": '"', "»": '"',
    "–": "-", "—": "-", "−": "-",
    "‐": "-", "‑": "-", "⁃": "-",
    "…": "...",
    " ": " ", " ": " ", " ": " ",
    " ": " ", " ": " ", "​": "",
    "×": "x", "⁄": "/", "∕": "/",
    "№": "No.", "℗": "(P)", "℃": "C", "℉": "F",
    "™": "", "®": "", "©": "(C)",
    "•": "-", "·": "-", "†": "+", "‡": "++",
    "′": "'", "″": '"', "‴": "'''", "⁊": "&",
}

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
CD_PATTERN = re.compile(r"^CD(\d+)$")
YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")

CATEGORY_LABELS: dict[str, str] = {
    "READ_ERROR":    "Tag read error",
    "MISSING_TAG":   "Missing required tag",
    "ID3_VERSION":   "Wrong ID3 version (must be ID3v2.3)",
    "ID3_V1":        "ID3v1 tag present (must be removed)",
    "RELIC_TAG":     "ID3v2.4 frame in ID3v2.3 file (TDRC must be converted to TYER)",
    "CHAR_NORM":     "Characters need normalization",
    "DATE_NORM":     "Date needs normalization",
    "TRACK_PAD":     "Track number not padded",
    "NON_MP3":       "Non-MP3 / non-cover file",
    "COVER":         "Cover image issue",
    "FILENAME":      "Filename mismatch",
    "FOLDER_NAME":   "Folder name mismatch",
    "ARTIST_FOLDER": "Artist folder name mismatch",
    "CD_MERGE":      "CD subfolders need merging",
    "NESTED_MUSIC":  "Unexpected nested music",
}

# ─── Helpers ──────────────────────────────────────────────────────────────────

def normalize(s: str) -> str:
    for old, new in CHAR_REPLACEMENTS.items():
        s = s.replace(old, new)
    return s


def has_nonstandard_chars(s: str) -> bool:
    return any(ch in s for ch in CHAR_REPLACEMENTS)


def extract_year(s: str) -> str | None:
    m = YEAR_RE.search(str(s))
    return m.group(1) if m else None


def parse_track(s: str) -> tuple[int | None, int | None]:
    parts = s.split("/")
    try:
        num = int(parts[0]) if parts[0].strip() else None
        total = int(parts[1]) if len(parts) > 1 and parts[1].strip() else None
        return num, total
    except (ValueError, IndexError):
        return None, None


def sanitize(name: str) -> str:
    """Replace filesystem-unsafe characters, matching rename_files.py behavior."""
    for old, new in {
        "/": "-", "\\": "-", ":": " -", "*": "",
        "?": "", '"': "'", "<": "", ">": "", "|": "-",
    }.items():
        name = name.replace(old, new)
    return name.rstrip(". ")


def _has_id3v1(path: Path) -> bool:
    """Return True if the file has an ID3v1 tag (last 128 bytes start with b'TAG')."""
    try:
        with open(path, "rb") as f:
            f.seek(-128, 2)
            return f.read(3) == b"TAG"
    except OSError:
        return False


def load_id3(path: Path) -> ID3:
    """Load raw ID3 frames without mutagen's v2.4 translation layer."""
    return ID3(path, translate=False)


def read_tags(path: Path) -> dict | None:
    """
    Read ID3 tags from file.
    Returns dict of tag values (None when tag absent) plus _version tuple,
    or None on read error.
    """
    keys = ("TPE1", "TIT2", "TALB", "TYER", "TDRC", "TCON", "TRCK")
    try:
        tags = load_id3(path)
        result = {
            k: (str(tags[k].text[0]) if k in tags and hasattr(tags[k], "text") else None)
            for k in keys
        }
        result["_version"] = tags.version
        return result
    except ID3NoHeaderError:
        result = {k: None for k in keys}
        result["_version"] = None
        return result
    except Exception:
        return None


def year_from_tags(tags: dict) -> str | None:
    return tags.get("TYER") or tags.get("TDRC")


def build_expected_filename(tags: dict, width: int) -> str | None:
    artist = tags.get("TPE1")
    title = tags.get("TIT2")
    trck = tags.get("TRCK")
    if not artist or not title or not trck:
        return None
    num, _ = parse_track(trck)
    if num is None:
        return None
    artist_s = sanitize(normalize(artist))
    title_s = sanitize(normalize(title))
    return f"{str(num).zfill(width)}. {artist_s} - {title_s}.mp3"


def build_expected_folder(tags_list: list[dict]) -> str | None:
    years, albums = [], []
    for t in tags_list:
        raw = year_from_tags(t)
        if raw:
            y = extract_year(raw)
            if y:
                years.append(y)
        if t.get("TALB"):
            albums.append(t["TALB"])
    year = Counter(years).most_common(1)[0][0] if years else None
    album = Counter(albums).most_common(1)[0][0] if albums else None
    if not year or not album:
        return None
    return sanitize(normalize(f"{year} - {album}"))


# ─── Issue ────────────────────────────────────────────────────────────────────

class Issue:
    __slots__ = ("cat", "msg")

    def __init__(self, cat: str, msg: str) -> None:
        self.cat = cat
        self.msg = msg

    def __str__(self) -> str:
        return f"[{CATEGORY_LABELS.get(self.cat, self.cat)}] {self.msg}"


# ─── File-level audit ─────────────────────────────────────────────────────────

def audit_file(path: Path, width: int) -> tuple[dict | None, list[Issue]]:
    """Check a single MP3 file. Returns (tags_or_None, issues)."""
    issues: list[Issue] = []
    tags = read_tags(path)

    if tags is None:
        return None, [Issue("READ_ERROR", "Cannot read ID3 tags")]

    # 0. ID3 version checks
    ver = tags.get("_version")
    if ver is not None and ver[1] != 3:
        issues.append(Issue("ID3_VERSION",
            f"ID3v2.{ver[1]} detected — must be ID3v2.3"))
    if _has_id3v1(path):
        issues.append(Issue("ID3_V1", "ID3v1 tag present — run standardize to remove"))

    # TDRC is an ID3v2.4 frame that must not appear in ID3v2.3 files
    if tags.get("TDRC"):
        issues.append(Issue("RELIC_TAG",
            f"TDRC frame present ({tags['TDRC']!r}) — ID3v2.4 timestamp in a v2.3 file; "
            "run standardize to convert to TYER"))

    # 1. Missing required tags
    missing = []
    if not tags.get("TPE1"): missing.append("Artist")
    if not tags.get("TIT2"): missing.append("Title")
    if not tags.get("TALB"): missing.append("Album")
    if not tags.get("TYER"): missing.append("Year")
    if not tags.get("TCON"): missing.append("Genre")
    if not tags.get("TRCK"): missing.append("Track")
    if missing:
        issues.append(Issue("MISSING_TAG", "Missing: " + ", ".join(missing)))

    # 2. Non-standard characters in tag values
    for label, key in [("Artist", "TPE1"), ("Title", "TIT2"), ("Album", "TALB"), ("Genre", "TCON")]:
        val = tags.get(key)
        if val and has_nonstandard_chars(val):
            issues.append(Issue("CHAR_NORM", f"{label}: {val!r} → {normalize(val)!r}"))

    # 2b. Non-standard characters in filename
    if has_nonstandard_chars(path.name):
        issues.append(Issue("CHAR_NORM", f"Filename: {path.name!r} → {normalize(path.name)!r}"))

    # 3. Date normalization (TYER only — TDRC must not be present, caught above)
    val = tags.get("TYER")
    if val:
        year = extract_year(val)
        if not year:
            issues.append(Issue("DATE_NORM", f"TYER: unrecognizable value {val!r}"))
        elif val != year:
            issues.append(Issue("DATE_NORM", f"TYER: {val!r} → {year!r}"))

    # 4. Track number padding
    trck = tags.get("TRCK")
    if trck:
        num, total = parse_track(trck)
        if num is not None:
            pn = str(num).zfill(width)
            pt = str(total) if total is not None else None
            expected = f"{pn}/{pt}" if pt else pn
            if trck != expected:
                issues.append(Issue("TRACK_PAD", f"TRCK: {trck!r} → {expected!r}"))
        else:
            issues.append(Issue("TRACK_PAD", f"TRCK: unparseable value {trck!r}"))

    # 6. Filename matches tags
    exp = build_expected_filename(tags, width)
    if exp and path.name != exp:
        issues.append(Issue("FILENAME", f"{path.name!r} → {exp!r}"))

    return tags, issues


# ─── Album-level audit ────────────────────────────────────────────────────────

def audit_cover_and_extras(folder: Path, also_check_cd_subdirs: bool = False) -> list[Issue]:
    """
    Check that the folder has exactly one cover image named 'cover.*'
    and no other non-MP3 files.
    """
    issues: list[Issue] = []
    files = [f for f in folder.iterdir() if f.is_file() and not f.name.startswith(".")]
    non_mp3 = [f for f in files if f.suffix.lower() != ".mp3"]
    covers = [f for f in non_mp3 if f.stem.lower() == "cover" and f.suffix.lower() in IMAGE_EXTENSIONS]
    extras = [f for f in non_mp3 if f not in covers]

    if not covers:
        if also_check_cd_subdirs:
            # Cover might be inside a CD subfolder; will be moved on merge
            cd_cover = None
            for d in folder.iterdir():
                if d.is_dir() and CD_PATTERN.match(d.name):
                    found = next(
                        (f for f in d.iterdir()
                         if f.is_file() and f.stem.lower() == "cover" and f.suffix.lower() in IMAGE_EXTENSIONS),
                        None,
                    )
                    if found:
                        cd_cover = found
                        break
            if cd_cover:
                issues.append(Issue("COVER",
                    f"Cover image is inside {cd_cover.parent.name}/ "
                    f"(will move to album folder on merge): {cd_cover.name}"))
            else:
                issues.append(Issue("COVER", "No cover image found (expected: cover.jpg, cover.png, etc.)"))
        else:
            issues.append(Issue("COVER", "No cover image found (expected: cover.jpg, cover.png, etc.)"))
    elif len(covers) > 1:
        issues.append(Issue("COVER", f"Multiple cover images: {', '.join(f.name for f in sorted(covers))}"))

    if extras:
        issues.append(Issue("NON_MP3", f"Files to remove: {', '.join(f.name for f in sorted(extras))}"))

    return issues


# ─── Full scan ────────────────────────────────────────────────────────────────

def scan(root: Path) -> list[tuple[Path, list[Issue], list[tuple]]]:
    """
    Walk root, identify album folders, run all checks.

    Returns sorted list of:
      (album_folder, album_issues, [(mp3_path, tags|None, file_issues), ...])

    An album folder is either:
    - A folder directly containing MP3 files (regular album)
    - A folder whose CDN-named children contain MP3 files (needs merge_cds)
    """
    # Discover all folders that directly contain at least one MP3
    leaf_folders: set[Path] = {mp3.parent for mp3 in root.rglob("*.mp3")}

    # Partition into CD-named leaves and regular leaves
    cd_leaves = {f for f in leaf_folders if CD_PATTERN.match(f.name)}
    regular = leaf_folders - cd_leaves
    cd_parents = {f.parent for f in cd_leaves}

    # Union: every unique album folder we need to check
    album_set = regular | cd_parents

    results = []

    for album_folder in sorted(album_set):
        album_issues: list[Issue] = []
        all_tags: list[dict] = []
        file_results: list[tuple] = []

        is_cd_parent = album_folder in cd_parents
        has_direct_mp3s = album_folder in regular

        # ── Collect MP3s and flag CD subfolders ───────────────────────────────
        mp3s: list[Path] = []

        if is_cd_parent:
            cd_dirs = sorted(
                [d for d in album_folder.iterdir()
                 if d.is_dir() and CD_PATTERN.match(d.name) and any(d.glob("*.mp3"))],
                key=lambda d: int(CD_PATTERN.match(d.name).group(1)),
            )
            if len(cd_dirs) >= 2:
                album_issues.append(Issue("CD_MERGE",
                    f"CD subfolders to merge: {', '.join(d.name for d in cd_dirs)}"))
            elif cd_dirs:
                album_issues.append(Issue("CD_MERGE",
                    f"Lone CD subfolder (no siblings to merge with): {cd_dirs[0].name}"))
            for cd_dir in cd_dirs:
                mp3s.extend(sorted(cd_dir.glob("*.mp3")))

        if has_direct_mp3s:
            direct = sorted(album_folder.glob("*.mp3"))
            if is_cd_parent:
                album_issues.append(Issue("NESTED_MUSIC",
                    "Folder has both direct MP3s and CD subfolders — unexpected mixed structure"))
            mp3s = direct + mp3s  # direct first, then CD content

        # ── Non-CD subfolders with music ──────────────────────────────────────
        non_cd_music = [
            d for d in album_folder.iterdir()
            if d.is_dir()
            and not CD_PATTERN.match(d.name)
            and not d.name.startswith(".")
            and any(d.rglob("*.mp3"))
        ]
        if non_cd_music:
            album_issues.append(Issue("NESTED_MUSIC",
                f"Subfolders with music files: {', '.join(d.name for d in sorted(non_cd_music))}"))

        # ── Cover + extra files ───────────────────────────────────────────────
        album_issues += audit_cover_and_extras(album_folder, also_check_cd_subdirs=is_cd_parent)

        # ── File-level checks ─────────────────────────────────────────────────
        width = 3 if len(mp3s) >= 100 else 2
        for mp3_path in mp3s:
            tags, issues = audit_file(mp3_path, width)
            if tags:
                all_tags.append(tags)
            file_results.append((mp3_path, tags, issues))

        # ── Folder name check ─────────────────────────────────────────────────
        exp_folder = build_expected_folder(all_tags)
        if exp_folder is None:
            album_issues.append(Issue("FOLDER_NAME",
                "Cannot determine expected name (files missing Year or Album tags)"))
        elif album_folder.name != exp_folder:
            album_issues.append(Issue("FOLDER_NAME",
                f"{album_folder.name!r} → {exp_folder!r}"))

        # ── Artist folder name check ──────────────────────────────────────────
        artist_folder = album_folder.parent
        if artist_folder != root:
            artists = [t["TPE1"] for t in all_tags if t.get("TPE1")]
            if artists:
                dominant = Counter(artists).most_common(1)[0][0]
                expected_artist = sanitize(normalize(dominant))
                if artist_folder.name != expected_artist:
                    album_issues.append(Issue("ARTIST_FOLDER",
                        f"Parent folder {artist_folder.name!r} ≠ Artist tag {expected_artist!r}"))

        results.append((album_folder, album_issues, file_results))

    return results


# ─── Report ───────────────────────────────────────────────────────────────────

def print_report(results: list, root: Path, show_ok: bool) -> None:
    if not results:
        print("No MP3 files found.")
        return

    total_albums = len(results)
    albums_with_issues = 0
    total_files = 0
    files_with_issues = 0
    issue_counts: dict[str, int] = defaultdict(int)

    current_parent: Path | None = None

    for album_folder, album_issues, file_results in results:
        file_issue_pairs = [(p, iss) for p, _, iss in file_results if iss]
        all_issues = album_issues + [i for _, iss in file_issue_pairs for i in iss]
        has_issues = bool(all_issues)

        total_files += len(file_results)
        if has_issues:
            albums_with_issues += 1
            files_with_issues += len(file_issue_pairs)
            for iss in all_issues:
                issue_counts[iss.cat] += 1

        # ── Artist folder grouping header ─────────────────────────────────────
        parent = album_folder.parent
        if parent != current_parent:
            current_parent = parent
            try:
                label = str(parent.relative_to(root))
            except ValueError:
                label = str(parent)
            print()
            print("━" * 72)
            print(f"  {label}/")
            print("━" * 72)

        if not has_issues and not show_ok:
            continue  # skip clean albums unless --all

        n_issues = len(all_issues)
        status = "OK" if not has_issues else f"{n_issues} issue{'s' if n_issues != 1 else ''}"
        print(f"\n  ▶ {album_folder.name}  [{status}]")

        if not has_issues:
            print(f"    ✓ {len(file_results)} file(s) — fully compliant")
            continue

        # Album-level issues
        for iss in album_issues:
            print(f"    [album] {iss}")

        # File-level issues (only files that have at least one issue)
        for mp3_path, _, file_issues in file_results:
            if not file_issues:
                continue
            try:
                rel = mp3_path.relative_to(album_folder)
            except ValueError:
                rel = mp3_path.name
            print(f"    ├─ {rel}")
            for iss in file_issues:
                print(f"    │  {iss}")

    # ─── Summary ──────────────────────────────────────────────────────────────
    print()
    print("━" * 72)
    print("  SUMMARY")
    print("━" * 72)
    print(f"  Albums scanned:     {total_albums:>5}")
    print(f"  Albums with issues: {albums_with_issues:>5}")
    print(f"  Files scanned:      {total_files:>5}")
    print(f"  Files with issues:  {files_with_issues:>5}")

    if issue_counts:
        print()
        print("  Issues by category:")
        for cat, label in CATEGORY_LABELS.items():
            n = issue_counts.get(cat, 0)
            if n:
                print(f"    {label:<44}  {n:>4}")
    else:
        print()
        print("  No issues found — everything is compliant!")


# ─── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit a music library for style compliance (read-only, no changes made)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python audit.py ~/Music/Johnny\\ Paycheck
  python audit.py ~/Music --all
        """,
    )
    parser.add_argument("directory", type=Path, help="Root directory to scan")
    parser.add_argument(
        "-a", "--all",
        action="store_true",
        help="Show all albums, including those with no issues",
    )
    args = parser.parse_args()

    if not args.directory.is_dir():
        print(f"Error: not a directory: {args.directory}", file=sys.stderr)
        sys.exit(1)

    print(f"Scanning: {args.directory}")
    print("(read-only audit — no files will be modified)")
    print()

    results = scan(args.directory)
    print_report(results, args.directory, show_ok=args.all)


if __name__ == "__main__":
    main()
