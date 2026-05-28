# MP3 Tools

> **Disclaimer:** This software is intended for managing music you own legally. Do not use it to copy, distribute, or rip media in violation of copyright law or any applicable terms of service.

A terminal-based music library manager for maintaining a locally standardized MP3 collection. Covers everything from tagging and renaming to CD ripping and device sync.

## Installation

```bash
pip install -e . --break-system-packages
```

This registers the `mp3tools` command. After installation, launch the TUI from anywhere:

```bash
mp3tools
```

Or run individual scripts directly from the project directory:

```bash
python tui.py
python audit.py ~/Music
python standardize.py ~/Music
python standardize.py -n ~/Music   # dry run
```

## Requirements

### Required

| Package   | Purpose                          | Install                        |
|-----------|----------------------------------|--------------------------------|
| `mutagen` | ID3 tag reading/writing          | `pip install mutagen`          |
| `wcwidth` | Unicode terminal width           | `pip install wcwidth`          |

### Optional

| Package / Tool          | Purpose                                            | Install                                              |
|-------------------------|----------------------------------------------------|------------------------------------------------------|
| `Pillow`                | Resize cover art before embedding                  | `pip install Pillow`                                 |
| `musicbrainzngs`        | MusicBrainz metadata lookup for CD ripping         | `pip install musicbrainzngs`                         |
| `discid` + `libdiscid0` | MusicBrainz disc ID from CD (preferred)            | `pip install discid` + `sudo apt install libdiscid0` |
| `cdparanoia`            | CD audio extraction                                | `sudo apt install cdparanoia`                        |
| `ffmpeg`                | WAV→FLAC and lossless→MP3 conversion               | `sudo apt install ffmpeg`                            |
| `cd-discid`             | CDDB disc ID fallback (when discid unavailable)    | `sudo apt install cd-discid`                         |
| `cd-info`               | CD-Text reading                                    | `sudo apt install libcdio-utils`                     |

## Features

### TUI (`tui.py`)

The TUI is a curses-based screen-stack interface. The main menu provides access to all workflows:

- **Browse** — navigate the library tree, view and edit tags inline, fetch or remove cover art per album or artist
- **Audit** — read-only scan; reports every compliance violation with counts and per-file detail
- **Standardize** — runs all fix steps in sequence (see [Standardization Steps](#standardization-steps))
- **Import** — copy tracks from a source directory into the library, normalizing tags and optionally converting lossless files; shows a preview before committing
- **Sync** — mirror selected artist folders to a device path
- **Rip CD** — rip a CD to FLAC, look up metadata from MusicBrainz → gnudb → CD-Text, then pass the output straight to the import workflow
- **Settings** — configure per-library options (cover art mode, online art sources, optional standardization steps)

### Audit (`audit.py`)

Read-only compliance scanner. Reports:

- Missing required tags
- Wrong ID3 version (must be ID3v2.3; no ID3v1, no ID3v2.4)
- Relic `TDRC` frames
- Curly quotes or apostrophes that need normalization
- Malformed year tags
- Track number padding issues
- Filename and folder name mismatches
- Cover image issues (missing, wrong name, multiple images)
- Non-MP3, non-image files in album folders
- CD subfolders needing merge
- Unexpected nested music structure
- Album artist mismatches and misplaced albums

### Standardize (`standardize.py`)

Runs up to 15 sequential fix steps. The core 13 steps always run; optional steps depend on library settings.

#### Standardization Steps

| Step | Name | Notes |
|------|------|-------|
| 0    | Convert lossless files | FLAC/ALAC → MP3 via ffmpeg |
| 1    | Merge disc subfolders | Flattens CD1/CD2/… into the album folder, renumbers tracks |
| 2    | Fix missing tags | Auto-fills Year from folder name; prompts for Album, Genre, Title |
| 3    | Enforce ID3v2.3 | Strips ID3v1, downgrades ID3v2.4, converts TDRC→TYER |
| 4    | Strip extraneous tags | Keeps only the 7 required frames (+ APIC in embed mode) |
| 5    | Normalize special characters | Curly quotes and apostrophes → ASCII equivalents |
| 5a   | Replace [] with () in titles | Optional; controlled by library setting |
| 6    | Normalize year tags | Trims `1999-01-01` → `1999` |
| 7    | Zero-pad track numbers | `1` → `01`, or `001` for 100+ track albums |
| 8    | Set total track counts | Writes `/N` total into every TRCK tag |
| 9    | Rename album folders | `YEAR - Album Title` format |
| 10   | Deduplicate album titles | Appends `(2)`, `(3)`, … to duplicate TALB values per artist |
| 11   | Rename album artist folders | Prompts when folder name and tag differ: retag or rename |
| 12a  | Enforce Artist = Album Artist | Optional; controlled by library setting |
| 12   | Rename MP3 files | `NN. Artist - Title.mp3` format |
| 13   | Clean non-MP3 files and covers | Keeps exactly one `cover.*` per album folder |
| 14   | Embed cover art | Writes APIC frame into every MP3; optional resize via Pillow |
| 15   | Fetch missing art online | iTunes → MusicBrainz/CAA → TheAudioDB → Discogs |

Run a specific step or steps with `--steps`:

```bash
python standardize.py --steps 5 ~/Music      # normalize chars only
python standardize.py --steps 15 ~/Music     # fetch missing art only
python standardize.py -n --steps 9,12 ~/Music  # dry run steps 9 and 12
```

### Import (`import_tracks.py`)

Copies tracks from a source directory into the library:

- Reads existing tags or infers them from filenames
- Normalizes and sanitizes all tag values
- Optionally converts FLAC/ALAC source files to MP3 via ffmpeg
- Shows an interactive preview of the proposed import before writing anything
- Prompts for any tags that couldn't be resolved automatically

### CD Ripping (`rip_cd.py`)

Rips a CD to FLAC using cdparanoia, then looks up metadata in order:

1. **MusicBrainz** — via `python-discid` + `musicbrainzngs` (most accurate; requires `libdiscid0`)
2. **gnudb (CDDB)** — via `cd-discid` or `python-discid` TOC data
3. **CD-Text** — reads metadata embedded on the disc via `cd-info`

Tags are written to the FLAC files and the output directory is passed directly to the import workflow.

### Artwork (`fetch_art.py`)

Multi-source artwork search with per-source rate limiting:

- **iTunes** — primary batch source
- **MusicBrainz / Cover Art Archive** — release-level art
- **TheAudioDB** — requires API key in settings
- **Discogs** — interactive-only (requires Discogs token); not used in batch fetch

In the browser, press `r` on an album to search and pick artwork interactively, or on an artist to batch-fetch for all albums under it. Press `x` to remove folder art, embedded art, or both.

Art can be saved as a folder `cover.jpg`, embedded as an APIC frame, or both, depending on the library's cover art mode setting.

### Sync (`sync_library.py`)

Interactive TUI for mirroring artist folders to a target device path. Select artists to include, then sync copies the album structure to the destination.

## Library Standard

All files are required to comply with the rules in `standard.md`, which covers:

- Directory structure: `Artist/YEAR - Album/NN. Artist - Title.mp3`
- Required ID3 tags: TPE1, TPE2, TIT2, TALB, TYER, TCON, TRCK
- ID3v2.3 exclusively (no ID3v2.4, no ID3v1)
- Track number zero-padding and `/total` format
- Character normalization and filesystem sanitization rules
- Cover image naming and placement
- Full description of every standardization step

## ID3 Conventions

- Always read with `ID3(path, translate=False)` — prevents mutagen auto-translating `TYER` → `TDRC`.
- Always write with `.save(path, v2_version=3, v1=0)` — ID3v2.3 only, no ID3v1.
- Album Artist is stored in `TPE2`. Legacy `TXXX:album artist` variants are migrated to `TPE2` during standardization.
- `TDRC` must not be present in compliant files; it is converted to `TYER` during standardization.

## Modules

| Module               | Role                                                         |
|----------------------|--------------------------------------------------------------|
| `tui.py`             | TUI entry point and screen-stack main loop                   |
| `audit.py`           | Read-only compliance scanner                                 |
| `standardize.py`     | 15-step library fixer                                        |
| `browse.py`          | Interactive library browser (also runs standalone)           |
| `import_tracks.py`   | Track import and tag normalization                           |
| `import_preview.py`  | Import preview screen (used by TUI and standalone)           |
| `sync_library.py`    | Device sync (also runs standalone)                           |
| `fetch_art.py`       | Multi-source artwork search and download                     |
| `rip_cd.py`          | CD ripping, disc ID, and metadata lookup                     |
| `convert_lossless.py`| FLAC/ALAC → MP3 conversion via ffmpeg                        |
| `settings.py`        | Per-library settings (JSON stored as `{root}/.mp3tools`)     |
| `termtext.py`        | Unicode-aware terminal layout (`cell_width`, `clip_cells`, …)|
