# Music Library Style Standard

## Directory Structure

```
root/
└── Album Artist Name/
    └── YEAR - Album Title/
        ├── 01. Artist Name - Track Title.mp3
        ├── 02. Artist Name - Track Title.mp3
        └── cover.jpg
```

- The root may contain multiple album artist folders.
- Each album artist folder contains one or more album folders.
- Each album folder contains only MP3 files and one cover image.
- No other nesting is allowed.

---

## Album Artist Folder

- The folder name must **exactly match** the `Album Artist` (`TPE2`) tag value in its files, after character normalization and filesystem sanitization (see below).
- If `Album Artist` is missing, standardize sets it to the same value as `Artist` (TPE1).
- Album Artist must be constant across every MP3 in the album folder.
- Example: files with `Album Artist = Some Artist` → folder named `Some Artist`

---

## Album Folder

Name format: `YEAR - Album Title`

- `YEAR` is the 4-digit year from the files' `Year` tag.
- `Album Title` is the `Album` (TALB) tag value, after normalization and sanitization.
- Separator is ` - ` (space, hyphen, space).
- Example: `1994 - Some Album Title`

The album folder must contain **only**:
- MP3 files
- One cover image (see Cover Image below)

No subfolders of any kind are permitted inside an album folder. If CD subfolders (`CD1`, `CD2`, …) exist, they are merged into the parent by step 1.

---

## MP3 Filename

Format: `TT. Artist Name - Track Title.mp3`

- `TT` is the zero-padded track number (see Track Numbers below).
- Separator between number and name is `. ` (period, space).
- Separator between artist and title is ` - ` (space, hyphen, space).
- Artist and title are taken from tags, after normalization and sanitization.
- The filename artist is the per-track `Artist` (TPE1), not the album-level Album Artist.
- Example: `01. Some Artist - Some Track Title.mp3`

---

## ID3 Tag Version

- All MP3 files must use **ID3v2.3** exclusively.
- ID3v2.4 tags are not permitted (incompatible with many players and hardware).
- ID3v1 tags must not be present (legacy format; causes "ID3v1 | ID3v2.3" display in players).
- When writing tags, always save with `v2_version=3, v1=0`.
- When reading tags through mutagen, loads must use `translate=False` so `TYER` is not auto-translated to `TDRC` in memory.

---

## Required ID3 Tags

All seven tags must be present on every MP3 file:

| Tag  | Field        | Example              |
|------|--------------|----------------------|
| TPE1 | Artist       | Some Artist          |
| TPE2 | Album Artist | Some Artist          |
| TIT2 | Title        | Some Track Title     |
| TALB | Album        | Some Album Title     |
| TYER | Year         | 1994                 |
| TCON | Genre        | Rock                 |
| TRCK | Track        | 01/9                 |

- Year is stored in `TYER` (ID3v2.3 only). `TDRC` (ID3v2.4 timestamp frame) must not be present.
- If a source file contains `TDRC` but no `TYER`, standardize converts the year value to `TYER` and removes `TDRC`.
- If `TYER` is absent and cannot be recovered from `TDRC`, standardize extracts the year from the album folder name (e.g. `1994 - Album Title` → `TYER: 1994`).
- Album Artist is stored in `TPE2`. Legacy `TXXX:album artist` and related spellings are read as fallbacks during standardization and migrated to `TPE2`.

---

## Track Numbers

- Format: `NN/T` where `NN` is the zero-padded track number and `T` is the total track count.
- The track number is zero-padded; the total is **not** padded:
  - **2 digits** for the track number when the album has fewer than 100 tracks (e.g. `01/9`, `09/9`, `10/10`)
  - **3 digits** for the track number when the album has 100 or more tracks (e.g. `001/120`)
- The total track count (`/T`) is required.

---

## Year / Date Tags

- Must contain **only a 4-digit year** (`1900`–`2099`).
- Extended formats are not allowed: `1999-01-01` → `1999`, `1999-05` → `1999`.
- Only `TYER` is permitted. `TDRC` is an ID3v2.4 frame and must not be present in a compliant file.
- When `TYER` is absent, standardize attempts to recover it in order: (1) from `TDRC` if present, (2) from the album folder name.

---

## Character Normalization

All tag values and filenames must use standard ASCII punctuation. The following substitutions are required:

| Replace           | With  | Examples                          |
|-------------------|-------|-----------------------------------|
| Curly apostrophes | `'`   | `'` `'` `‚` `‛` `` ` ``         |
| Curly quotes      | `"`   | `"` `"` `„` `‟` `«` `»`         |
| En/em dashes      | `-`   | `–` `—` `−` `‐` `‑` `⁃`        |
| Ellipsis          | `...` | `…`                               |
| Non-breaking spaces | ` ` | various Unicode space variants    |
| Zero-width space  | _(removed)_ | `​`                        |
| Multiplication sign | `x` | `×`                               |
| Fraction slashes  | `/`   | `⁄` `∕`                          |
| Numero sign       | `No.` | `№`                               |
| Sound recording   | `(P)` | `℗`                               |
| Degree Celsius/Fahrenheit | `C` / `F` | `℃` `℉`             |
| Trademark / Registered | _(removed)_ | `™` `®`              |
| Copyright         | `(C)` | `©`                               |
| Bullet / middle dot | `-` | `•` `·`                          |
| Dagger            | `+` / `++` | `†` `‡`                     |
| Prime / double prime / triple | `'` / `"` / `'''` | `′` `″` `‴` |
| Tironian et       | `&`   | `⁊`                               |

This table is duplicated in `standardize.py`, `audit.py`, `browse.py`, and `import_tracks.py`. Keep all four in sync when modifying.

---

## Filesystem Sanitization

After character normalization, the following substitutions are applied to make names safe as filenames and folder names:

| Character | Replaced with |
|-----------|---------------|
| `/`       | `-`           |
| `\`       | `-`           |
| `:`       | ` -`          |
| `*`       | _(removed)_   |
| `?`       | _(removed)_   |
| `"`       | `'`           |
| `<` `>`   | _(removed)_   |
| `\|`      | `-`           |

Trailing periods and spaces are also stripped from folder and filenames.

---

## Cover Image

- Exactly **one** cover image per album folder.
- The file stem must be exactly `cover` (case-insensitive): `cover.jpg`, `cover.png`, etc.
- Accepted extensions: `.jpg`, `.jpeg`, `.png`, `.gif`, `.webp`, `.bmp`
- All other image files (e.g. `Front.jpg`, `Back.jpg`, `folder.jpg`, `CD.jpg`) must be removed.
- If multiple images exist, all but one must be removed and the remaining one renamed to `cover.*`.
- Missing cover art can be fetched from enabled online sources through the browser `r` command or standardize step 15.

---

## Standardization Steps

`standardize.py` runs the following steps in order. Steps 0, 5a, 12a, 14, and 15 are conditional (see notes per step).

### Step 0 — Convert lossless files _(conditional)_

Converts any FLAC or ALAC files found under the root to MP3 via ffmpeg. Only runs when `--steps` is not specified or explicitly includes `0`.

### Step 1 — Merge disc subfolders

Finds album folders that contain subfolders with MP3 files (e.g. `CD1`, `CD2`). Merges all MP3s into the parent folder, renumbering tracks sequentially across discs in disc-number order. Sets `TALB` to the most common album title across all subfolders and updates `TRCK` to reflect the merged sequence. Copies a cover image from the first subfolder that has one. Deletes the empty subfolders afterward (skips any subfolder that still contains non-image files).

### Step 2 — Fix missing tags

Groups files by album folder. For each folder that has any missing required tags:

- **YEAR**: auto-filled from the album folder name if it matches `DDDD - …`; falls back to `1900`.
- **Album Artist**: auto-derived from `Artist` (TPE1).
- **Album, Genre**: prompted interactively, applied to all files in the folder.
- **Title**: prompted per file; if the filename contains ` - `, the portion after it is offered as a suggestion.

In the TUI, text prompts are answered inline. In CLI mode, `input()` is used.

### Step 3 — Enforce ID3v2.3

For every MP3:

1. Detects and removes the ID3v1 tail (last 128 bytes starting with `TAG`).
2. Downgrades ID3v2.4 headers to ID3v2.3 via mutagen's `update_to_v23()`.
3. Converts any `TDRC` frame to `TYER` (extracting the 4-digit year) and deletes `TDRC`.
4. If `TYER` is still absent, fills it from the album folder name.

### Step 4 — Strip extraneous tags

Removes every ID3 frame except: `TPE1`, `TPE2`, `TIT2`, `TALB`, `TYER`, `TCON`, `TRCK`. When cover art mode is `embed` or `both`, `APIC` frames are also kept.

Before stripping, if an `APIC` frame is present and the album folder has no cover image file, the embedded art is extracted and saved as `cover.jpg` (or `cover.png` for PNG data).

Also migrates any legacy `TXXX:album artist` (and variant spellings) into `TPE2`, removing the `TXXX` frame.

### Step 5 — Normalize special characters

Applies the character replacement table (see Character Normalization above) to:

- Every text frame in every MP3's tags.
- Every MP3 filename.
- Every album and artist folder name (processed deepest-first so parents are renamed after their children).

### Step 5a — Replace square brackets _(conditional)_

Replaces `[` → `(` and `]` → `)` in `TALB` and `TIT2` tags. Runs immediately before step 6 only when `replace_brackets_with_parentheses` is enabled in library settings.

### Step 6 — Normalize year tags

Extracts a 4-digit year from any `TYER` value that contains more than 4 digits (e.g. `1999-01-01` → `1999`, `1999-05` → `1999`) and rewrites the tag.

### Step 7 — Zero-pad track numbers

Pads the numeric part of each `TRCK` value to 2 digits, or 3 digits for albums with 100 or more tracks. Preserves any `/total` suffix already present.

### Step 8 — Set total track counts

Counts the MP3 files in each album folder and writes `NN/total` into every `TRCK` tag in that folder, applying the same padding width as step 7.

### Step 9 — Rename album folders

Renames each album folder to `YEAR - Album Title`, derived from the most common `TYER` and `TALB` values across the folder's MP3s, after sanitization. Skips folders where either value is unavailable. Skips (and reports an error) if the target name already exists.

### Step 10 — Deduplicate album titles

For each artist folder, groups its album subfolders by their dominant `TALB` value. If two or more folders share the same title, the second and subsequent folders are retagged (`Title (2)`, `Title (3)`, …) and their folder names updated accordingly.

### Step 11 — Rename album artist folders

Computes the correct folder name for each artist folder from the dominant `Album Artist` tag across all its MP3s (with normalization and sanitization applied). When the folder name and tag value differ, prompts interactively:

- **[R]etag** — rewrite Album Artist on all files to match the folder name.
- **[M]ove/rename** — rename the folder to match the tag value.
- **[S]kip** — leave as-is.

Also checks each album subfolder's dominant Album Artist against the containing artist folder. When they differ:

- If the album has mixed album artists but the dominant matches the folder, prompts to retag the minority files.
- If the album's dominant Album Artist doesn't match the folder at all, prompts to retag or move the entire album subfolder to the correct artist folder.

### Step 12a — Enforce Artist = Album Artist _(conditional)_

Overwrites `TPE1` (Artist) with the Album Artist value on every track. Runs immediately before step 12 only when `enforce_artist_equals_album_artist` is enabled in library settings.

### Step 12 — Rename MP3 files

Renames each MP3 to `NN. Artist - Title.mp3` using the track number (from `TRCK`), `TPE1`, and `TIT2`, after sanitization. Skips files missing any of the three required fields, or with an unparseable track number.

### Step 13 — Clean non-MP3 files and cover images

For each album folder:

- If a `cover.*` image exists, it is kept. Additional `cover.*` files and all other image files are deleted automatically.
- If no `cover.*` exists but other image files do, the first is renamed to `cover.*` and the rest are deleted.
- Non-MP3, non-image files are listed and deletion is confirmed interactively before proceeding.
- Folders with no cover image are reported (unless cover art mode is `embed`, in which case step 14 handles art).

### Step 14 — Embed cover art _(conditional)_

For each album folder that has a cover image file, reads the image, optionally resizes it (via PIL, if installed) to `cover_art_embed_size` pixels, and writes it as an `APIC` frame into every MP3 in the folder. Skips tracks that already have the identical image embedded. Only runs when `cover_art` is `embed` or `both`. When `cover_art` is `embed` (not `both`), the cover file is deleted after all tracks in the folder are embedded successfully.

### Step 15 — Fetch missing album art online _(conditional)_

For each album folder that still lacks art (per the active cover art mode), queries enabled online sources in order: iTunes, MusicBrainz/Cover Art Archive, TheAudioDB, Discogs. The highest-scoring result is applied automatically if it meets the confidence threshold; low-confidence matches are skipped and reported. Only runs when `fetch_art_online` is enabled in library settings, or when explicitly requested via `--steps 15`.

---

## Optional Settings

These are stored per-library in `{library_root}/.mp3tools` and control conditional steps:

| Setting | Effect |
|---------|--------|
| `cover_art` | `folder` (default), `embed`, or `both` — controls steps 4, 13, 14 |
| `cover_art_embed_size` | Max pixel dimension for embedded art (0 = no resize) |
| `fetch_art_online` | Enable step 15 |
| `enforce_artist_equals_album_artist` | Enable step 12a |
| `replace_brackets_with_parentheses` | Enable step 5a |

---

## Tools

| Command / Script     | Purpose                                                   |
|----------------------|-----------------------------------------------------------|
| `mp3tui` / `tui.py`  | TUI — primary entry point; browse, audit, standardize, import, sync, rip |
| `audit.py`           | Scan and report all compliance issues (read-only)         |
| `standardize.py`     | Run all fixes in order; prompts for missing tags          |
| `browse.py`          | Browse library in an interactive terminal tree (standalone) |
| `import_tracks.py`   | Copy and tag tracks from a source directory               |
| `sync_library.py`    | Mirror selected artist folders to a device path           |
| `fetch_art.py`       | Multi-source album artwork search and download            |
| `rip_cd.py`          | Rip a CD to FLAC with MusicBrainz / gnudb / CD-Text tags |
| `convert_lossless.py`| FLAC/ALAC → MP3 conversion via ffmpeg                    |
| `settings.py`        | Load and save per-library settings                        |
