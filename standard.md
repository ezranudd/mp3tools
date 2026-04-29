# Music Library Style Standard

## Directory Structure

```
root/
└── Artist Name/
    └── YEAR - Album Title/
        ├── 01. Artist Name - Track Title.mp3
        ├── 02. Artist Name - Track Title.mp3
        └── cover.jpg
```

- The root may contain multiple artist folders.
- Each artist folder contains one or more album folders.
- Each album folder contains only MP3 files and one cover image.
- No other nesting is allowed.

---

## Artist Folder

- The folder name must **exactly match** the `Artist` (TPE1) tag value in its files, after character normalization and filesystem sanitization (see below).
- Example: files with `Artist = Some Artist` → folder named `Some Artist`

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
- Example: `01. Some Artist - Some Track Title.mp3`

---

## ID3 Tag Version

- All MP3 files must use **ID3v2.3** exclusively.
- ID3v2.4 tags are not permitted (incompatible with many players and hardware).
- ID3v1 tags must not be present (legacy format; causes "ID3v1 | ID3v2.3" display in players).
- When writing tags, always save with `v2_version=3, v1=0`.

---

## Required ID3 Tags

All six tags must be present on every MP3 file:

| Tag  | Field  | Example              |
|------|--------|----------------------|
| TPE1 | Artist | Some Artist          |
| TIT2 | Title  | Some Track Title     |
| TALB | Album  | Some Album Title     |
| TYER | Year   | 1994                 |
| TCON | Genre  | Rock                 |
| TRCK | Track  | 01/10                |

- Year is stored in `TYER` (ID3v2.3). `TDRC` (ID3v2.4) is also accepted if present, but must contain the same normalized value.
- If both `TYER` and `TDRC` are present, both must be normalized.

---

## Track Numbers

- Format: `NN/TT` where `NN` is the track number and `TT` is the total track count.
- Both are zero-padded to the same width:
  - **2 digits** when the album has fewer than 100 tracks (e.g. `01/10`, `09/10`, `10/10`)
  - **3 digits** when the album has 100 or more tracks (e.g. `001/120`)
- The total track count (`/TT`) is required.

---

## Year / Date Tags

- Must contain **only a 4-digit year** (`1900`–`2099`).
- Extended formats are not allowed: `1999-01-01` → `1999`, `1999-05` → `1999`.
- Applies to both `TYER` and `TDRC` if either is present.

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
