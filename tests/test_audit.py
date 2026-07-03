"""
Audit integration tests (ffmpeg): a defect zoo where each library exhibits
exactly one known defect, and scan() must report exactly that issue.

The most important case is the fully clean album reporting zero issues —
false positives destroy trust in audit.
"""
import pytest
from mutagen.id3 import ID3, TPE2

import audit
import settings as settings_mod

from conftest import TINY_PNG, add_id3v1, embed_art, make_mp3, make_v24

pytestmark = pytest.mark.ffmpeg

ARTIST = "Clean Artist"
ALBUM = "Clean Album"
YEAR = "2024"


def clean_album(root, artist=ARTIST, album=ALBUM, year=YEAR, n=2, cover=True,
                folder_name=None):
    """A fully standard-compliant album; returns its folder."""
    folder = root / artist / (folder_name or f"{year} - {album}")
    titles = [f"Song {i}" for i in range(1, n + 1)]
    for i, title in enumerate(titles, 1):
        make_mp3(folder / f"{i:02d}. {artist} - {title}.mp3",
                 TIT2=title, TPE1=artist, TPE2=artist, TALB=album,
                 TYER=year, TRCK=f"{i:02d}/{n}", TCON="Rock")
    if cover:
        (folder / "cover.jpg").write_bytes(TINY_PNG)
    return folder


def scan_cats(root):
    """The set of all issue categories reported anywhere in the library."""
    cats = set()
    for _folder, album_issues, file_results in audit.scan(root):
        cats |= {i.cat for i in album_issues}
        for _path, _tags, issues in file_results:
            cats |= {i.cat for i in issues}
    return cats


# ── The false-positive guard ──────────────────────────────────────────────────

def test_clean_album_zero_issues(tmp_path):
    clean_album(tmp_path)
    assert scan_cats(tmp_path) == set()


# ── One defect per case ───────────────────────────────────────────────────────

def test_wrong_filename(tmp_path):
    folder = clean_album(tmp_path)
    (folder / f"01. {ARTIST} - Song 1.mp3").rename(folder / "track one.mp3")
    assert scan_cats(tmp_path) == {"FILENAME"}


def test_wrong_folder_year(tmp_path):
    clean_album(tmp_path, folder_name=f"1999 - {ALBUM}")
    assert scan_cats(tmp_path) == {"FOLDER_NAME"}


def test_unpadded_track_number(tmp_path):
    folder = clean_album(tmp_path)
    tags = ID3(folder / f"01. {ARTIST} - Song 1.mp3", translate=False)
    tags["TRCK"].text = ["1/2"]
    tags.save(v2_version=3, v1=0)
    assert scan_cats(tmp_path) == {"TRACK_PAD"}


def test_missing_tpe2(tmp_path):
    folder = clean_album(tmp_path)
    for mp3 in folder.glob("*.mp3"):
        tags = ID3(mp3, translate=False)
        tags.delall("TPE2")
        tags.save(mp3, v2_version=3, v1=0)
    assert scan_cats(tmp_path) == {"MISSING_TAG"}


def test_missing_genre(tmp_path):
    folder = clean_album(tmp_path)
    tags = ID3(folder / f"01. {ARTIST} - Song 1.mp3", translate=False)
    tags.delall("TCON")
    tags.save(v2_version=3, v1=0)
    assert scan_cats(tmp_path) == {"MISSING_TAG"}


def test_nonstandard_chars_in_title(tmp_path):
    folder = clean_album(tmp_path)
    # Filename carries the normalized form so only the tag defect is flagged.
    src = folder / f"01. {ARTIST} - Song 1.mp3"
    dst = folder / f"01. {ARTIST} - Don't Stop.mp3"
    src.rename(dst)
    tags = ID3(dst, translate=False)
    tags["TIT2"].text = ["Don’t Stop"]
    tags.save(v2_version=3, v1=0)
    assert scan_cats(tmp_path) == {"CHAR_NORM"}


def test_id3v1_present(tmp_path):
    folder = clean_album(tmp_path)
    add_id3v1(folder / f"01. {ARTIST} - Song 1.mp3")
    assert scan_cats(tmp_path) == {"ID3_V1"}


def test_id3v24_tags(tmp_path):
    folder = clean_album(tmp_path)
    make_v24(folder / f"01. {ARTIST} - Song 1.mp3")
    # v2.4 conversion turns TYER into TDRC, so three defects surface together:
    # the version itself, the relic TDRC frame, and the now-missing TYER.
    assert scan_cats(tmp_path) == {"ID3_VERSION", "RELIC_TAG", "MISSING_TAG"}


def test_unnormalized_year_value(tmp_path):
    folder = clean_album(tmp_path)
    for mp3 in folder.glob("*.mp3"):
        tags = ID3(mp3, translate=False)
        tags["TYER"].text = ["2024-05-01"]
        tags.save(mp3, v2_version=3, v1=0)
    assert scan_cats(tmp_path) == {"DATE_NORM"}


def test_missing_cover(tmp_path):
    clean_album(tmp_path, cover=False)
    assert scan_cats(tmp_path) == {"COVER"}


def test_multiple_covers(tmp_path):
    folder = clean_album(tmp_path)
    (folder / "cover.png").write_bytes(TINY_PNG)
    assert scan_cats(tmp_path) == {"COVER"}


def test_junk_extra_file(tmp_path):
    folder = clean_album(tmp_path)
    (folder / "rip log.txt").write_text("junk")
    assert scan_cats(tmp_path) == {"NON_MP3"}


def test_legacy_txxx_albumartist_flagged(tmp_path):
    from mutagen.id3 import TXXX
    folder = clean_album(tmp_path)
    tags = ID3(folder / f"01. {ARTIST} - Song 1.mp3", translate=False)
    tags.add(TXXX(encoding=3, desc="ALBUM ARTIST", text=ARTIST))
    tags.save(v2_version=3, v1=0)
    # TPE2 is intact, but the leftover legacy frame is a relic standardize
    # step 4 removes — audit must surface it.
    assert scan_cats(tmp_path) == {"ALBUM_ARTIST"}


def test_decomposed_unicode_title_flagged(tmp_path):
    folder = clean_album(tmp_path)
    src = folder / f"01. {ARTIST} - Song 1.mp3"
    dst = folder / f"01. {ARTIST} - Café.mp3"     # filename already NFC
    src.rename(dst)
    tags = ID3(dst, translate=False)
    from mutagen.id3 import TIT2
    tags["TIT2"] = TIT2(encoding=3, text="Café")  # decomposed é in the tag
    tags.save(v2_version=3, v1=0)
    assert scan_cats(tmp_path) == {"CHAR_NORM"}


def test_album_artist_varies_within_album(tmp_path):
    folder = clean_album(tmp_path)
    tags = ID3(folder / f"02. {ARTIST} - Song 2.mp3", translate=False)
    tags["TPE2"] = TPE2(encoding=3, text="Someone Else")
    tags.save(v2_version=3, v1=0)
    # The varying TPE2 also breaks that file's parent-folder match.
    assert scan_cats(tmp_path) == {"ALBUM_ARTIST"}


def test_artist_folder_mismatch(tmp_path):
    clean_album(tmp_path, artist="Wrong Folder")
    # Tag TPE2 says "Wrong Folder"... make tags disagree with the folder instead:
    for mp3 in (tmp_path / "Wrong Folder").rglob("*.mp3"):
        tags = ID3(mp3, translate=False)
        tags["TPE2"] = TPE2(encoding=3, text=ARTIST)
        tags.save(mp3, v2_version=3, v1=0)
    assert scan_cats(tmp_path) == {"ARTIST_FOLDER"}


def test_cd_subfolders_flagged_for_merge(tmp_path):
    parent = tmp_path / ARTIST / f"{YEAR} - {ALBUM}"
    for disc in (1, 2):
        make_mp3(parent / f"CD{disc}" / f"01. {ARTIST} - Song {disc}.mp3",
                 TIT2=f"Song {disc}", TPE1=ARTIST, TPE2=ARTIST, TALB=ALBUM,
                 TYER=YEAR, TRCK="01/1", TCON="Rock")
    (parent / "cover.jpg").write_bytes(TINY_PNG)
    assert scan_cats(tmp_path) == {"CD_MERGE"}


def test_nested_non_cd_music(tmp_path):
    folder = clean_album(tmp_path)
    make_mp3(folder / "Bonus" / f"01. {ARTIST} - Extra.mp3",
             TIT2="Extra", TPE1=ARTIST, TPE2=ARTIST, TALB=ALBUM,
             TYER=YEAR, TRCK="01/1", TCON="Rock")
    results = {f.name: [i.cat for i in issues]
               for f, issues, _ in audit.scan(tmp_path)}
    # The parent flags the stray subfolder; the subfolder is also scanned as
    # its own album (and complains about its cover/folder name).
    assert "NESTED_MUSIC" in results[f"{YEAR} - {ALBUM}"]
    assert "Bonus" in results


# ── Embed mode ────────────────────────────────────────────────────────────────

def test_embed_mode_requires_apic_not_folder_cover(tmp_path):
    folder = clean_album(tmp_path, cover=False)
    settings_mod.save(tmp_path, {"cover_art": "embed"})
    assert scan_cats(tmp_path) == {"COVER"}          # no APIC anywhere
    for mp3 in folder.glob("*.mp3"):
        embed_art(mp3)
    assert scan_cats(tmp_path) == set()              # no folder cover needed


# ── scan_json contract ────────────────────────────────────────────────────────

def test_scan_json_shape_and_totals(tmp_path):
    clean_album(tmp_path)                            # 2 clean files
    bad = clean_album(tmp_path, artist="Bad Artist", cover=False)
    (bad / f"01. Bad Artist - Song 1.mp3").rename(bad / "wrong.mp3")

    data = audit.scan_json(tmp_path)
    assert set(data) == {"root", "category_labels", "albums", "totals"}
    assert data["root"] == str(tmp_path)
    assert data["totals"] == {"albums": 2, "albums_with_issues": 1,
                              "files": 4, "files_with_issues": 1}

    assert len(data["albums"]) == 2
    for album in data["albums"]:
        assert set(album) == {"path", "name", "album_issues", "files"}
        for f in album["files"]:
            assert set(f) == {"path", "name", "issues"}
            for issue in f["issues"]:
                assert set(issue) == {"cat", "label", "msg"}
                assert issue["label"] == data["category_labels"][issue["cat"]]

    bad_album = next(a for a in data["albums"] if "Bad Artist" in a["path"])
    assert {i["cat"] for i in bad_album["album_issues"]} == {"COVER"}
    file_cats = {i["cat"] for f in bad_album["files"] for i in f["issues"]}
    assert file_cats == {"FILENAME"}
