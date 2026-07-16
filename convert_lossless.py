#!/usr/bin/env python3
"""
Utilities for detecting and converting lossless audio (FLAC/ALAC) to MP3.
Used by both standardize.py (in-place conversion) and import_tracks.py (convert-on-copy).
"""

import re
import shutil
import subprocess
from collections import Counter
from pathlib import Path

import encoding
import tagio
from encoding import EncodeProfile
from mp3header import has_lame_header

LOSSLESS_EXTENSIONS = {".flac", ".m4a", ".alac"}


def _has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def is_alac(path: Path) -> bool:
    """Return True if the M4A file contains ALAC (lossless) audio, not AAC."""
    try:
        from mutagen.mp4 import MP4
        audio = MP4(str(path))
        return bool(audio.info and audio.info.codec and audio.info.codec.startswith("alac"))
    except Exception:
        return True  # assume lossless on read error (conservative)


def find_lossless(root: Path) -> list[Path]:
    """Return sorted list of all lossless files under root, skipping AAC .m4a files."""
    files = []
    for ext in LOSSLESS_EXTENSIONS:
        for path in root.rglob(f"*{ext}"):
            if ext == ".m4a" and not is_alac(path):
                print(f"  SKIP {path.name} (AAC inside .m4a container — not lossless)")
                continue
            files.append(path)
    return sorted(files)


def read_lossless_tags(path: Path) -> dict:
    """Read tags from a FLAC or M4A/ALAC file into the standard tag dict."""
    empty = {"TPE1": None, "ALBUMARTIST": None, "TIT2": None, "TALB": None,
             "YEAR": None, "TCON": None, "TRCK": None}

    def _year(raw: str | None) -> str | None:
        if not raw:
            return None
        m = re.search(r'\b(19\d{2}|20\d{2})\b', raw)
        return m.group(1) if m else raw[:4]

    if path.suffix.lower() == ".flac":
        try:
            from mutagen.flac import FLAC
            audio = FLAC(str(path))

            def g(key: str) -> str | None:
                v = audio.get(key)
                return v[0] if v else None

            trck_n = g("tracknumber")
            trck_t = g("totaltracks") or g("tracktotal")
            trck   = f"{trck_n}/{trck_t}" if trck_n and trck_t else trck_n

            disc_n = g("discnumber") or g("disc")
            disc_t = g("disctotal") or g("totaldiscs")
            tpos   = f"{disc_n}/{disc_t}" if disc_n and disc_t else disc_n

            return {
                "TPE1": g("artist"),
                "ALBUMARTIST": g("albumartist") or g("album artist") or g("album_artist"),
                "TIT2": g("title"),
                "TALB": g("album"),
                "YEAR": _year(g("date") or g("year")),
                "TCON": g("genre"),
                "TRCK": trck,
                "TPOS": tpos,
            }
        except Exception as e:
            print(f"  ERROR reading {path.name}: {e}")
            return empty

    else:  # .m4a / .alac
        try:
            from mutagen.mp4 import MP4
            audio = MP4(str(path))
            tags = audio.tags or {}

            def g(key: str) -> str | None:
                v = tags.get(key)
                if not v:
                    return None
                item = v[0]
                return str(item) if not isinstance(item, tuple) else None

            trkn = tags.get("trkn")
            trck = None
            if trkn and isinstance(trkn[0], tuple):
                num, total = trkn[0]
                trck = f"{num}/{total}" if total else str(num)

            disk = tags.get("disk")
            tpos = None
            if disk and isinstance(disk[0], tuple):
                dnum, dtotal = disk[0]
                tpos = f"{dnum}/{dtotal}" if dtotal else str(dnum)

            return {
                "TPE1": g("\xa9ART"),
                "ALBUMARTIST": g("aART"),
                "TIT2": g("\xa9nam"),
                "TALB": g("\xa9alb"),
                "YEAR": _year(g("\xa9day")),
                "TCON": g("\xa9gen"),
                "TRCK": trck,
                "TPOS": tpos,
            }
        except Exception as e:
            print(f"  ERROR reading {path.name}: {e}")
            return empty


def prompt_bitrate(ask_choice=None) -> int | None:
    """Ask user to pick 192/256/320 kbps or skip. Returns kbps int or None."""
    while True:
        try:
            prompt = "  Convert to: [1] 192 kbps  [2] 256 kbps  [3] 320 kbps  [S]kip: "
            if ask_choice:
                choice = str(ask_choice(prompt)).strip().lower()
            else:
                choice = input(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if choice == "1":
            return 192
        if choice == "2":
            return 256
        if choice == "3":
            return 320
        if choice in ("s", ""):
            return None


def _has_lame_binary() -> bool:
    return shutil.which("lame") is not None


def _apply_source_tags(src: Path, dst: Path) -> None:
    """Copy the lossless source's tags onto the freshly-encoded output, leaving
    the audio (and any gapless header) untouched. Used for the in-place
    standardize path, where the encoder produces an untagged file. MP3 keeps the
    historical mutagen ID3 path exactly; other formats route through the
    format-agnostic tagio interface (canonical model)."""
    if dst.suffix.lower() != ".mp3":
        try:
            src_tags = read_lossless_tags(src)
            audio = tagio.open_audio(dst)
            if audio is not None:
                audio.write({
                    "title":        src_tags.get("TIT2"),
                    "artist":       src_tags.get("TPE1"),
                    "album_artist": src_tags.get("ALBUMARTIST"),
                    "album":        src_tags.get("TALB"),
                    "date":         src_tags.get("YEAR"),
                    "genre":        src_tags.get("TCON"),
                    "track":        src_tags.get("TRCK"),
                    "disc":         src_tags.get("TPOS"),
                })
        except Exception as e:
            print(f"    WARNING: could not copy tags to {dst.name}: {e}")
        return
    try:
        from mutagen.id3 import (ID3, ID3NoHeaderError, TPE1, TPE2, TIT2,
                                 TALB, TYER, TCON, TRCK, TPOS)
        tags = read_lossless_tags(src)
        try:
            # translate=False: never let mutagen rewrite TYER→TDRC on load
            # (project ID3 rule). The ffmpeg fallback path leaves a tag on dst.
            id3 = ID3(str(dst), translate=False)
        except ID3NoHeaderError:
            id3 = ID3()
        if "TDRC" in id3:            # v2.4 relic from the ffmpeg fallback muxer
            del id3["TDRC"]
        framemap = {
            "TPE1": (TPE1, tags.get("TPE1")),
            "TPE2": (TPE2, tags.get("ALBUMARTIST")),
            "TIT2": (TIT2, tags.get("TIT2")),
            "TALB": (TALB, tags.get("TALB")),
            "TYER": (TYER, tags.get("YEAR")),
            "TCON": (TCON, tags.get("TCON")),
            "TRCK": (TRCK, tags.get("TRCK")),
            "TPOS": (TPOS, tags.get("TPOS")),
        }
        for key, (cls, val) in framemap.items():
            if val:
                id3[key] = cls(encoding=1, text=str(val))
        id3.save(str(dst), v2_version=3, v1=0)
    except Exception as e:
        print(f"    WARNING: could not copy tags to {dst.name}: {e}")


def _source_audio_info(src: Path) -> tuple[int, int, int]:
    """(channels, sample_rate, bits_per_sample) of the lossless source, zeros
    when unknown (treated as plain 16-bit stereo ≤48k, i.e. no extra args)."""
    try:
        if src.suffix.lower() == ".flac":
            from mutagen.flac import FLAC
            info = FLAC(str(src)).info
        else:
            from mutagen.mp4 import MP4
            info = MP4(str(src)).info
        return (getattr(info, "channels", 0) or 0,
                getattr(info, "sample_rate", 0) or 0,
                getattr(info, "bits_per_sample", 0) or 0)
    except Exception:
        return (0, 0, 0)


def _decode_args(src: Path) -> list[str]:
    """Extra ffmpeg output args so any lossless source decodes to something
    `lame` can encode at full quality: downmix >2ch to stereo (lame can't take
    5.1), clamp >48kHz to an MP3-legal rate (44.1k for the 88.2/176.4 family,
    else 48k), and dither >16-bit sources to s16 instead of truncating."""
    ch, rate, bits = _source_audio_info(src)
    args: list[str] = []
    if ch > 2:
        args += ["-ac", "2"]
    osr = None
    if rate > 48000:
        osr = 44100 if rate % 44100 == 0 else 48000
    if bits > 16:
        opts = ["osf=s16", "dither_method=triangular"]
        if osr:
            opts.append(f"osr={osr}")
        args += ["-af", "aresample=" + ":".join(opts)]
    elif osr:
        args += ["-ar", str(osr)]
    return args


def _lame_pipe(src: Path, dst: Path, lame_frag,
               start_time: float | None = None,
               end_time: float | None = None) -> bool:
    """Decode src with ffmpeg (dropping any embedded cover-art video stream via
    -map 0:a) and pipe PCM to the reference `lame` encoder, which writes a correct
    gapless Xing/LAME header. This is the only way to get real encoder
    delay/padding — ffmpeg's own libmp3lame muxer writes dummy values.
    *lame_frag* is the encoder-mode fragment (e.g. ("--cbr","-b","320") or
    ("-V","0")) — the only thing that differs between MP3 profiles."""
    dec = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(src)]
    if start_time is not None:
        dec += ["-ss", f"{start_time:.6f}"]
    if end_time is not None:
        dec += ["-to", f"{end_time:.6f}"]
    dec += ["-map", "0:a"] + _decode_args(src) + ["-c:a", "pcm_s16le", "-f", "wav", "pipe:1"]
    enc = ["lame", "--quiet", "-q", "0", *lame_frag, "-", str(dst)]
    try:
        p1 = subprocess.Popen(dec, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        p2 = subprocess.Popen(enc, stdin=p1.stdout, stderr=subprocess.PIPE)
        p1.stdout.close()  # let ffmpeg get SIGPIPE if lame dies
        _, enc_err = p2.communicate()
        dec_err = p1.stderr.read()
        p1.stderr.close()
        p1.wait()
        if p1.returncode != 0:
            print(f"    ffmpeg error: {dec_err.decode('utf-8', 'replace')[-300:].strip()}")
            if dst.exists():
                dst.unlink()
            return False
        if p2.returncode != 0:
            print(f"    lame error: {enc_err.decode('utf-8', 'replace')[-300:].strip()}")
            if dst.exists():
                dst.unlink()
            return False
        return True
    except FileNotFoundError as e:
        print(f"    ERROR: {e}")
        return False
    except Exception as e:
        print(f"    ERROR: {e}")
        return False


def _lame_pipe_convert(src: Path, dst: Path, bitrate: int,
                       start_time: float | None = None,
                       end_time: float | None = None) -> bool:
    """Back-compat shim: CBR encode at *bitrate* kbps (the historical behaviour,
    byte-identical). New code calls convert_audio() with an EncodeProfile."""
    return _lame_pipe(src, dst, ("--cbr", "-b", str(bitrate)), start_time, end_time)


def _ffmpeg_mp3(src: Path, dst: Path, lame_frag,
                start_time: float | None = None,
                end_time: float | None = None) -> bool:
    """Fallback MP3 encoder used only when `lame` is not installed: ffmpeg
    libmp3lame directly. Produces a working MP3 but with an incomplete gapless
    header. *lame_frag* is the profile's lame fragment, mapped to ffmpeg's
    equivalent (`-V n` → `-q:a n` VBR; `--cbr -b N` → `-b:a Nk`)."""
    if lame_frag and lame_frag[0] == "-V":
        rate_args = ["-q:a", str(lame_frag[1])]
    else:  # ("--cbr", "-b", "N")
        rate_args = ["-b:a", f"{lame_frag[-1]}k"]
    try:
        cmd = ["ffmpeg", "-i", str(src)]
        if start_time is not None:
            cmd += ["-ss", f"{start_time:.6f}"]
        if end_time is not None:
            cmd += ["-to", f"{end_time:.6f}"]
        cmd += ["-c:a", "libmp3lame"] + rate_args + [
                "-compression_level", "0",
                "-map", "0:a"] + _decode_args(src) + [
                "-map_metadata", "0",
                "-id3v2_version", "3", "-y", str(dst)]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"    ffmpeg error: {result.stderr[-300:].strip()}")
            return False
        return True
    except FileNotFoundError:
        print("    ERROR: ffmpeg not found. Install it: sudo apt install ffmpeg")
        return False
    except Exception as e:
        print(f"    ERROR: {e}")
        return False


def _ffmpeg_convert(src: Path, dst: Path, bitrate: int,
                    start_time: float | None = None,
                    end_time: float | None = None) -> bool:
    """Back-compat shim over _ffmpeg_mp3 (CBR at *bitrate* kbps)."""
    return _ffmpeg_mp3(src, dst, ("--cbr", "-b", str(bitrate)), start_time, end_time)


def _valid_opus(dst: Path) -> bool:
    """True if *dst* probes as a valid Opus stream with a nonzero duration."""
    try:
        r = subprocess.run(
            ["ffprobe", "-hide_banner", "-loglevel", "error",
             "-select_streams", "a:0",
             "-show_entries", "stream=codec_name:format=duration",
             "-of", "default=nw=1", str(dst)],
            capture_output=True, text=True)
        out = r.stdout
        if "codec_name=opus" not in out:
            return False
        for line in out.splitlines():
            if line.startswith("duration="):
                try:
                    return float(line.split("=", 1)[1]) > 0
                except ValueError:
                    return False
        return False
    except Exception:
        return False


def _opus_convert(src: Path, dst: Path, bitrate: int,
                  start_time: float | None = None,
                  end_time: float | None = None) -> bool:
    """Encode src to VBR Opus at *bitrate* kbps via ffmpeg libopus. Unlike MP3,
    ffmpeg's Ogg Opus muxing is gapless-correct (pre-skip/end-trim are mandatory
    parts of the format), so no encoder pipe is needed. -map 0:a drops any
    embedded cover-art video stream; libopus preserves the source channel count
    (mono stays mono, stereo stays stereo — never upmixed or auto-downmixed) and
    ffmpeg auto-resamples unsupported source rates to an Opus-legal 48 kHz."""
    try:
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(src)]
        if start_time is not None:
            cmd += ["-ss", f"{start_time:.6f}"]
        if end_time is not None:
            cmd += ["-to", f"{end_time:.6f}"]
        cmd += ["-map", "0:a", "-c:a", "libopus",
                "-b:a", f"{bitrate}k", "-vbr", "on",
                "-f", "opus", "-y", str(dst)]  # force the muxer: dst may be *.part
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"    ffmpeg error: {result.stderr[-300:].strip()}")
            if dst.exists():
                dst.unlink()
            return False
        return True
    except FileNotFoundError:
        print("    ERROR: ffmpeg not found. Install it: sudo apt install ffmpeg")
        return False
    except Exception as e:
        print(f"    ERROR: {e}")
        return False


def _encode(src: Path, dst: Path, profile: EncodeProfile,
            start_time: float | None = None,
            end_time: float | None = None) -> bool:
    """Run the encoder for *profile*, returning success. No tagging/validation —
    that is convert_audio()'s job."""
    if profile.fmt == "opus":
        return _opus_convert(src, dst, profile.opus_bitrate, start_time, end_time)
    # MP3: prefer the reference lame pipe (correct gapless header) over ffmpeg.
    if _has_lame_binary():
        return _lame_pipe(src, dst, profile.lame_args, start_time, end_time)
    print("    NOTE: `lame` not found — using ffmpeg (gapless header will be "
          "incomplete; install lame for gapless output)")
    return _ffmpeg_mp3(src, dst, profile.lame_args, start_time, end_time)


def convert_audio(src: Path, dst: Path, profile: EncodeProfile,
                  start_time: float | None = None,
                  end_time: float | None = None) -> bool:
    """Convert the lossless *src* to *dst* using EncodeProfile *profile*. Returns
    True on success. Encodes, copies the source's tags onto the output (via the
    format-agnostic tagio path for non-MP3), then validates the output: MP3 must
    carry a real LAME/Xing gapless header; Opus must probe as valid Ogg Opus."""
    if not _encode(src, dst, profile, start_time, end_time):
        return False
    _apply_source_tags(src, dst)
    if profile.fmt == "mp3":
        if not has_lame_header(dst):
            print(f"    WARNING: {dst.name} has no valid LAME/Xing gapless header — gapless may break")
    elif not _valid_opus(dst):
        print(f"    WARNING: {dst.name} did not probe as valid Opus")
    return True


def convert_to_mp3(src: Path, dst: Path, bitrate: int,
                   start_time: float | None = None,
                   end_time: float | None = None) -> bool:
    """Back-compat shim: CBR-MP3 convert at *bitrate* kbps. Byte-identical to the
    historical path. New code calls convert_audio() with an EncodeProfile."""
    profile = EncodeProfile("mp3-cbr", "mp3", ".mp3", "",
                            lame_args=("--cbr", "-b", str(bitrate)))
    return convert_audio(src, dst, profile, start_time, end_time)


def _cue_to_secs(ts: str) -> float:
    """Convert CUE MM:SS:FF timestamp to seconds (75 frames/sec)."""
    mm, ss, ff = (int(x) for x in ts.strip().split(":"))
    return mm * 60 + ss + ff / 75.0


def find_cue(flac_path: Path) -> Path | None:
    """Return the .cue file for flac_path (same stem, or any .cue in the same folder)."""
    same_stem = flac_path.with_suffix(".cue")
    if same_stem.exists():
        return same_stem
    cues = sorted(flac_path.parent.glob("*.cue"))
    return cues[0] if cues else None


def parse_cue(cue_path: Path) -> list[dict]:
    """Parse a .cue file, returning a list of track dicts with timing and tag info."""
    tracks = []
    album_artist = album_title = album_year = album_genre = None
    cur: dict | None = None

    try:
        text = cue_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []

    for raw in text.splitlines():
        line  = raw.strip()
        upper = line.upper()

        if upper.startswith("TRACK "):
            if cur is not None:
                tracks.append(cur)
            try:
                num = int(line.split()[1])
            except (IndexError, ValueError):
                num = len(tracks) + 1
            cur = {"track_num": num, "title": None, "artist": None, "start_secs": None}

        elif cur is None:
            if upper.startswith("PERFORMER "):
                album_artist = line.split(None, 1)[1].strip().strip('"')
            elif upper.startswith("TITLE "):
                album_title = line.split(None, 1)[1].strip().strip('"')
            elif upper.startswith("REM DATE "):
                album_year = line.split(None, 2)[-1].strip()[:4]
            elif upper.startswith("REM GENRE "):
                album_genre = line.split(None, 2)[-1].strip().strip('"')

        else:
            if upper.startswith("TITLE "):
                cur["title"] = line.split(None, 1)[1].strip().strip('"')
            elif upper.startswith("PERFORMER "):
                cur["artist"] = line.split(None, 1)[1].strip().strip('"')
            elif upper.startswith("INDEX 01 "):
                cur["start_secs"] = _cue_to_secs(line.split(None, 2)[2].strip())

    if cur is not None:
        tracks.append(cur)

    # Filter out malformed entries and compute end times
    tracks = [t for t in tracks if t["start_secs"] is not None]
    for i, t in enumerate(tracks):
        t["end_secs"] = tracks[i + 1]["start_secs"] if i + 1 < len(tracks) else None
        if t["artist"] is None:
            t["artist"] = album_artist
        t["album_artist"] = album_artist
        t["album_title"]  = album_title
        t["album_year"]   = album_year
        t["album_genre"]  = album_genre

    return tracks


def read_cue_tracks(flac_path: Path) -> list[tuple[Path, dict]] | None:
    """
    If flac_path has an associated .cue file, return one (flac_path, tagdict) per
    track. The tagdicts include _CUE_START/_CUE_END for timed conversion.
    Returns None if no usable .cue is found.
    """
    cue = find_cue(flac_path)
    if not cue:
        return None

    # A single-file CUE (whole album in one FLAC) has exactly one FILE directive.
    # Multi-file CUEs (one FLAC per track) have one FILE per track — skip those,
    # since each FLAC is already its own entry and the CUE is just a playlist.
    try:
        cue_text = cue.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    file_lines = [l for l in cue_text.splitlines()
                  if l.strip().upper().startswith("FILE ")]
    if len(file_lines) != 1:
        return None

    tracks = parse_cue(cue)
    if not tracks:
        return None

    total = len(tracks)
    result = []
    for t in tracks:
        td = {
            "TPE1": t["artist"],
            "ALBUMARTIST": t["album_artist"],
            "TIT2": t["title"] or f"Track {t['track_num']}",
            "TALB": t["album_title"],
            "YEAR": t["album_year"],
            "TCON": t["album_genre"],
            "TRCK": f"{t['track_num']}/{total}",
            "TPOS": None,
            "_CUE_START": t["start_secs"],
            "_CUE_END":   t["end_secs"],
        }
        result.append((flac_path, td))
    return result


def step_convert_lossless(root: Path, dry_run: bool, *,
                          ask_choice=None, profile=None) -> dict:
    """
    Step 0 for standardize: find FLAC/ALAC files in root, convert each to the
    chosen encoding profile in-place, and delete the original.

    *profile* (a profile id or EncodeProfile) is the non-interactive choice used
    by the web/job flow, which passes the library's import_profile — so a
    standardize run can produce Opus. With no profile the CLI prompts for an MP3
    bitrate (prompt_bitrate), preserving the historical interactive behaviour.
    """
    print(f"\n{'=' * 60}")
    print("Step 0: Convert lossless files (FLAC/ALAC)")
    print("=" * 60)

    lossless = find_lossless(root)
    if not lossless:
        print("  No lossless files found.")
        return {"converted": 0, "errors": 0}

    ext_counts = Counter(f.suffix.lower() for f in lossless)
    summary = ", ".join(
        f"{n} {ext.upper().lstrip('.')}" for ext, n in sorted(ext_counts.items())
    )
    print(f"  Found: {summary} ({len(lossless)} total)")
    for f in lossless:
        print(f"    {f.relative_to(root)}")
    print()

    if dry_run:
        print("  (dry run) Would convert the lossless files to the chosen profile.")
        return {"converted": len(lossless), "errors": 0}

    if not _has_ffmpeg():
        print("  ERROR: ffmpeg not found. Install it: sudo apt install ffmpeg")
        print("  Skipping lossless conversion.")
        return {"converted": 0, "errors": len(lossless)}

    if profile is not None:
        prof = encoding.get(profile) if isinstance(profile, str) else profile
    else:
        bitrate = prompt_bitrate(ask_choice=ask_choice)
        if bitrate is None:
            print("  Skipped.")
            return {"converted": 0, "errors": 0}
        prof = EncodeProfile("mp3-cbr", "mp3", ".mp3", "",
                             lame_args=("--cbr", "-b", str(bitrate)))

    stats = {"converted": 0, "errors": 0}
    for src in lossless:
        dst = src.with_suffix(prof.ext)
        if dst.exists():
            print(f"  SKIP {src.name} — {dst.name} already exists")
            stats["errors"] += 1
            continue
        print(f"  {src.name}  ->  {dst.name}  [{prof.label or prof.fmt}]")
        if convert_audio(src, dst, prof):
            src.unlink()
            stats["converted"] += 1
        else:
            stats["errors"] += 1

    print(f"\n  Converted: {stats['converted']}  Errors: {stats['errors']}")
    return stats
