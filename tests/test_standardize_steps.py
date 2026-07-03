"""
Standardize step tests (ffmpeg) — the destructive core of the project.

Every step gets three angles:
  fix          — a library with the defect is corrected and the stats match
  dry-run      — dry_run=True leaves every byte untouched but still reports
  idempotency  — a second real run changes nothing (snapshot-stable)

Steps that can prompt always get an injected callback — the get_input
fallback calls sys.exit(0) on EOF, which would kill the test run.
"""
import shutil

import pytest
from mutagen.id3 import (
    APIC, COMM, ID3, TALB, TIT2, TPE1, TPE2, TPOS, TRCK, TXXX, TYER,
)

import standardize as st
from convert_lossless import step_convert_lossless

from conftest import (
    TINY_PNG, add_id3v1, embed_art, make_flac, make_mp3, make_v24, snapshot,
)

pytestmark = pytest.mark.ffmpeg

DEFAULTS = {"TPE1": "Artist", "TPE2": "Artist", "TALB": "Album",
            "TYER": "2020", "TCON": "Rock"}


def mk(path, **overrides):
    """make_mp3 with standard defaults; an explicit None drops that frame."""
    frames = {**DEFAULTS, **overrides}
    make_mp3(path, **{k: v for k, v in frames.items() if v is not None})


def tags_of(path):
    return ID3(path, translate=False)


def no_prompt(prompt):
    raise AssertionError(f"unexpected prompt: {prompt!r}")


def dry_run_untouched(step, root, **kwargs):
    """Run with dry_run=True; assert zero filesystem effect; return stats."""
    before = snapshot(root)
    stats = step(root, True, **kwargs)
    assert snapshot(root) == before, "dry run must not touch any file"
    return stats


def second_run_stable(step, root, **kwargs):
    """After one real run, a second must be a byte-level no-op."""
    snap = snapshot(root)
    stats = step(root, False, **kwargs)
    assert snapshot(root) == snap, "second run must not touch any file"
    return stats


def bystander(root):
    """A fully compliant album no step should ever touch. Returns its folder."""
    folder = root / "Bystander" / "2001 - Untouched"
    mk(folder / "01. Bystander - Calm.mp3", TPE1="Bystander", TPE2="Bystander",
       TALB="Untouched", TYER="2001", TIT2="Calm", TRCK="01/1")
    (folder / "cover.jpg").write_bytes(TINY_PNG)
    return folder


# ── Step 1: merge_subfolders ──────────────────────────────────────────────────

def _disc_library(root):
    album = root / "Artist" / "2010 - Live"
    mk(album / "CD1" / "01. Artist - A.mp3", TALB="Live", TYER="2010",
       TIT2="A", TRCK="1/2")
    mk(album / "CD1" / "02. Artist - B.mp3", TALB="Live", TYER="2010",
       TIT2="B", TRCK="2/2")
    mk(album / "CD2" / "01. Artist - C.mp3", TALB="Live", TYER="2010",
       TIT2="C", TRCK="1/1")
    (album / "CD1" / "cover.jpg").write_bytes(TINY_PNG)
    return album


def test_merge_subfolders_fix(tmp_path):
    album = _disc_library(tmp_path)
    safe = bystander(tmp_path)
    safe_before = snapshot(safe)

    stats = st.step_merge_subfolders(tmp_path, False)

    assert stats["albums"] == 1 and stats["moved"] == 3 and stats["errors"] == 0
    assert not (album / "CD1").exists() and not (album / "CD2").exists()
    names = sorted(p.name for p in album.glob("*.mp3"))
    assert names == ["01. Artist - A.mp3", "02. Artist - B.mp3", "03. Artist - C.mp3"]
    # Renumbered as one sequence (totals are unpadded here; step 8 pads later).
    assert str(tags_of(album / "01. Artist - A.mp3")["TRCK"]) == "1/3"
    assert str(tags_of(album / "03. Artist - C.mp3")["TRCK"]) == "3/3"
    # Cover migrated out of the disc folder.
    assert (album / "cover.jpg").read_bytes() == TINY_PNG
    assert snapshot(safe) == safe_before


def test_merge_subfolders_preserve_tpos(tmp_path):
    album = _disc_library(tmp_path)
    st.step_merge_subfolders(tmp_path, False, preserve_tpos=True)
    a = tags_of(album / "01. Artist - A.mp3")
    c = tags_of(album / "01. Artist - C.mp3")
    assert str(a["TRCK"]) == "1/2" and str(a["TPOS"]) == "1/2"
    assert str(c["TRCK"]) == "1/1" and str(c["TPOS"]) == "2/2"


def test_merge_subfolders_bonus_folder_appends_without_tpos(tmp_path):
    album = tmp_path / "Artist" / "2010 - Live"
    mk(album / "01. Artist - Main.mp3", TALB="Live", TYER="2010",
       TIT2="Main", TRCK="1/1")
    mk(album / "Bonus Tracks" / "01. Artist - Extra.mp3", TALB="Live",
       TYER="2010", TIT2="Extra", TRCK="1/1")
    st.step_merge_subfolders(tmp_path, False, preserve_tpos=True)
    # A non-disc subfolder forces sequential renumbering — extras append after
    # the parent's own tracks and never receive a bogus TPOS.
    names = sorted(p.name for p in album.glob("*.mp3"))
    assert names == ["01. Artist - Main.mp3", "02. Artist - Extra.mp3"]
    assert "TPOS" not in tags_of(album / "02. Artist - Extra.mp3")


def test_merge_subfolders_keeps_folder_with_unexpected_files(tmp_path):
    album = _disc_library(tmp_path)
    (album / "CD2" / "notes.txt").write_text("keep me")
    st.step_merge_subfolders(tmp_path, False)
    # Tracks still merged, but the folder holding a non-image file survives.
    assert not (album / "CD1").exists()
    assert (album / "CD2" / "notes.txt").exists()


def test_merge_subfolders_dry_run(tmp_path):
    _disc_library(tmp_path)
    stats = dry_run_untouched(st.step_merge_subfolders, tmp_path)
    # Albums are counted in dry-run, but "moved" only counts real renames.
    assert stats["albums"] == 1 and stats["moved"] == 0


def test_merge_subfolders_idempotent(tmp_path):
    _disc_library(tmp_path)
    st.step_merge_subfolders(tmp_path, False)
    stats = second_run_stable(st.step_merge_subfolders, tmp_path)
    assert stats["albums"] == 0 and stats["moved"] == 0


# ── Step 2: fix_missing_tags ──────────────────────────────────────────────────

def test_fix_missing_tags_fix(tmp_path):
    album = tmp_path / "Artist" / "2020 - Album"
    mk(album / "01. Artist - One.mp3", TALB=None, TIT2="One", TRCK="01/2")
    mk(album / "02. Artist - Mystery.mp3", TALB=None, TIT2=None, TRCK="02/2")

    prompts = []

    def answer(prompt):
        prompts.append(prompt)
        if "Album" in prompt:
            return "Filled Album"
        if "Title" in prompt:
            return ""            # accept the filename-derived suggestion
        raise AssertionError(prompt)

    stats = st.step_fix_missing_tags(tmp_path, False, ask_text=answer)

    assert stats["fixed"] == 2
    assert any("Album" in p for p in prompts)
    title_prompt = next(p for p in prompts if "Title" in p)
    assert "[Mystery]" in title_prompt        # suggestion comes from the filename
    for f in album.glob("*.mp3"):
        assert str(tags_of(f)["TALB"]) == "Filled Album"
    assert str(tags_of(album / "02. Artist - Mystery.mp3")["TIT2"]) == "Mystery"


def test_fix_missing_tags_autofills_year_and_albumartist(tmp_path):
    album = tmp_path / "Artist" / "2020 - Album"
    mk(album / "01. Artist - One.mp3", TYER=None, TPE2=None, TIT2="One", TRCK="01/1")
    stats = st.step_fix_missing_tags(tmp_path, False, ask_text=no_prompt)
    t = tags_of(album / "01. Artist - One.mp3")
    assert str(t["TYER"]) == "2020"           # from the folder name
    assert str(t["TPE2"]) == "Artist"         # copied from TPE1
    assert stats["fixed"] == 1


def test_fix_missing_tags_blank_answer_skips(tmp_path):
    album = tmp_path / "Artist" / "2020 - Album"
    mk(album / "01. Artist - One.mp3", TCON=None, TIT2="One", TRCK="01/1")
    stats = st.step_fix_missing_tags(tmp_path, False, ask_text=lambda p: "")
    assert stats["fixed"] == 0 and stats["skipped"] == 1
    assert "TCON" not in tags_of(album / "01. Artist - One.mp3")


def test_fix_missing_tags_dry_run(tmp_path):
    album = tmp_path / "Artist" / "2020 - Album"
    mk(album / "01. Artist - One.mp3", TALB=None, TIT2="One", TRCK="01/1")
    # Dry-run never prompts (no_prompt would raise) and never writes.
    stats = dry_run_untouched(st.step_fix_missing_tags, tmp_path, ask_text=no_prompt)
    assert stats["fixed"] == 0 and stats["skipped"] == 1


def test_fix_missing_tags_idempotent(tmp_path):
    album = tmp_path / "Artist" / "2020 - Album"
    mk(album / "01. Artist - One.mp3", TALB=None, TIT2="One", TRCK="01/1")
    st.step_fix_missing_tags(tmp_path, False, ask_text=lambda p: "Filled")
    stats = second_run_stable(st.step_fix_missing_tags, tmp_path, ask_text=no_prompt)
    assert stats["fixed"] == 0


# ── Step 3: enforce_id3v23 ────────────────────────────────────────────────────

def test_enforce_id3v23_fix(tmp_path):
    album = tmp_path / "Artist" / "2020 - Album"
    v1 = album / "01. Artist - One.mp3"
    v24 = album / "02. Artist - Two.mp3"
    noyear = album / "03. Artist - Three.mp3"
    mk(v1, TIT2="One", TRCK="01/3")
    add_id3v1(v1)
    mk(v24, TIT2="Two", TRCK="02/3")
    make_v24(v24)
    mk(noyear, TIT2="Three", TRCK="03/3", TYER=None)

    stats = st.step_enforce_id3v23(tmp_path, False)

    assert stats["fixed"] == 3
    import audit
    assert not audit._has_id3v1(v1)
    t24 = tags_of(v24)
    assert t24.version[:2] == (2, 3)
    assert "TDRC" not in t24 and str(t24["TYER"]) == "2020"
    assert str(tags_of(noyear)["TYER"]) == "2020"     # from the folder name


def test_enforce_id3v23_dry_run(tmp_path):
    p = tmp_path / "Artist" / "2020 - Album" / "01. Artist - One.mp3"
    mk(p, TIT2="One", TRCK="01/1")
    add_id3v1(p)
    stats = dry_run_untouched(st.step_enforce_id3v23, tmp_path)
    assert stats["fixed"] == 1


def test_enforce_id3v23_idempotent(tmp_path):
    p = tmp_path / "Artist" / "2020 - Album" / "01. Artist - One.mp3"
    mk(p, TIT2="One", TRCK="01/1")
    make_v24(p)
    st.step_enforce_id3v23(tmp_path, False)
    stats = second_run_stable(st.step_enforce_id3v23, tmp_path)
    assert stats["fixed"] == 0


# ── Step 4: strip_tags ────────────────────────────────────────────────────────

def _decorated_mp3(tmp_path, **mk_overrides):
    p = tmp_path / "Artist" / "2020 - Album" / "01. Artist - One.mp3"
    mk(p, TIT2="One", TRCK="01/1", **mk_overrides)
    t = ID3(p, translate=False)
    t.add(COMM(encoding=3, lang="eng", desc="", text="a comment"))
    t.add(TXXX(encoding=3, desc="foo", text="bar"))
    t.add(TXXX(encoding=3, desc="replaygain_track_gain", text="-3.0 dB"))
    t.add(TXXX(encoding=3, desc="iTunSMPB", text=" 000000 etc"))
    t.add(TPOS(encoding=3, text="1/2"))
    t.add(APIC(encoding=3, mime="image/png", type=3, desc="", data=TINY_PNG))
    t.save(p, v2_version=3, v1=0)
    return p


def test_strip_tags_fix(tmp_path):
    p = _decorated_mp3(tmp_path)
    stats = st.step_strip_tags(tmp_path, False)
    remaining = set(tags_of(p).keys())
    assert remaining == {"TPE1", "TPE2", "TIT2", "TALB", "TYER", "TCON", "TRCK"}
    assert stats["files"] == 1 and stats["tags_removed"] == 6
    # The doomed APIC was rescued to a folder cover first (mime png → .png).
    assert (p.parent / "cover.png").read_bytes() == TINY_PNG
    assert stats["covers_extracted"] == 1


def test_strip_tags_keep_flags(tmp_path):
    p = _decorated_mp3(tmp_path)
    st.step_strip_tags(tmp_path, False, keep_apic=True, keep_replay_gain=True,
                       keep_tpos=True, keep_gapless=True)
    remaining = set(tags_of(p).keys())
    assert "APIC:" in remaining
    assert "TXXX:replaygain_track_gain" in remaining
    assert "TXXX:iTunSMPB" in remaining
    assert "TPOS" in remaining
    assert not any(k.startswith("COMM") for k in remaining)  # never whitelisted
    assert "TXXX:foo" not in remaining


def test_strip_tags_migrates_legacy_albumartist(tmp_path):
    p = tmp_path / "Artist" / "2020 - Album" / "01. Artist - One.mp3"
    mk(p, TIT2="One", TRCK="01/1", TPE2=None)
    t = ID3(p, translate=False)
    t.add(TXXX(encoding=3, desc="ALBUM ARTIST", text="Legacy AA"))
    t.save(p, v2_version=3, v1=0)

    stats = st.step_strip_tags(tmp_path, False)
    t = tags_of(p)
    assert str(t["TPE2"]) == "Legacy AA"
    assert not any(k.startswith("TXXX") for k in t)
    assert stats["albumartist_fixed"] == 1


def test_strip_tags_sets_tcmp_for_compilations(tmp_path):
    p = tmp_path / "Various" / "2020 - Comp" / "01. Someone - One.mp3"
    mk(p, TPE1="Someone", TPE2="Various Artists", TIT2="One", TRCK="01/1")
    stats = st.step_strip_tags(tmp_path, False, keep_tcmp=True)
    assert str(tags_of(p)["TCMP"]) == "1"
    assert stats["tcmp_set"] == 1


def test_strip_tags_dry_run(tmp_path):
    _decorated_mp3(tmp_path)
    stats = dry_run_untouched(st.step_strip_tags, tmp_path)
    assert stats["files"] == 1 and stats["tags_removed"] == 6
    assert stats["covers_extracted"] == 1      # counted, but nothing written


def test_strip_tags_idempotent(tmp_path):
    _decorated_mp3(tmp_path)
    st.step_strip_tags(tmp_path, False)
    stats = second_run_stable(st.step_strip_tags, tmp_path)
    assert stats["files"] == 0 and stats["tags_removed"] == 0


# ── Step 5: normalize_chars ───────────────────────────────────────────────────

def test_normalize_chars_fix(tmp_path):
    album = tmp_path / "Artist’s Band" / "2020 - Album"
    f = album / "01. Artist - Don’t Stop.mp3"
    mk(f, TIT2="Don’t Stop", TRCK="01/1")

    stats = st.step_normalize_chars(tmp_path, False)

    assert stats["tags"] == 1 and stats["files"] == 1
    fixed = tmp_path / "Artist's Band" / "2020 - Album" / "01. Artist - Don't Stop.mp3"
    assert fixed.is_file()
    assert str(tags_of(fixed)["TIT2"]) == "Don't Stop"


def test_normalize_chars_composes_nfc(tmp_path):
    album = tmp_path / "Artist" / "2020 - Album"
    f = album / ("01. Artist - Café.mp3")        # decomposed filename
    mk(f, TIT2="Café", TRCK="01/1")              # decomposed tag

    stats = st.step_normalize_chars(tmp_path, False)

    assert stats["tags"] == 1 and stats["files"] == 1
    fixed = album / "01. Artist - Café.mp3"
    assert fixed.is_file()
    assert str(tags_of(fixed)["TIT2"]) == "Café"
    stats = second_run_stable(st.step_normalize_chars, tmp_path)
    assert stats["tags"] == 0 and stats["files"] == 0


def test_normalize_chars_dry_run(tmp_path):
    mk(tmp_path / "A" / "2020 - X" / "01. A - Don’t.mp3", TIT2="Don’t", TRCK="01/1")
    stats = dry_run_untouched(st.step_normalize_chars, tmp_path)
    assert stats["tags"] == 1 and stats["files"] == 1


def test_normalize_chars_idempotent(tmp_path):
    mk(tmp_path / "A’s" / "2020 - X" / "01. A - Don’t.mp3", TIT2="Don’t", TRCK="01/1")
    st.step_normalize_chars(tmp_path, False)
    stats = second_run_stable(st.step_normalize_chars, tmp_path)
    assert stats["tags"] == 0 and stats["files"] == 0


# ── Step 5a: replace_title_brackets ───────────────────────────────────────────

def test_replace_title_brackets_fix(tmp_path):
    p = tmp_path / "Artist" / "2020 - Album" / "01. Artist - One.mp3"
    mk(p, TIT2="One [Live]", TALB="Album [Deluxe]", TRCK="01/1")
    stats = st.step_replace_title_brackets(tmp_path, False)
    t = tags_of(p)
    assert str(t["TIT2"]) == "One (Live)"
    assert str(t["TALB"]) == "Album (Deluxe)"
    assert stats["files"] == 1 and stats["fields"] == 2


def test_replace_title_brackets_dry_run(tmp_path):
    mk(tmp_path / "A" / "2020 - X" / "01. A - T.mp3", TIT2="T [x]", TRCK="01/1")
    stats = dry_run_untouched(st.step_replace_title_brackets, tmp_path)
    assert stats["files"] == 1 and stats["fields"] == 1


def test_replace_title_brackets_idempotent(tmp_path):
    mk(tmp_path / "A" / "2020 - X" / "01. A - T.mp3", TIT2="T [x]", TRCK="01/1")
    st.step_replace_title_brackets(tmp_path, False)
    stats = second_run_stable(st.step_replace_title_brackets, tmp_path)
    assert stats["files"] == 0


# ── Step 6: normalize_year ────────────────────────────────────────────────────

def test_normalize_year_fix(tmp_path):
    p = tmp_path / "Artist" / "2020 - Album" / "01. Artist - One.mp3"
    mk(p, TYER="2020-05-01", TIT2="One", TRCK="01/1")
    stats = st.step_normalize_year(tmp_path, False)
    assert str(tags_of(p)["TYER"]) == "2020"
    assert stats["fixed"] == 1


def test_normalize_year_dry_run(tmp_path):
    mk(tmp_path / "A" / "2020 - X" / "01. A - T.mp3", TYER="2020-05-01",
       TIT2="T", TRCK="01/1")
    stats = dry_run_untouched(st.step_normalize_year, tmp_path)
    assert stats["fixed"] == 1


def test_normalize_year_idempotent(tmp_path):
    mk(tmp_path / "A" / "2020 - X" / "01. A - T.mp3", TYER="2020-05-01",
       TIT2="T", TRCK="01/1")
    st.step_normalize_year(tmp_path, False)
    stats = second_run_stable(st.step_normalize_year, tmp_path)
    assert stats["fixed"] == 0


# ── Step 7: renumber_tracks ───────────────────────────────────────────────────

def _gappy_album(root):
    album = root / "Artist" / "2020 - Album"
    mk(album / "01. Artist - One.mp3", TIT2="One", TRCK="01/7")
    mk(album / "03. Artist - Three.mp3", TIT2="Three", TRCK="03/7")
    mk(album / "07. Artist - Seven.mp3", TIT2="Seven", TRCK="07/7")
    return album


def test_renumber_tracks_fix(tmp_path):
    album = _gappy_album(tmp_path)
    stats = st.step_renumber_tracks(tmp_path, False)
    assert stats["fixed"] == 2
    # Only files whose number changes are rewritten — track 1 keeps its stale
    # total (step 9 fixes totals); 3→2 and 7→3 get fresh padded values.
    assert str(tags_of(album / "01. Artist - One.mp3")["TRCK"]) == "01/7"
    assert str(tags_of(album / "03. Artist - Three.mp3")["TRCK"]) == "02/3"
    assert str(tags_of(album / "07. Artist - Seven.mp3")["TRCK"]) == "03/3"


def test_renumber_tracks_respect_tpos(tmp_path):
    album = tmp_path / "Artist" / "2020 - Album"
    mk(album / "a.mp3", TIT2="A", TRCK="01/2", TPOS="1/2")
    mk(album / "b.mp3", TIT2="B", TRCK="03/2", TPOS="1/2")
    mk(album / "c.mp3", TIT2="C", TRCK="02/2", TPOS="2/2")
    mk(album / "d.mp3", TIT2="D", TRCK="05/2", TPOS="2/2")
    stats = st.step_renumber_tracks(tmp_path, False, respect_tpos=True)
    # Gaps close per disc: disc 1 [1,3]→[1,2], disc 2 [2,5]→[1,2].
    assert stats["fixed"] == 3
    assert str(tags_of(album / "b.mp3")["TRCK"]) == "02/2"
    assert str(tags_of(album / "c.mp3")["TRCK"]) == "01/2"
    assert str(tags_of(album / "d.mp3")["TRCK"]) == "02/2"


def test_renumber_tracks_skips_duplicates(tmp_path):
    album = tmp_path / "Artist" / "2020 - Album"
    mk(album / "a.mp3", TIT2="A", TRCK="02/2")
    mk(album / "b.mp3", TIT2="B", TRCK="02/2")
    stats = st.step_renumber_tracks(tmp_path, False)
    assert stats["fixed"] == 0           # duplicate numbers are not our job


def test_renumber_tracks_dry_run(tmp_path):
    _gappy_album(tmp_path)
    stats = dry_run_untouched(st.step_renumber_tracks, tmp_path)
    assert stats["fixed"] == 2


def test_renumber_tracks_idempotent(tmp_path):
    _gappy_album(tmp_path)
    st.step_renumber_tracks(tmp_path, False)
    stats = second_run_stable(st.step_renumber_tracks, tmp_path)
    assert stats["fixed"] == 0


# ── Step 8: pad_tracks ────────────────────────────────────────────────────────

def test_pad_tracks_fix(tmp_path):
    album = tmp_path / "Artist" / "2020 - Album"
    mk(album / "a.mp3", TIT2="A", TRCK="1/2")
    mk(album / "b.mp3", TIT2="B", TRCK="02/2")
    stats = st.step_pad_tracks(tmp_path, False)
    assert stats["fixed"] == 1
    assert str(tags_of(album / "a.mp3")["TRCK"]) == "01/2"


def test_pad_tracks_width_3_for_big_albums(tmp_path):
    album = tmp_path / "Artist" / "2020 - Big"
    seed = album / "seed.mp3"
    mk(seed, TIT2="T", TRCK="1/100", TALB="Big")
    for i in range(2, 101):
        dst = album / f"t{i:03d}.mp3"
        shutil.copyfile(seed, dst)
        t = ID3(dst, translate=False)
        t["TRCK"] = TRCK(encoding=3, text=f"{i}/100")
        t.save(dst, v2_version=3, v1=0)
    st.step_pad_tracks(tmp_path, False)
    assert str(tags_of(seed)["TRCK"]) == "001/100"
    assert str(tags_of(album / "t042.mp3")["TRCK"]) == "042/100"


def test_pad_tracks_dry_run(tmp_path):
    mk(tmp_path / "A" / "2020 - X" / "a.mp3", TIT2="A", TRCK="1/1")
    stats = dry_run_untouched(st.step_pad_tracks, tmp_path)
    assert stats["fixed"] == 1


def test_pad_tracks_idempotent(tmp_path):
    mk(tmp_path / "A" / "2020 - X" / "a.mp3", TIT2="A", TRCK="1/1")
    st.step_pad_tracks(tmp_path, False)
    stats = second_run_stable(st.step_pad_tracks, tmp_path)
    assert stats["fixed"] == 0


# ── Step 9: set_total_tracks ──────────────────────────────────────────────────

def test_set_total_tracks_fix(tmp_path):
    album = tmp_path / "Artist" / "2020 - Album"
    mk(album / "a.mp3", TIT2="A", TRCK="01")          # no total
    mk(album / "b.mp3", TIT2="B", TRCK="02/5")        # wrong total
    stats = st.step_set_total_tracks(tmp_path, False)
    assert stats["fixed"] == 2
    assert str(tags_of(album / "a.mp3")["TRCK"]) == "01/2"
    assert str(tags_of(album / "b.mp3")["TRCK"]) == "02/2"


def test_set_total_tracks_respect_tpos(tmp_path):
    album = tmp_path / "Artist" / "2020 - Album"
    mk(album / "a.mp3", TIT2="A", TRCK="01", TPOS="1/2")
    mk(album / "b.mp3", TIT2="B", TRCK="02", TPOS="1/2")
    mk(album / "c.mp3", TIT2="C", TRCK="01", TPOS="2/2")
    st.step_set_total_tracks(tmp_path, False, respect_tpos=True)
    # Totals are per disc, not per folder.
    assert str(tags_of(album / "a.mp3")["TRCK"]) == "01/2"
    assert str(tags_of(album / "c.mp3")["TRCK"]) == "01/1"


def test_set_total_tracks_dry_run(tmp_path):
    mk(tmp_path / "A" / "2020 - X" / "a.mp3", TIT2="A", TRCK="01")
    stats = dry_run_untouched(st.step_set_total_tracks, tmp_path)
    assert stats["fixed"] == 1


def test_set_total_tracks_idempotent(tmp_path):
    mk(tmp_path / "A" / "2020 - X" / "a.mp3", TIT2="A", TRCK="01")
    st.step_set_total_tracks(tmp_path, False)
    stats = second_run_stable(st.step_set_total_tracks, tmp_path)
    assert stats["fixed"] == 0


# ── Step 10: rename_album_folders ─────────────────────────────────────────────

def test_rename_album_folders_fix(tmp_path):
    folder = tmp_path / "Artist" / "Wrong Name"
    mk(folder / "01. Artist - One.mp3", TIT2="One", TRCK="01/1")
    safe = bystander(tmp_path)
    safe_before = snapshot(safe)

    stats = st.step_rename_album_folders(tmp_path, False)

    assert stats["renamed"] == 1
    assert not folder.exists()
    assert (tmp_path / "Artist" / "2020 - Album").is_dir()
    assert snapshot(safe) == safe_before


def test_rename_album_folders_collision_errors(tmp_path):
    mk(tmp_path / "Artist" / "Wrong Name" / "01. Artist - One.mp3",
       TIT2="One", TRCK="01/1")
    mk(tmp_path / "Artist" / "2020 - Album" / "01. Artist - Two.mp3",
       TIT2="Two", TRCK="01/1")
    stats = st.step_rename_album_folders(tmp_path, False)
    assert stats["errors"] == 1
    assert (tmp_path / "Artist" / "Wrong Name").is_dir()   # left in place


def test_rename_album_folders_dry_run(tmp_path):
    mk(tmp_path / "Artist" / "Wrong" / "01. Artist - One.mp3",
       TIT2="One", TRCK="01/1")
    stats = dry_run_untouched(st.step_rename_album_folders, tmp_path)
    assert stats["renamed"] == 1


def test_rename_album_folders_idempotent(tmp_path):
    mk(tmp_path / "Artist" / "Wrong" / "01. Artist - One.mp3",
       TIT2="One", TRCK="01/1")
    st.step_rename_album_folders(tmp_path, False)
    stats = second_run_stable(st.step_rename_album_folders, tmp_path)
    assert stats["renamed"] == 0 and stats["errors"] == 0


# ── Step 11: deduplicate_albums ───────────────────────────────────────────────

def _dup_library(root):
    a1 = root / "Artist" / "1999 - Album"
    a2 = root / "Artist" / "2005 - Album"
    mk(a1 / "01. Artist - One.mp3", TYER="1999", TIT2="One", TRCK="01/1")
    mk(a2 / "01. Artist - Two.mp3", TYER="2005", TIT2="Two", TRCK="01/1")
    return a1, a2


def test_deduplicate_albums_fix(tmp_path):
    a1, a2 = _dup_library(tmp_path)
    survivor_before = snapshot(a1)

    stats = st.step_deduplicate_albums(tmp_path, False)

    # First folder in sorted order is canonical and untouched; the second is
    # retagged "Album (2)" and its folder renamed to match (keeping its year).
    assert stats["renamed"] == 1 and stats["retagged"] == 1 and stats["errors"] == 0
    assert snapshot(a1) == survivor_before
    assert not a2.exists()
    renamed = tmp_path / "Artist" / "2005 - Album (2)"
    assert renamed.is_dir()
    assert str(tags_of(renamed / "01. Artist - Two.mp3")["TALB"]) == "Album (2)"


def test_deduplicate_albums_dry_run(tmp_path):
    _dup_library(tmp_path)
    stats = dry_run_untouched(st.step_deduplicate_albums, tmp_path)
    assert stats["renamed"] == 1 and stats["retagged"] == 1


def test_deduplicate_albums_idempotent(tmp_path):
    _dup_library(tmp_path)
    st.step_deduplicate_albums(tmp_path, False)
    stats = second_run_stable(st.step_deduplicate_albums, tmp_path)
    assert stats["renamed"] == 0 and stats["retagged"] == 0


# ── Step 12: rename_artist_folders ────────────────────────────────────────────

def _mismatched_artist(root):
    folder = root / "Wrong Artist" / "2020 - Album"
    mk(folder / "01. Artist - One.mp3", TPE1="Right Artist", TPE2="Right Artist",
       TIT2="One", TRCK="01/1")
    return folder


def test_rename_artist_folders_move_choice(tmp_path):
    _mismatched_artist(tmp_path)
    choices = []

    def choose(prompt):
        choices.append(prompt)
        return "m"

    stats = st.step_rename_artist_folders(tmp_path, False, ask_choice=choose)
    assert stats["renamed"] == 1
    assert (tmp_path / "Right Artist" / "2020 - Album").is_dir()
    assert not (tmp_path / "Wrong Artist").exists()
    assert any("[M]ove" in c for c in choices)


def test_rename_artist_folders_retag_choice(tmp_path):
    folder = _mismatched_artist(tmp_path)
    stats = st.step_rename_artist_folders(tmp_path, False, ask_choice=lambda p: "r")
    assert stats["retagged"] == 1
    assert (tmp_path / "Wrong Artist").is_dir()       # folder kept
    assert str(tags_of(folder / "01. Artist - One.mp3")["TPE2"]) == "Wrong Artist"


def test_rename_artist_folders_skip_choice(tmp_path):
    _mismatched_artist(tmp_path)
    before = snapshot(tmp_path)
    stats = st.step_rename_artist_folders(tmp_path, False, ask_choice=lambda p: "s")
    assert snapshot(tmp_path) == before
    assert stats["skipped"] == 1


def test_rename_artist_folders_moves_misplaced_album(tmp_path):
    own = tmp_path / "Main" / "2020 - Own"
    mk(own / "01. Main - A.mp3", TPE1="Main", TPE2="Main", TIT2="A", TRCK="01/2")
    mk(own / "02. Main - B.mp3", TPE1="Main", TPE2="Main", TIT2="B", TRCK="02/2")
    stray = tmp_path / "Main" / "2019 - Other"
    mk(stray / "01. Other - X.mp3", TPE1="Other Artist", TPE2="Other Artist",
       TIT2="X", TRCK="01/1")

    stats = st.step_rename_artist_folders(tmp_path, False, ask_choice=lambda p: "m")
    assert stats["moved"] == 1
    assert (tmp_path / "Other Artist" / "2019 - Other").is_dir()
    assert not stray.exists()


def test_rename_artist_folders_repairs_missing_albumartist(tmp_path):
    folder = tmp_path / "Solo" / "2020 - Album"
    mk(folder / "01. Solo - One.mp3", TPE1="Solo", TPE2=None, TIT2="One", TRCK="01/1")
    stats = st.step_rename_artist_folders(tmp_path, False, ask_choice=no_prompt)
    assert stats["retagged"] == 1
    assert str(tags_of(folder / "01. Solo - One.mp3")["TPE2"]) == "Solo"


def test_rename_artist_folders_dry_run(tmp_path):
    _mismatched_artist(tmp_path)
    stats = dry_run_untouched(st.step_rename_artist_folders, tmp_path,
                              ask_choice=no_prompt)   # dry run never prompts
    assert stats["renamed"] == 1


def test_rename_artist_folders_idempotent(tmp_path):
    _mismatched_artist(tmp_path)
    st.step_rename_artist_folders(tmp_path, False, ask_choice=lambda p: "m")
    stats = second_run_stable(st.step_rename_artist_folders, tmp_path,
                              ask_choice=no_prompt)
    assert stats["renamed"] == 0 and stats["retagged"] == 0 and stats["moved"] == 0


# ── Step 12a: enforce_track_artist ────────────────────────────────────────────

def test_enforce_track_artist_fix(tmp_path):
    album = tmp_path / "Band" / "2020 - Album"
    mk(album / "01. Band - One.mp3", TPE1="Band feat. Guest", TPE2="Band",
       TIT2="One", TRCK="01/2")
    mk(album / "02. Band - Two.mp3", TPE1="Band", TPE2="Band",
       TIT2="Two", TRCK="02/2")
    stats = st.step_enforce_track_artist(tmp_path, False)
    assert stats["fixed"] == 1 and stats["skipped"] == 1
    assert str(tags_of(album / "01. Band - One.mp3")["TPE1"]) == "Band"


def test_enforce_track_artist_dry_run(tmp_path):
    mk(tmp_path / "B" / "2020 - X" / "01. B - T.mp3", TPE1="Someone", TPE2="B",
       TIT2="T", TRCK="01/1")
    stats = dry_run_untouched(st.step_enforce_track_artist, tmp_path)
    assert stats["fixed"] == 1


def test_enforce_track_artist_idempotent(tmp_path):
    mk(tmp_path / "B" / "2020 - X" / "01. B - T.mp3", TPE1="Someone", TPE2="B",
       TIT2="T", TRCK="01/1")
    st.step_enforce_track_artist(tmp_path, False)
    stats = second_run_stable(st.step_enforce_track_artist, tmp_path)
    assert stats["fixed"] == 0


# ── Step 14: clean_files ──────────────────────────────────────────────────────

def test_clean_files_deletes_junk_on_confirm(tmp_path):
    album = tmp_path / "Artist" / "2020 - Album"
    mk(album / "01. Artist - One.mp3", TIT2="One", TRCK="01/1")
    (album / "cover.jpg").write_bytes(TINY_PNG)
    (album / "notes.txt").write_text("junk")
    (album / "rip.log").write_text("junk")
    safe = bystander(tmp_path)
    safe_before = snapshot(safe)

    stats = st.step_clean_files(tmp_path, False, ask_choice=lambda p: "d")

    assert stats["deleted"] == 2
    assert not (album / "notes.txt").exists() and not (album / "rip.log").exists()
    assert (album / "cover.jpg").exists()      # cover always preserved
    assert snapshot(safe) == safe_before


def test_clean_files_keep_choice(tmp_path):
    album = tmp_path / "Artist" / "2020 - Album"
    mk(album / "01. Artist - One.mp3", TIT2="One", TRCK="01/1")
    (album / "cover.jpg").write_bytes(TINY_PNG)
    (album / "notes.txt").write_text("junk")
    stats = st.step_clean_files(tmp_path, False, ask_choice=lambda p: "k")
    assert stats["deleted"] == 0
    assert (album / "notes.txt").exists()


def test_clean_files_extra_images_auto_deleted(tmp_path):
    album = tmp_path / "Artist" / "2020 - Album"
    mk(album / "01. Artist - One.mp3", TIT2="One", TRCK="01/1")
    (album / "cover.jpg").write_bytes(TINY_PNG)
    (album / "back.png").write_bytes(TINY_PNG)
    (album / "scan.jpg").write_bytes(TINY_PNG)
    stats = st.step_clean_files(tmp_path, False, ask_choice=no_prompt)
    assert stats["deleted"] == 2
    assert [p.name for p in sorted(album.iterdir()) if p.suffix != ".mp3"] == ["cover.jpg"]


def test_clean_files_renames_lone_image_to_cover(tmp_path):
    album = tmp_path / "Artist" / "2020 - Album"
    mk(album / "01. Artist - One.mp3", TIT2="One", TRCK="01/1")
    (album / "folder.png").write_bytes(TINY_PNG)
    stats = st.step_clean_files(tmp_path, False, ask_choice=no_prompt)
    assert stats["renamed_covers"] == 1
    assert (album / "cover.png").read_bytes() == TINY_PNG


def test_clean_files_reports_missing_cover_unless_embed(tmp_path):
    album = tmp_path / "Artist" / "2020 - Album"
    mk(album / "01. Artist - One.mp3", TIT2="One", TRCK="01/1")
    stats = st.step_clean_files(tmp_path, False, ask_choice=no_prompt)
    assert stats["missing_covers"] == 1
    stats = st.step_clean_files(tmp_path, False, cover_art="embed",
                                ask_choice=no_prompt)
    assert stats["missing_covers"] == 0


def test_clean_files_dry_run(tmp_path):
    album = tmp_path / "Artist" / "2020 - Album"
    mk(album / "01. Artist - One.mp3", TIT2="One", TRCK="01/1")
    (album / "cover.jpg").write_bytes(TINY_PNG)
    (album / "notes.txt").write_text("junk")
    (album / "back.png").write_bytes(TINY_PNG)
    stats = dry_run_untouched(st.step_clean_files, tmp_path, ask_choice=no_prompt)
    assert stats["deleted"] == 2       # 1 auto image + 1 pending confirmation


def test_clean_files_idempotent(tmp_path):
    album = tmp_path / "Artist" / "2020 - Album"
    mk(album / "01. Artist - One.mp3", TIT2="One", TRCK="01/1")
    (album / "cover.jpg").write_bytes(TINY_PNG)
    (album / "notes.txt").write_text("junk")
    st.step_clean_files(tmp_path, False, ask_choice=lambda p: "d")
    stats = second_run_stable(st.step_clean_files, tmp_path, ask_choice=no_prompt)
    assert stats["deleted"] == 0 and stats["renamed_covers"] == 0


# ── Step 15: embed_cover_art ──────────────────────────────────────────────────

def _cover_album(root):
    album = root / "Artist" / "2020 - Album"
    mk(album / "01. Artist - One.mp3", TIT2="One", TRCK="01/2")
    mk(album / "02. Artist - Two.mp3", TIT2="Two", TRCK="02/2")
    (album / "cover.jpg").write_bytes(TINY_PNG)
    return album


def test_embed_cover_art_fix(tmp_path):
    album = _cover_album(tmp_path)
    stats = st.step_embed_cover_art(tmp_path, False)
    assert stats["embedded"] == 2 and stats["errors"] == 0
    for f in album.glob("*.mp3"):
        assert tags_of(f)["APIC:"].data == TINY_PNG
    assert (album / "cover.jpg").exists()      # kept unless delete_covers


def test_embed_cover_art_delete_covers(tmp_path):
    album = _cover_album(tmp_path)
    st.step_embed_cover_art(tmp_path, False, delete_covers=True)
    assert not (album / "cover.jpg").exists()


def test_embed_cover_art_no_cover_counted(tmp_path):
    album = tmp_path / "Artist" / "2020 - Album"
    mk(album / "01. Artist - One.mp3", TIT2="One", TRCK="01/1")
    stats = st.step_embed_cover_art(tmp_path, False)
    assert stats["no_cover"] == 1 and stats["embedded"] == 0


def test_embed_cover_art_dry_run(tmp_path):
    _cover_album(tmp_path)
    stats = dry_run_untouched(st.step_embed_cover_art, tmp_path)
    assert stats["embedded"] == 2


def test_embed_cover_art_idempotent(tmp_path):
    _cover_album(tmp_path)
    st.step_embed_cover_art(tmp_path, False)
    stats = second_run_stable(st.step_embed_cover_art, tmp_path)
    assert stats["embedded"] == 0 and stats["already_ok"] == 2


# ── Step 16: fetch_missing_art (network mocked) ──────────────────────────────

def _mock_art_search(monkeypatch, score=200, results=True):
    import fetch_art

    def fake_search(artist, album, settings, interactive=False):
        if not results:
            return []
        return [{"url": "https://example.com/a.png", "artist": artist,
                 "album": album, "score": score, "source_label": "TestSrc"}]

    monkeypatch.setattr(fetch_art, "search_art_sources", fake_search)
    monkeypatch.setattr(fetch_art, "fetch_artwork",
                        lambda url: (TINY_PNG, "image/png"))


def test_fetch_missing_art_writes_cover(tmp_path, monkeypatch):
    pytest.importorskip("PIL")
    album = tmp_path / "Artist" / "2020 - Album"
    mk(album / "01. Artist - One.mp3", TIT2="One", TRCK="01/1")
    _mock_art_search(monkeypatch)

    stats = st.step_fetch_missing_art(tmp_path, False)

    assert stats["fetched"] == 1 and stats["by_source"] == {"TestSrc": 1}
    assert (album / "cover.png").read_bytes() == TINY_PNG


def test_fetch_missing_art_embed_mode(tmp_path, monkeypatch):
    pytest.importorskip("PIL")
    album = tmp_path / "Artist" / "2020 - Album"
    mk(album / "01. Artist - One.mp3", TIT2="One", TRCK="01/1")
    _mock_art_search(monkeypatch)
    st.step_fetch_missing_art(tmp_path, False, cover_art="embed")
    assert tags_of(album / "01. Artist - One.mp3")["APIC:"].data == TINY_PNG


def test_fetch_missing_art_low_confidence_skipped(tmp_path, monkeypatch):
    album = tmp_path / "Artist" / "2020 - Album"
    mk(album / "01. Artist - One.mp3", TIT2="One", TRCK="01/1")
    _mock_art_search(monkeypatch, score=10)
    stats = st.step_fetch_missing_art(tmp_path, False)
    assert stats["uncertain"] == 1 and stats["fetched"] == 0
    assert not list(album.glob("cover.*"))


def test_fetch_missing_art_not_found(tmp_path, monkeypatch):
    mk(tmp_path / "A" / "2020 - X" / "01. A - T.mp3", TIT2="T", TRCK="01/1")
    _mock_art_search(monkeypatch, results=False)
    stats = st.step_fetch_missing_art(tmp_path, False)
    assert stats["not_found"] == 1


def test_fetch_missing_art_skips_albums_with_art(tmp_path, monkeypatch):
    _cover_album(tmp_path)
    _mock_art_search(monkeypatch)
    stats = st.step_fetch_missing_art(tmp_path, False)
    assert stats["skipped"] == 1 and stats["fetched"] == 0


def test_fetch_missing_art_dry_run(tmp_path, monkeypatch):
    mk(tmp_path / "A" / "2020 - X" / "01. A - T.mp3", TIT2="T", TRCK="01/1")
    _mock_art_search(monkeypatch)
    stats = dry_run_untouched(st.step_fetch_missing_art, tmp_path)
    assert stats["fetched"] == 1


# ── Step 0: convert_lossless ──────────────────────────────────────────────────

def test_convert_lossless_fix(tmp_path):
    album = tmp_path / "Artist" / "2020 - Album"
    album.mkdir(parents=True)
    make_flac(album / "01. Artist - One.flac", title="One", artist="Artist",
              albumartist="Artist", album="Album", date="2020", tracknumber="1")

    stats = step_convert_lossless(tmp_path, False, ask_choice=lambda p: "3")   # 3 = 320 kbps

    assert not (album / "01. Artist - One.flac").exists()
    mp3 = album / "01. Artist - One.mp3"
    assert mp3.is_file()
    t = tags_of(mp3)
    assert str(t["TIT2"]) == "One" and str(t["TPE2"]) == "Artist"
    assert stats.get("errors", 0) == 0


def test_convert_lossless_dry_run(tmp_path):
    album = tmp_path / "Artist" / "2020 - Album"
    album.mkdir(parents=True)
    make_flac(album / "01. Artist - One.flac", title="One")
    before = snapshot(tmp_path)
    step_convert_lossless(tmp_path, True, ask_choice=no_prompt)
    assert snapshot(tmp_path) == before


def test_convert_lossless_idempotent(tmp_path):
    album = tmp_path / "Artist" / "2020 - Album"
    album.mkdir(parents=True)
    make_flac(album / "01. Artist - One.flac", title="One", artist="Artist",
              albumartist="Artist", album="Album", date="2020", tracknumber="1")
    step_convert_lossless(tmp_path, False, ask_choice=lambda p: "3")   # 3 = 320 kbps
    stats = second_run_stable(step_convert_lossless, tmp_path, ask_choice=no_prompt)
    assert stats.get("converted", 0) == 0
