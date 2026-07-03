"""
Sync planning and execution tests. sync_library never reads tags — it mirrors
files by size+mtime — so plain byte files stand in for MP3s and the whole
suite runs without ffmpeg. A second tmp dir acts as the "device".
"""
import shutil

from sync_library import (
    AlbumInfo,
    ArtistInfo,
    combined_plan,
    compare_artist,
    make_plan,
    run_plan,
    synced_albums,
)

from conftest import snapshot


def write(path, size=100):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def build_artist(root, name="Artist", albums=("2019 - First", "2021 - Second"), tracks=2):
    artist = root / name
    for album in albums:
        for i in range(1, tracks + 1):
            write(artist / album / f"{i:02d}. {name} - Song {i}.mp3")
    return artist


# ── make_plan ─────────────────────────────────────────────────────────────────

def test_make_plan_new_artist_copies_everything(tmp_path):
    lib, dev = tmp_path / "lib", tmp_path / "dev"
    src = build_artist(lib)
    plan = make_plan(src, dev / "Artist")
    assert len(plan.copy_files) == 4
    assert plan.remove_files == [] and plan.remove_dirs == []
    assert plan.bytes_to_copy == 400
    # Destinations mirror the source layout under the device artist dir.
    dsts = {str(d.relative_to(dev / "Artist")) for _, d in plan.copy_files}
    assert "2019 - First/01. Artist - Song 1.mp3" in dsts


def test_make_plan_identical_is_noop(tmp_path):
    lib, dev = tmp_path / "lib", tmp_path / "dev"
    src = build_artist(lib)
    shutil.copytree(src, dev / "Artist")     # copy2 preserves mtimes
    plan = make_plan(src, dev / "Artist")
    assert plan.copy_files == [] and plan.remove_files == [] and plan.remove_dirs == []


def test_make_plan_size_change_recopies(tmp_path):
    lib, dev = tmp_path / "lib", tmp_path / "dev"
    src = build_artist(lib)
    shutil.copytree(src, dev / "Artist")
    changed = dev / "Artist" / "2019 - First" / "01. Artist - Song 1.mp3"
    changed.write_bytes(b"y" * 50)           # size differs → stale
    plan = make_plan(src, dev / "Artist")
    assert [d for _, d in plan.copy_files] == [changed]


def test_make_plan_removes_files_gone_from_library(tmp_path):
    lib, dev = tmp_path / "lib", tmp_path / "dev"
    src = build_artist(lib)
    shutil.copytree(src, dev / "Artist")
    stale_album = dev / "Artist" / "1999 - Deleted"
    write(stale_album / "01. Artist - Gone.mp3")
    plan = make_plan(src, dev / "Artist")
    assert plan.copy_files == []
    assert plan.remove_files == [stale_album / "01. Artist - Gone.mp3"]
    assert plan.remove_dirs == [stale_album]


# ── combined_plan ─────────────────────────────────────────────────────────────

def _artist_info(artist_dir, selected_albums):
    albums = [AlbumInfo(path=d, selected=(d.name in selected_albums))
              for d in sorted(artist_dir.iterdir()) if d.is_dir()]
    return ArtistInfo(path=artist_dir, albums=albums)


def test_combined_plan_whole_artist_mirrors(tmp_path):
    lib, dev = tmp_path / "lib", tmp_path / "dev"
    src = build_artist(lib)
    shutil.copytree(src, dev / "Artist")
    stale = dev / "Artist" / "1999 - Deleted"
    write(stale / "01. Artist - Gone.mp3")

    info = _artist_info(src, {"2019 - First", "2021 - Second"})   # all selected
    plan = combined_plan(lib, dev, [info])
    # Full mirror prunes the album that no longer exists in the library.
    assert stale / "01. Artist - Gone.mp3" in plan.remove_files


def test_combined_plan_partial_selection_leaves_device_extras(tmp_path):
    lib, dev = tmp_path / "lib", tmp_path / "dev"
    src = build_artist(lib)
    stale = dev / "Artist" / "1999 - Deleted"
    write(stale / "01. Artist - Gone.mp3")

    info = _artist_info(src, {"2019 - First"})    # partial selection
    plan = combined_plan(lib, dev, [info])
    copies = {str(d) for _, d in plan.copy_files}
    assert all("2019 - First" in c for c in copies) and len(copies) == 2
    # Unselected/unknown albums on the device are not touched.
    assert plan.remove_files == []


def test_combined_plan_skips_unloaded_and_unselected(tmp_path):
    lib, dev = tmp_path / "lib", tmp_path / "dev"
    src = build_artist(lib)
    unloaded = ArtistInfo(path=src, albums=None)          # never enumerated
    unselected = _artist_info(src, set())                 # nothing ticked
    plan = combined_plan(lib, dev, [unloaded, unselected])
    assert plan.copy_files == [] and plan.remove_files == []


def test_combined_plan_loose_tracks_artist(tmp_path):
    lib, dev = tmp_path / "lib", tmp_path / "dev"
    loose = lib / "Loose"
    write(loose / "01. Loose - Single.mp3")
    info = ArtistInfo(path=loose, albums=[], whole_selected=True)
    plan = combined_plan(lib, dev, [info])
    assert [d for _, d in plan.copy_files] == [dev / "Loose" / "01. Loose - Single.mp3"]


# ── run_plan ──────────────────────────────────────────────────────────────────

def test_run_plan_dry_run_touches_nothing(tmp_path):
    lib, dev = tmp_path / "lib", tmp_path / "dev"
    src = build_artist(lib)
    shutil.copytree(src, dev / "Artist")
    stale = dev / "Artist" / "1999 - Deleted"
    write(stale / "01. Artist - Gone.mp3")
    (lib / "Artist" / "2019 - First" / "03. Artist - New.mp3").write_bytes(b"z" * 10)

    plan = make_plan(src, dev / "Artist")
    before = snapshot(dev)
    counts = run_plan(plan, dry_run=True)
    assert snapshot(dev) == before
    # Dry run still reports the full would-do counts.
    assert counts == (1, 1, 1)


def test_run_plan_applies_and_counts(tmp_path):
    lib, dev = tmp_path / "lib", tmp_path / "dev"
    src = build_artist(lib)
    shutil.copytree(src, dev / "Artist")
    stale = dev / "Artist" / "1999 - Deleted"
    write(stale / "01. Artist - Gone.mp3")
    new = lib / "Artist" / "2019 - First" / "03. Artist - New.mp3"
    new.write_bytes(b"z" * 10)

    plan = make_plan(src, dev / "Artist")
    events = []
    copied, removed_files, removed_dirs = run_plan(
        plan, dry_run=False,
        on_progress=lambda action, name, *rest: events.append((action, name)))

    assert (copied, removed_files, removed_dirs) == (1, 1, 1)
    assert (dev / "Artist" / "2019 - First" / "03. Artist - New.mp3").read_bytes() == b"z" * 10
    assert not stale.exists()
    assert ("Delete", "01. Artist - Gone.mp3") in events
    assert any(a == "Copy" for a, _ in events)
    # Device now mirrors the library artist exactly.
    assert make_plan(src, dev / "Artist").copy_files == []


# ── synced_albums / compare_artist ────────────────────────────────────────────

def test_synced_albums(tmp_path):
    dev_artist = tmp_path / "Artist"
    write(dev_artist / "2019 - First" / "01. x.mp3")
    (dev_artist / "Empty Dir").mkdir()
    (dev_artist / "2020 - No Music" / "sub").mkdir(parents=True)
    assert synced_albums(dev_artist) == ["2019 - First"]
    assert synced_albums(tmp_path / "absent") == []


def test_compare_artist_states(tmp_path):
    lib, dev = tmp_path / "lib", tmp_path / "dev"
    src = build_artist(lib, albums=("2019 - First",), tracks=3)

    assert compare_artist(src, dev / "Artist") == "not on device"

    shutil.copytree(src, dev / "Artist")
    assert compare_artist(src, dev / "Artist") == "synced"

    dst = dev / "Artist" / "2019 - First"
    (dst / "01. Artist - Song 1.mp3").unlink()          # 1 missing
    (dst / "02. Artist - Song 2.mp3").write_bytes(b"y")  # 1 changed
    write(dst / "99. Artist - Bootleg.mp3")             # 1 extra
    assert compare_artist(src, dev / "Artist") == "1 missing, 1 changed, 1 extra"


def test_compare_artist_partial_vocab(tmp_path):
    lib, dev = tmp_path / "lib", tmp_path / "dev"
    src = build_artist(lib, albums=("2019 - First",), tracks=2)
    shutil.copytree(src, dev / "Artist")
    (dev / "Artist" / "2019 - First" / "01. Artist - Song 1.mp3").unlink()
    assert compare_artist(src, dev / "Artist") == "1 missing"
