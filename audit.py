#!/usr/bin/env python3
"""
Scan a music directory for style compliance (read-only — no files modified).

Expected structure:
  root/
  └── Album Artist Name/     ← folder name must match Album Artist tag
      └── YEAR - Album Name/ ← folder name derived from Year + Album tags
          ├── 01. Artist Name - Track Title.mp3
          └── cover.jpg      ← exactly one cover, stem must be "cover"

Checks performed:
  1.  Required tags present (Artist, Album Artist, Title, Album, Year, Genre, Track)
  2.  No non-standard characters in tag values or filenames
  3.  Year tags normalized to 4-digit year only
  4.  TDRC frame absent (ID3v2.4 timestamp must not appear in ID3v2.3 files)
  5.  Track numbers zero-padded (01/9 not 1/9)
  6.  Only MP3 files + one "cover.*" image per album folder; no other files
  7.  Filename matches "XX. Artist - Title.mp3" derived from tags
  8.  Album folder name matches "YEAR - Album Title" derived from tags
  9.  Album artist (parent) folder name matches Album Artist tag
 10.  No CD subfolders (CD1, CD2, …) — flag for merge_cds
 11.  No other subfolders containing music
"""

import argparse
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import settings as settings_mod
import tagio
from chars import (
    extract_year,
    needs_normalization as has_nonstandard_chars,
    normalize,
    parse_track,
    sanitize,
)
# ID3 leaf helpers live in tagio (one home; audit holds no direct mutagen code).
# Re-export the historical names so audit's public contract is unchanged.
from tagio import (          # noqa: F401  (re-exported API)
    ALBUM_ARTIST_KEYS,
    album_artist_value,
    has_embedded_art,
    load_id3,
)

read_tags = tagio.mp3_read_framekey   # legacy frame-key dict (audit's contract)
_has_id3v1 = tagio.has_id3v1

# ─── Constants ────────────────────────────────────────────────────────────────

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
CD_PATTERN = re.compile(r"^CD(\d+)$")

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
    "ALBUM_ARTIST":  "Album artist issue",
    "ARTIST_FOLDER": "Album artist folder name mismatch",
    "CD_MERGE":      "CD subfolders need merging",
    "NESTED_MUSIC":  "Unexpected nested music",
}

# ─── Helpers ──────────────────────────────────────────────────────────────────
# ID3 leaf helpers (_has_id3v1, load_id3, has_embedded_art, album_artist_value,
# read_tags) are re-exported from tagio at the top of this module.


def year_from_tags(tags: dict) -> str | None:
    return tags.get("TYER") or tags.get("TDRC")


def build_expected_filename(tags: dict, width: int, ext: str = ".mp3") -> str | None:
    artist = tags.get("TPE1")
    title = tags.get("TIT2")
    trck = tags.get("TRCK")
    if not artist or not title or not trck:
        return None
    num, _ = parse_track(trck)
    if num is None:
        return None
    artist_s = sanitize(artist)
    title_s = sanitize(title)
    return f"{str(num).zfill(width)}. {artist_s} - {title_s}{ext}"


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
    return sanitize(f"{year} - {album}")


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
    """Check a single audio file. Returns (frame-key tags dict | None, issues).

    Issues are derived from the format-agnostic canonical model + diagnostics()
    (via tagio), so one code path serves every format. MP3-specific structural
    checks (ID3 version, ID3v1, TDRC relic, raw TPE2) key off diagnostics keys
    that only the MP3 backend provides — they cleanly vanish for other formats.
    The returned dict is the legacy frame-key shape scan() aggregates (identical
    to read_tags on MP3)."""
    a = tagio.open_audio(path)
    if a is None:
        return None, [Issue("READ_ERROR", "Cannot read ID3 tags")]
    c = a.read()
    d = a.diagnostics()
    issues: list[Issue] = []

    # Frame-key view for scan()'s album/folder aggregation (== read_tags on MP3;
    # for other formats, canonical date lands in the TYER slot so the shared
    # year/folder helpers keep working).
    tags = {
        "TPE1": c["artist"], "TPE2": d.get("tpe2"), "TIT2": c["title"],
        "TALB": c["album"],
        "TYER": d["tyer"] if "tyer" in d else c["date"],
        "TDRC": d.get("tdrc"),
        "TCON": c["genre"], "TRCK": c["track"],
        "ALBUMARTIST": c["album_artist"],
        "_version": d.get("id3_version"),
        "_legacy_albumartist": d.get("legacy_albumartist", False),
    }

    # 0. ID3-specific structural checks (present only when the backend reports them)
    ver = d.get("id3_version")
    if ver is not None and ver[1] != 3:
        issues.append(Issue("ID3_VERSION",
            f"ID3v2.{ver[1]} detected — must be ID3v2.3"))
    if d.get("has_id3v1"):
        issues.append(Issue("ID3_V1", "ID3v1 tag present — run standardize to remove"))
    if d.get("tdrc"):
        issues.append(Issue("RELIC_TAG",
            f"TDRC frame present ({d['tdrc']!r}) — ID3v2.4 timestamp in a v2.3 file; "
            "run standardize to convert to TYER"))

    # 1. Missing required tags
    missing = []
    if not c["artist"]: missing.append("Artist")
    if not c["album_artist"]: missing.append("Album Artist")
    if "tpe2" in d and not d["tpe2"]: missing.append("TPE2")
    if not c["title"]: missing.append("Title")
    if not c["album"]: missing.append("Album")
    year_val = d["tyer"] if "tyer" in d else c["date"]
    if not year_val: missing.append("Year")
    if not c["genre"]: missing.append("Genre")
    if not c["track"]: missing.append("Track")
    if missing:
        issues.append(Issue("MISSING_TAG", "Missing: " + ", ".join(missing)))

    if d.get("legacy_albumartist"):
        issues.append(Issue("ALBUM_ARTIST",
            "Legacy TXXX album-artist frame present — run standardize to remove"))

    # 2. Non-standard characters in tag values
    char_fields = [("Artist", c["artist"]), ("Album Artist", c["album_artist"]),
                   ("Title", c["title"]), ("Album", c["album"]), ("Genre", c["genre"])]
    if "tpe2" in d:
        char_fields.insert(2, ("TPE2", d["tpe2"]))
    for label, val in char_fields:
        if val and has_nonstandard_chars(val):
            issues.append(Issue("CHAR_NORM", f"{label}: {val!r} → {normalize(val)!r}"))

    # 2b. Non-standard characters in filename
    if has_nonstandard_chars(path.name):
        issues.append(Issue("CHAR_NORM", f"Filename: {path.name!r} → {normalize(path.name)!r}"))

    # 3. Date normalization (MP3: raw TYER, TDRC caught above; else canonical date)
    date_val = d["tyer"] if "tyer" in d else c["date"]
    if date_val:
        year = extract_year(date_val)
        if not year:
            issues.append(Issue("DATE_NORM", f"TYER: unrecognizable value {date_val!r}"))
        elif date_val != year:
            issues.append(Issue("DATE_NORM", f"TYER: {date_val!r} → {year!r}"))

    # 4. Track number padding
    trck = c["track"]
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
    exp = build_expected_filename(
        {"TPE1": c["artist"], "TIT2": c["title"], "TRCK": c["track"]},
        width, ext=path.suffix.lower())
    if exp and path.name != exp:
        issues.append(Issue("FILENAME", f"{path.name!r} → {exp!r}"))

    return tags, issues


# ─── Album-level audit ────────────────────────────────────────────────────────

def audit_cover_and_extras(
    folder: Path,
    mp3s: list[Path] | None = None,
    cover_art_mode: str = "folder",
    also_check_cd_subdirs: bool = False,
) -> list[Issue]:
    """
    Check cover art presence and extra files according to cover_art_mode:
      "folder" — require a cover.* file in the album folder
      "embed"  — require APIC frame in every MP3; no folder file expected
      "both"   — require both
    """
    issues: list[Issue] = []
    files = [f for f in folder.iterdir() if f.is_file() and not f.name.startswith(".")]
    non_mp3 = [f for f in files if f.suffix.lower() != ".mp3"]
    covers = [f for f in non_mp3 if f.stem.lower() == "cover" and f.suffix.lower() in IMAGE_EXTENSIONS]
    extras = [f for f in non_mp3 if f not in covers]

    need_folder = cover_art_mode in ("folder", "both")
    need_embed  = cover_art_mode in ("embed", "both")

    if need_folder:
        if not covers:
            if also_check_cd_subdirs:
                cd_cover = None
                for d in folder.iterdir():
                    if d.is_dir() and CD_PATTERN.match(d.name):
                        found = next(
                            (f for f in d.iterdir()
                             if f.is_file() and f.stem.lower() == "cover"
                             and f.suffix.lower() in IMAGE_EXTENSIONS),
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
                    issues.append(Issue("COVER",
                        "No cover image found (expected: cover.jpg, cover.png, etc.)"))
            else:
                issues.append(Issue("COVER",
                    "No cover image found (expected: cover.jpg, cover.png, etc.)"))
        elif len(covers) > 1:
            issues.append(Issue("COVER",
                f"Multiple cover images: {', '.join(f.name for f in sorted(covers))}"))

    if need_embed and mp3s:
        missing = [p for p in mp3s if not has_embedded_art(p)]
        if missing:
            if len(missing) == len(mp3s):
                issues.append(Issue("COVER", "No embedded art in any track"))
            else:
                names = ", ".join(p.name for p in missing[:5])
                suffix = f" (and {len(missing) - 5} more)" if len(missing) > 5 else ""
                issues.append(Issue("COVER",
                    f"Missing embedded art in {len(missing)} track(s): {names}{suffix}"))

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
    sett = settings_mod.load(root)
    cover_art_mode = sett.get("cover_art", "folder")

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
        album_issues += audit_cover_and_extras(
            album_folder, mp3s=mp3s,
            cover_art_mode=cover_art_mode,
            also_check_cd_subdirs=is_cd_parent,
        )

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

        # ── Album artist consistency check ────────────────────────────────────
        album_artist_values = [
            sanitize(t["ALBUMARTIST"])
            for t in all_tags
            if t.get("ALBUMARTIST")
        ]
        unique_album_artists = sorted(set(album_artist_values))
        if len(unique_album_artists) > 1:
            album_issues.append(Issue("ALBUM_ARTIST",
                "Album Artist varies within album: " + ", ".join(repr(v) for v in unique_album_artists)))

        # ── Album artist folder name check ────────────────────────────────────
        artist_folder = album_folder.parent
        if artist_folder != root:
            album_artists = [t["ALBUMARTIST"] for t in all_tags if t.get("ALBUMARTIST")]
            if album_artists:
                dominant = Counter(album_artists).most_common(1)[0][0]
                expected_album_artist = sanitize(dominant)
                if artist_folder.name != expected_album_artist:
                    album_issues.append(Issue("ARTIST_FOLDER",
                        f"Parent folder {artist_folder.name!r} ≠ Album Artist tag {expected_album_artist!r}"))

        results.append((album_folder, album_issues, file_results))

    return results


def scan_json(root: Path) -> dict:
    """JSON-safe view of scan() for the web UI.

    {
      "root": str,
      "category_labels": {cat: label, ...},
      "albums": [
        {
          "path": str, "name": str,
          "album_issues": [{"cat", "label", "msg"}, ...],
          "files": [{"path", "name", "issues": [{"cat","label","msg"}, ...]}, ...]
        }, ...
      ],
      "totals": {"albums", "albums_with_issues", "files", "files_with_issues"}
    }
    """
    def issue_json(iss: "Issue") -> dict:
        return {"cat": iss.cat,
                "label": CATEGORY_LABELS.get(iss.cat, iss.cat),
                "msg": iss.msg}

    results = scan(root)
    albums = []
    albums_with_issues = files = files_with_issues = 0
    for album_folder, album_issues, file_results in results:
        files += len(file_results)
        file_entries = []
        any_file_issue = False
        for mp3_path, _tags, issues in file_results:
            if issues:
                files_with_issues += 1
                any_file_issue = True
            file_entries.append({
                "path": str(mp3_path),
                "name": mp3_path.name,
                "issues": [issue_json(i) for i in issues],
            })
        if album_issues or any_file_issue:
            albums_with_issues += 1
        albums.append({
            "path": str(album_folder),
            "name": album_folder.name,
            "album_issues": [issue_json(i) for i in album_issues],
            "files": file_entries,
        })

    return {
        "root": str(root),
        "category_labels": dict(CATEGORY_LABELS),
        "albums": albums,
        "totals": {
            "albums": len(results),
            "albums_with_issues": albums_with_issues,
            "files": files,
            "files_with_issues": files_with_issues,
        },
    }


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

        # ── Album artist folder grouping header ───────────────────────────────
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
