# MP3 Tools

A set of Python scripts for managing and standardizing a local MP3 music library.

## Requirements

- Python 3.10+
- `mutagen` — ID3 tag reading/writing (`pip install mutagen`)
- `Pillow` — placeholder cover generation (`pip install Pillow`)
- `ffmpeg` — lossless audio conversion (system package: `sudo apt install ffmpeg`)

## ID3 Handling

- The application writes MP3 tags as `ID3v2.3` only with `v2_version=3, v1=0`.
- All ID3 reads must use `translate=False` so mutagen does not auto-translate `TYER` into `TDRC` in memory.
- `TYER` is the only supported year frame in compliant library files. `TDRC` is treated as a source/input relic to be converted and removed.

## Usage

Launch the interactive menu:

```
python mp3tools.py
```

Or run individual scripts directly:

```
python audit.py ~/Music
python standardize.py ~/Music
python standardize.py -n ~/Music   # dry run
```

## Album Art

- In `browse.py`, press `r` on an album to search enabled artwork sources, pick a result, and apply it according to the library cover-art setting.
- Press `r` on an artist to fetch the first confident non-Discogs artwork result for each album under that artist.
- Press `x` on an album or track to remove folder art, embedded art, or both from that album.
- In Settings, enable/disable artwork sources. Batch order is iTunes, MusicBrainz/Cover Art Archive, then TheAudioDB; Discogs is interactive-only. TheAudioDB needs an API key, and Discogs image results need a Discogs token.
- In Settings, enable “Fetch missing art during Standardize” to make `standardize.py` fetch art for albums missing the configured folder and/or embedded artwork.
- You can also run just the fetch step with `python standardize.py --steps 15 ~/Music`; use `-n` first for a dry-run preview.

## Artist Enforcement

- In Settings, enable “Enforce Artist = Album Artist” to make `standardize.py` rewrite each track artist tag from its album artist tag before MP3 filenames are generated.

## Scripts

| Script             | Purpose                                              |
|--------------------|------------------------------------------------------|
| `mp3tools.py`      | Interactive menu (start here)                        |
| `audit.py`         | Scan library and report compliance issues (read-only)|
| `browse.py`        | Browse and edit tags in an interactive terminal UI   |
| `standardize.py`   | Run all fixes in sequence                            |
| `import_tracks.py` | Copy and standardize tracks from another directory   |
| `sync_library.py`  | Sync selected artist folders to a device             |

## Library Standard

See `standard.md` for the full style specification covering folder structure, filename format, required ID3 tags, and tag version requirements.
