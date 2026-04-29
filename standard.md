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

- The folder name must **exactly match** the `Album Artist` (`TXXX:album artist`) tag value in its files, after character normalization and filesystem sanitization (see below).
- If `Album Artist` (`TXXX:album artist`) is missing, standardize sets it to the same value as `Artist` (TPE1).
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

No subfolders of any kind are permitted inside an album folder. If CD subfolders (`CD1`, `CD2`, …) exist, they must be merged into the parent using `merge_cds.py`.

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

All eight tags must be present on every MP3 file:

| Tag  | Field        | Example              |
|------|--------------|----------------------|
| TPE1 | Artist       | Some Artist          |
| TXXX:album artist | Album Artist | Some Artist          |
| TPE2 | Band / Album Artist compatibility | Some Artist |
| TIT2 | Title        | Some Track Title     |
| TALB | Album        | Some Album Title     |
| TYER | Year         | 1994                 |
| TCON | Genre        | Rock                 |
| TRCK | Track        | 01/9                 |

- Year is stored in `TYER` (ID3v2.3 only). `TDRC` (ID3v2.4 timestamp frame) must not be present.
- If a source file contains `TDRC` but no `TYER`, standardize converts the year value to `TYER` and removes `TDRC`.
- If `TYER` is absent and cannot be recovered from `TDRC`, standardize extracts the year from the album folder name (e.g. `1994 - Album Title` → `TYER: 1994`).
- Album Artist is stored as `TXXX:album artist` for DeaDBeeF and mirrored to `TPE2` for compatibility with players that use the de facto MP3 Album Artist field. Audit requires both fields to be present with the same value. Older `TXXX` spellings may be read as legacy fallbacks during standardization and are rewritten to the canonical form.

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
| Curly apostrophes | `'`   | `'` `'` `` ` ``                  |
| Curly quotes      | `"`   | `"` `"` `«` `»`                  |
| En/em dashes      | `-`   | `–` `—` `−`                      |
| Ellipsis          | `...` | `…`                               |
| Non-breaking spaces | ` ` | various Unicode space variants    |
| Zero-width space  | _(removed)_ | `​`                        |
| Multiplication sign | `x` | `×`                               |
| Trademark / Registered | _(removed)_ | `™` `®`              |
| Copyright         | `(C)` | `©`                               |
| Bullet / middle dot | `-` | `•` `·`                          |
| Prime / double prime | `'` / `"` | `′` `″`                  |

For the full replacement table, see `normalize_characters.py`.

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

---

## Tools

| Script           | Purpose                                           |
|------------------|---------------------------------------------------|
| `mp3tools.py`    | Interactive menu (launch this)                    |
| `audit.py`       | Scan and report all compliance issues (read-only) |
| `browse.py`      | Browse library in an interactive terminal tree    |
| `standardize.py` | Run all fixes in order; prompts for missing tags  |
