# MP3 Tools

> **Disclaimer:** This software is intended for managing music you own legally. Do not use it to copy, distribute, or rip media in violation of copyright law or any applicable terms of service.

A web-based music library manager for maintaining a locally standardized MP3 collection. Covers everything from tagging and renaming to CD ripping and device sync, all from the browser.

## Installation

```bash
pip install -e . --break-system-packages
```

This registers the `mp3tools` command, which launches the web server for a library:

```bash
mp3tools ~/Music                  # or: mp3tools-web ~/Music
# → http://127.0.0.1:8765
```

The headless command-line tools also run directly from the project directory:

```bash
python audit.py ~/Music
python standardize.py ~/Music
python standardize.py -n ~/Music   # dry run
python import_tracks.py ~/Downloads/NewAlbum ~/Music
```

## Requirements

### Required

| Package              | Purpose                          | Install                        |
|----------------------|----------------------------------|--------------------------------|
| `mutagen`            | ID3 tag reading/writing          | `pip install mutagen`          |
| `fastapi` + `uvicorn`| Web server                       | installed by `pip install -e .`|

### Optional

| Package / Tool          | Purpose                                            | Install                                              |
|-------------------------|----------------------------------------------------|------------------------------------------------------|
| `Pillow`                | Resize cover art before embedding                  | `pip install Pillow`                                 |
| `musicbrainzngs`        | MusicBrainz metadata lookup for CD ripping         | `pip install musicbrainzngs`                         |
| `discid` + `libdiscid0` | MusicBrainz disc ID from CD (preferred)            | `pip install discid` + `sudo apt install libdiscid0` |
| `cdparanoia`            | CD audio extraction                                | `sudo apt install cdparanoia`                        |
| `ffmpeg`                | WAV→FLAC and lossless decode for MP3 conversion    | `sudo apt install ffmpeg`                            |
| `lame`                  | Gapless MP3 encoding (correct encoder delay/padding) | `sudo apt install lame`                            |
| `cd-discid`             | CDDB disc ID fallback (when discid unavailable)    | `sudo apt install cd-discid`                         |
| `cd-info`               | CD-Text reading                                    | `sudo apt install libcdio-utils`                     |
| `pywebview`             | `--desktop` native-window mode                     | `pip install -e ".[desktop]" --break-system-packages`|

## Web UI

The browser interface is the primary way to use mp3tools. It is a thin
[FastAPI](https://fastapi.tiangolo.com/) shell over the shared library modules —
no business logic is duplicated.

```bash
mp3tools ~/Music                  # or: python server.py ~/Music
# → http://127.0.0.1:8765
```

The left nav switches between views:

- **Browse** — the library by **Artists**, **Genres**, or **Albums**, with a
  built-in gapless player (plus an opt-in gapless streaming mode for phones,
  under the header gear). Flip the header toggle to **Edit** mode to change
  anything in place: track titles/artists and album title/artist/year/genre
  auto-save as you edit, drag tracks to reorder (files are renumbered on disk),
  delete tracks or whole albums, and click the cover to search online sources,
  upload an image, or remove the art.
- **Devices** — owner-only: browsers currently connected to the server, what
  each is playing, and a connection history.
- **Access** — owner-only (remote mode): set the shared access password and
  approve, rename, or block the devices that have logged in.
- **Audit** — read-only compliance scan, grouped by album with category labels.
- **Standardize** — runs the full pipeline using your saved Settings. Interactive
  steps (fill missing tags, confirm deletions, choose lossless bitrate) prompt
  right in the browser.
- **Import** — copy tracks from a dragged-in folder (or a server-side path) into
  the library; review an editable preview (tags, cover art, lossless bitrate),
  then import.
- **Import CD** — rip the disc in the server's optical drive to FLAC, look up
  metadata (MusicBrainz → gnudb → CD-Text), then review the same editable import
  preview before the tracks land in the library.
- **Sync** — mirror selected artists/albums to a device: pick an auto-detected
  device, tick what to sync (tri-state per artist, expand for individual albums),
  preview the plan (files/bytes to copy & delete vs. free space), then run it.
- **Settings** — edit every option (cover-art mode, preserve flags, art sources,
  API keys) and save to `{root}/.mp3tools`.

Standardize, Import, Import CD and Sync run as background **jobs** with a live
log; the browser polls each job and surfaces its prompts as dialogs. Only one
operation runs at a time. Tag rules (ID3v2.3, `TPE2` album-artist, etc.) are
enforced by the shared modules.

Pass `--host`/`--port` to change the bind address, or `--desktop` (requires the
`desktop` extra) to open in a native window instead of a browser tab.

### Sharing on the local network (read-only)

```bash
mp3tools ~/Music --lan
# → http://127.0.0.1:8765            (this machine: full access)
# → http://192.168.1.50:8765         (local network: read-only browse + playback)
```

`--lan` binds `0.0.0.0` so other computers on your network can open the printed
LAN URL in a browser to **browse the library and play music**. Access is decided
by client IP: requests from the machine running the server (loopback) get the
full UI; every other device is a **read-only guest** — no audit, standardize,
import, sync, tag/art editing, or settings changes (the server returns `403` and
the guest UI hides those controls). There is no login. Only use `--lan` on a
network you trust. For authenticated internet access behind a TLS reverse proxy,
see `--remote` and the `Caddyfile`.

> **Heads-up:** edits, standardize, import, rip and sync all write to disk. Point
> the server at a copy of your library if you want to experiment safely.

## Features

The browser views above are backed by these modules, each of which also has a
headless command-line entry point (except where noted):

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
| 0    | Convert lossless files | FLAC/ALAC → gapless MP3 (ffmpeg decode → lame encode; falls back to ffmpeg if lame is missing) |
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
- Optionally converts FLAC/ALAC source files to gapless MP3 (ffmpeg decode → lame encode)
- In the browser, shows an editable preview (tags, cover art, lossless bitrate)
  before writing; on the CLI, prints a summary and proceeds non-interactively

### CD Ripping (`rip_cd.py`)

Rips a CD to FLAC using cdparanoia, then looks up metadata in order:

1. **MusicBrainz** — via `python-discid` + `musicbrainzngs` (most accurate; requires `libdiscid0`)
2. **gnudb (CDDB)** — via `cd-discid` or `python-discid` TOC data
3. **CD-Text** — reads metadata embedded on the disc via `cd-info`

Tags are written to the FLAC files and the output is fed straight into the import
workflow. In the web UI this is the **Import CD** view: the server (which owns the
optical drive) rips locally, then presents the editable import preview.

### Artwork (`fetch_art.py`)

Multi-source artwork search with per-source rate limiting:

- **iTunes** — primary batch source
- **MusicBrainz / Cover Art Archive** — release-level art
- **TheAudioDB** — requires API key in settings
- **Discogs** — interactive-only (requires Discogs token); not used in batch fetch

In the browser, use **Find artwork** on an album to search and pick artwork, or on
an artist to batch-fetch for all albums under it, and **Remove art** to delete
folder art, embedded art, or both.

Art can be saved as a folder `cover.jpg`, embedded as an APIC frame, or both, depending on the library's cover art mode setting.

> **Artwork & third-party APIs:** Album art is fetched on demand from third-party
> services (iTunes, MusicBrainz / Cover Art Archive, TheAudioDB, Discogs) and saved
> only to your own machine for your own files. Artwork is copyrighted by its
> respective owners. You are responsible for complying with each provider's terms
> of service and with applicable copyright law. TheAudioDB and Discogs require your
> own API key/token (configured in Settings); none are bundled with this software.
> The tool rate-limits requests per source to stay within those providers' usage
> guidelines.

### Sync (`sync_library.py`)

Mirrors selected artist folders to a target device path. Auto-detects mounted
devices, builds a copy/delete plan against the device, and applies it. Driven
from the **Sync** view.

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
| `server.py`          | FastAPI web server (`mp3tools` entry point)                  |
| `webjobs.py`         | Background job runner (standardize / import / rip / sync)    |
| `webauth.py`         | Remote-access auth: password, device whitelist, sessions     |
| `album_stream.py`    | Gapless album streaming: whole-album WAV + per-track manifest|
| `audit.py`           | Read-only compliance scanner                                 |
| `standardize.py`     | 15-step library fixer                                        |
| `browse.py`          | Library tree, tag I/O, and edit logic (web Browse core)      |
| `import_tracks.py`   | Track import and tag normalization                           |
| `sync_library.py`    | Device detection, sync planning, and mirroring               |
| `fetch_art.py`       | Multi-source artwork search and download                     |
| `rip_cd.py`          | CD ripping, disc ID, and metadata lookup                     |
| `convert_lossless.py`| FLAC/ALAC → gapless MP3 (ffmpeg decode → lame encode)        |
| `settings.py`        | Per-library settings (JSON stored as `{root}/.mp3tools`)     |
| `chars.py`           | Shared character-normalization table                         |

## Tests

```bash
pytest test_server.py
```

Endpoint tests for the web server; they build a tiny real library in a temp
directory, so `ffmpeg` must be installed (tests are skipped without it).
