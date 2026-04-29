# MP3 Tools

A set of Python scripts for managing and standardizing a local MP3 music library.

## Requirements

- Python 3.10+
- `mutagen` — ID3 tag reading/writing (`pip install mutagen`)
- `Pillow` — placeholder cover generation (`pip install Pillow`)
- `ffmpeg` — lossless audio conversion (system package: `sudo apt install ffmpeg`)

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

## Scripts

| Script             | Purpose                                              |
|--------------------|------------------------------------------------------|
| `mp3tools.py`      | Interactive menu (start here)                        |
| `audit.py`         | Scan library and report compliance issues (read-only)|
| `browse.py`        | Browse and edit tags in an interactive terminal UI   |
| `standardize.py`   | Run all fixes in sequence                            |
| `import_tracks.py` | Copy and standardize tracks from another directory   |

## Library Standard

See `standard.md` for the full style specification covering folder structure, filename format, required ID3 tags, and tag version requirements.
