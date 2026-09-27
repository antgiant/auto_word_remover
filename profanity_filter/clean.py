#!/usr/bin/env python
"""
Profanity_Filter - remove flagged profanity from a media file's audio.

End-to-end from a single media file:

  1. transcript  reuse "<name>.json" next to the input, else run Voice_to_Text
                 on the ORIGINAL audio - but only once it's actually known to
                 be needed (see get_transcript() in main()): deferred until
                 step 3's discovery chain either finds a subtitle to resync
                 against or runs out of methods, so a file with no subtitle
                 anywhere is detected BEFORE ever transcribing the original
                 mix, and goes straight to the vocals-stem transcript below
                 instead of transcribing twice.
  2. flag        flag_language.py (this folder) finds every hit + timestamp
  3. backfill    a text subtitle track is cross-checked for words the
                 transcript missed entirely; found ones are timed via the
                 transcript's own word timings where possible (see
                 flag_language.backfill_from_srt). Discovery order, first
                 usable one wins: an embedded text/CC608 track, then a
                 sidecar subtitle FILE next to the input (e.g. "Movie.srt"),
                 then OCR'd from an embedded PGS/VobSub bitmap track, then
                 (last resort) fetched from OpenSubtitles. A sidecar file is
                 treated exactly like an OpenSubtitles download, not like an
                 embedded track: it did not necessarily come from THIS
                 exact cut of the file, so its own timestamps are never
                 trusted either - both are realigned to the transcript's own
                 word timings via flag_language.resync_units_to_transcript
                 before use. See AGENTS.md.

                 If EVERY method above comes up empty (see
                 Config.stem_retranscribe), the transcript from step 1 is the
                 only safety net left, so it's worth paying for a second,
                 better-odds attempt at it: the chosen audio track's vocals
                 are isolated with the stemmer (build_vocals_stem, the
                 "Vocals" stem rather than mute_fill's "Instrumental") and
                 re-transcribed into "<name>.vocals.json"
                 (ensure_vocals_transcript). If step 1 never ran at all (no
                 candidate was found anywhere, so get_transcript() was never
                 called), this vocals-stem transcript simply BECOMES step 1's
                 transcript - one Voice_to_Text call total, not two. Only
                 when a candidate WAS found but failed to resync does this
                 run as a genuine second pass, merging in whatever it catches
                 that the first pass missed entirely (scan_vocals_transcript /
                 _dedupe_vocals_hits). No-ops with a warning when no stemmer
                 is installed. On a <=2 channel track, the SAME separator
                 invocation also returns the whole-track Instrumental stem
                 for free (build_vocals_stem's also_instrumental) - step 4's
                 mute_fill="stems" reuses it instead of stemming the track
                 again (see mute_track's cached_whole_instrumental).
  4. remove      ffmpeg pulls ONE audio track out and removes each flagged span:
       method "mute"  (default) - silence. If the track has more than two
                 channels, the center channel's real content is checked first
                 (see detect_center_dominance): when dialogue really does live
                 on the center channel alone, ONLY that channel is muted -
                 music/effects on the other channels play through unbroken.
                 Otherwise, by default (mute_fill = "stems"), the muted span
                 isn't dead air either: a high-quality stemmer (audio-separator,
                 see STEM_VENV) pulls the ambient noise/music out of the whole
                 track ahead of time and that plays through the muted span
                 instead - so even a plain stereo track keeps its room tone/
                 score under a bleep. Set mute_fill = "silence" for the old
                 dead-air behaviour, or if no stemmer is installed (it's the
                 automatic fallback when STEM_VENV isn't found).
       method "bleep" - every channel is muted and a 1 kHz tone laid over it.
       method "cut"   - the flagged span is spliced out entirely, shortening
                 the file. AUDIO-ONLY INPUTS ONLY (e.g. audiobooks) - cutting
                 a video file's audio would desync it from the picture, so a
                 video track refuses this method. There is no "keep the
                 original as an alt track" for cut (the duration changed, so
                 that's not meaningful); the output is the cut file itself,
                 same container/codec as the input, no mkvmerge remux.
                 On an mp3 source this is genuinely LOSSLESS (mp3_splice_cut):
                 every kept region is a plain stream copy, bit-identical to
                 the source - nothing is decoded or re-encoded, only the
                 flagged spans disappear. Every other codec falls back to a
                 decode+re-encode pass (cut_track) since naive concatenation
                 glitches on codecs with inter-frame prediction (AAC, etc.).
                 ID3v2 tags (including cover art and nonstandard frames) are
                 copied from the source onto an mp3 output byte-for-byte via
                 mutagen, not ffmpeg's lossier -map_metadata.
       method "dialog" - a different job entirely: strip ALL dialogue from
                 the track (not just flagged words - the wordlists/transcript
                 aren't even consulted) into a new "(Wordless)" track, added
                 as a non-default alt track alongside the original (see
                 dialog_track_default to make it default instead). Same
                 center-channel-first logic as "mute": if dialogue is clearly
                 isolated on the center channel across the WHOLE file, only
                 that channel is muted and every other channel is untouched
                 (channel count trivially preserved). Otherwise the stemmer
                 strips the dialogue out of every channel and the result -
                 same channel count as the source wherever the stemmer can
                 manage it - becomes the whole output track. See
                 dialog_remove_track / build_instrumental_stem.
  5. subs        (mute/bleep only) every cue that overlaps a removed span has
                 its profane words censored: "mute" removes them entirely
                 (the audio has no audible trace left either), "bleep"
                 replaces them with *** (subs_mask), matching the audible
                 tone. "dialog" leaves subtitles completely alone (there's
                 nothing left to censor, and the removed dialogue may still
                 be worth reading).
  6. remux       (mute/bleep/dialog) mkvmerge muxes the cleaned audio (+
                 cleaned subtitle, mute/bleep only) back in. mute/bleep add it
                 as a new DEFAULT track named "<original label> (Cleaned)";
                 dialog adds "<original label> (Wordless)" as a non-default
                 track by default. Video and all other tracks are copied
                 bit-for-bit.
  7. replace     the source file is REPLACED IN PLACE: once the build above
                 succeeds, the pre-clean original is moved to the Windows
                 Recycle Bin (recoverable, never a hard delete - see
                 send_to_recycle_bin) and the newly built file takes its
                 place at the same path (same folder/stem; the extension
                 changes only for mute/bleep/dialog on a non-.mkv source,
                 since mkvmerge always writes .mkv). Nothing is touched on
                 disk until the build finishes successfully.

Usage:
  clean.py "Movie (2002).mkv"
  clean.py "Movie.mkv" --method bleep --beep-gain-db -8
  clean.py "Audiobook.m4b" --method cut
  clean.py "Movie.mkv" --method dialog
  clean.py "Movie.mkv" --dry-run
  clean.py "Movie.mkv" --force  # rebuild even if a rerun finds the same words already covered

The stemmer (mute_fill="stems", and --method dialog whenever it can't just
mute a center channel) needs a one-time setup - see README.md - and both
features degrade automatically (to plain silence / a clear error) when it
isn't installed.
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
VOICE_TO_TEXT = HERE.parent / "voice_to_text"
GPU_LOCK_DIR = HERE.parent / "gpu_lock"

# Empirically calibrated audio-separator per-invocation cost model, used by
# mute_track() to predict per-span vs. whole-track total wall-clock time and
# pick whichever is actually faster on this machine's GPU, instead of a
# rough word-count proxy. cost(duration) = STEM_STARTUP_S + STEM_RATE *
# duration: STEM_STARTUP_S is the fixed per-invocation cost (ONNX model
# load + CUDA context init + the surrounding ffmpeg channel-extract/fold
# calls) that dominates for short clips; STEM_RATE is the actual chunk-
# processing throughput once past startup, which dominates for long
# (whole-track) inputs. Measured during this rollout: STEM_STARTUP_S=9.0
# from ~9.3s/channel-invocation across dozens of real ~4-5s per-span clips
# from a real movie's per-span run; STEM_RATE=0.10 by holding that same
# startup fixed and solving against three other real whole-track movie runs,
# which independently agreed to within about 10% of each other (0.111, 0.096,
# 0.094). Re-measure both constants if the GPU or stemmer model changes.
STEM_STARTUP_S = 9.0
STEM_RATE = 0.10
SPLICE_CONTEXT_S = 2.0  # padding added on each side of a span before stemming - see splice_stemmed_spans

CODEC_EXT = {"flac": ".mka", "ac3": ".ac3", "eac3": ".eac3", "aac": ".m4a"}

# "cut" method: re-encode with whatever the source audio codec already is (the
# output KEEPS the source file's own extension/container - an audiobook.m4b
# comes back as audiobook (Cleaned).m4b, just shorter).
# {ffprobe codec_name: (ffmpeg encoder, lossless?)}
CUT_CODEC_MAP = {
    "mp3": ("libmp3lame", False), "aac": ("aac", False),
    "vorbis": ("libvorbis", False), "opus": ("libopus", False),
    "flac": ("flac", True), "alac": ("alac", True),
    "ac3": ("ac3", False), "eac3": ("eac3", False),
    "pcm_s16le": ("pcm_s16le", True), "pcm_s24le": ("pcm_s24le", True),
    "pcm_f32le": ("pcm_f32le", True),
}
CUT_FALLBACK_CODEC = ("libmp3lame", False)  # unrecognised source codec

# ffmpeg's standard channel layouts (`ffmpeg -layouts`) that occur in real
# movie audio, channel order as ffmpeg defines it. Used to locate the center
# (dialogue) channel by name rather than guessing an index from a bare count.
CHANNEL_LAYOUTS = {
    "mono": ["FC"], "stereo": ["FL", "FR"], "2.1": ["FL", "FR", "LFE"],
    "3.0": ["FL", "FR", "FC"], "3.0(back)": ["FL", "FR", "BC"],
    "4.0": ["FL", "FR", "FC", "BC"],
    "quad": ["FL", "FR", "BL", "BR"], "quad(side)": ["FL", "FR", "SL", "SR"],
    "3.1": ["FL", "FR", "FC", "LFE"],
    "5.0": ["FL", "FR", "FC", "BL", "BR"], "5.0(side)": ["FL", "FR", "FC", "SL", "SR"],
    "4.1": ["FL", "FR", "FC", "LFE", "BC"],
    "5.1": ["FL", "FR", "FC", "LFE", "BL", "BR"],
    "5.1(side)": ["FL", "FR", "FC", "LFE", "SL", "SR"],
    "6.0": ["FL", "FR", "FC", "BC", "SL", "SR"],
    "6.0(front)": ["FL", "FR", "FLC", "FRC", "SL", "SR"],
    "6.1": ["FL", "FR", "FC", "LFE", "BC", "SL", "SR"],
    "6.1(back)": ["FL", "FR", "FC", "LFE", "BL", "BR", "BC"],
    "6.1(front)": ["FL", "FR", "LFE", "FLC", "FRC", "SL", "SR"],
    "7.0": ["FL", "FR", "FC", "BL", "BR", "SL", "SR"],
    "7.0(front)": ["FL", "FR", "FC", "FLC", "FRC", "SL", "SR"],
    "7.1": ["FL", "FR", "FC", "LFE", "BL", "BR", "SL", "SR"],
    "7.1(wide)": ["FL", "FR", "FC", "LFE", "BL", "BR", "FLC", "FRC"],
    "7.1(wide-side)": ["FL", "FR", "FC", "LFE", "FLC", "FRC", "SL", "SR"],
}

OPENSUBS_TRACK_SUFFIX = " (OpenSubtitles)"  # the extra uncensored track remux() adds for an
#                                             OpenSubtitles-sourced subtitle (see main()) - not a
#                                             Config field since, unlike track_name_suffix, it's not
#                                             meant to be user-tunable, just recognised on rerun
#                                             (is_own_output_track) so it's replaced, not duplicated
SIDECAR_TRACK_SUFFIX = " (Sidecar)"  # same idea as OPENSUBS_TRACK_SUFFIX, for a sidecar subtitle
#                                       file next to the input - see find_sidecar_subtitle/main()

SIDECAR_SUB_EXTS = (".srt", ".vtt", ".ass", ".ssa")  # ffmpeg converts .ass/.ssa to plain SRT text
#                                                        before resyncing; .srt/.vtt parse as-is
#                                                        (flag_language.parse_srt's timestamp regex
#                                                        already accepts either decimal separator).
#                                                        Listed in the order preferred when two
#                                                        otherwise-tied sidecar candidates are found.

LANG_NAMES = {
    "eng": "English", "spa": "Spanish", "fre": "French", "fra": "French",
    "ger": "German", "deu": "German", "ita": "Italian", "jpn": "Japanese",
    "por": "Portuguese", "rus": "Russian", "chi": "Chinese", "zho": "Chinese",
    "kor": "Korean", "dut": "Dutch", "nld": "Dutch", "und": "",
}

# 2-letter -> 3-letter, for matching a sidecar subtitle's "<stem>.<lang>.srt"
# tag (commonly 2-letter) against a track's 3-letter language property -
# same pairs as opensubtitles.py's LANG_2TO3 (kept separate: this module
# doesn't otherwise depend on opensubtitles.py, and the OCR/sidecar/audio
# paths all key on the 3-letter form already).
LANG_2TO3 = {
    "en": "eng", "es": "spa", "fr": "fre", "de": "ger", "it": "ita",
    "ja": "jpn", "pt": "por", "ru": "rus", "zh": "chi", "ko": "kor", "nl": "dut",
}

try:
    import tomllib  # py3.11+
except ModuleNotFoundError:  # pragma: no cover
    try:
        import tomli as tomllib  # py3.10 and earlier: pip install tomli
    except ModuleNotFoundError:
        tomllib = None


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------
@dataclasses.dataclass
class Config:
    method: str = "mute"               # "mute" (silence, center-channel-aware) | "bleep" | "cut"
    categories: list = dataclasses.field(default_factory=lambda: ["profanity"])
    pad_start: float = 0.10
    pad_end: float = 0.10
    merge_gap: float = 0.20
    beep_hz: int = 1000
    beep_gain_db: float = -6.0         # tone peak level, dBFS ("bleep" only)
    clean_codec: str = "ac3"           # ac3 | eac3 | aac | flac (lossless)
    clean_bitrate: str = "224k"        # lossy codecs only, for a <=2ch source
    clean_bitrate_surround: str = "448k"  # lossy codecs only, for a >2ch source (unless --clean-bitrate given)
    center_margin_db: float = 6.0      # "mute": center must be this many dB louder than
    #                                    every other channel, during the flagged spans, to
    #                                    be treated as dialogue-only-on-center
    source_track: str = "default"      # "default" | index | 3-letter language
    track_name_suffix: str = " (Cleaned)"
    sync_ms: int = 0                   # mkvmerge --sync for the clean track

    mute_fill: str = "stems"           # "mute" only, whenever there's no clean center channel to
    #                                    mute alone (stereo/mono, or a >2ch track where dialogue
    #                                    isn't center-only): "stems" (default) plays the stemmed-out
    #                                    ambient noise/music through the muted span instead of dead
    #                                    air; "silence" is the old behaviour.
    stem_model: str = "UVR-MDX-NET-Inst_HQ_3.onnx"  # audio-separator model, Vocals/Instrumental stems

    dialog_track_suffix: str = " (Wordless)"
    dialog_track_default: bool = False  # "dialog": make the new (Wordless) track the default audio

    cut_bitrate: str = "96k"           # "cut" only, lossy source codecs (audiobooks are low-bitrate speech)

    subs_track: str = "default"        # SubRip track to clean: "default"|index|lang|"none"
    subs_mask: str = "***"             # what bleeped words become in the subtitle
    subs_pad: float = 0.15            # s of slack when matching cues to bleep spans
    srt_backfill: bool = True          # also cross-check the embedded SRT for missed words

    sidecar_subs: bool = True          # when there's no usable embedded text/CC608 track, look for
    #                                    an external subtitle FILE sitting next to the input (e.g.
    #                                    "Movie.srt", "Movie.en.srt") before falling back to OCR'ing
    #                                    an embedded bitmap track. Unlike an embedded track, a
    #                                    sidecar isn't assumed to already match this exact file's
    #                                    cut - it's treated exactly like an OpenSubtitles download
    #                                    (resynced against the transcript's own word timings via
    #                                    flag_language.resync_units_to_transcript, added as its own
    #                                    extra "(Sidecar)" track - see find_sidecar_subtitle/main()).
    sidecar_lang: str = ""             # 2- or 3-letter language to prefer when more than one sidecar
    #                                    file exists ("" = derive from the chosen audio track's own
    #                                    language)

    pgs_ocr: bool = True               # when there's no text/sidecar subtitle but there IS a PGS
    #                                    (Blu-ray bitmap) one, OCR it into a real SRT (see pgs_ocr.py)
    #                                    and use that for srt_backfill/censoring instead of skipping
    #                                    subtitles entirely. Needs Tesseract - see locate_tesseract().
    pgs_ocr_lang: str = ""             # Tesseract language code, "" = derive from the track's own
    #                                    3-letter language tag (falls back to "eng" if unrecognised)

    vobsub_ocr: bool = True            # same idea as pgs_ocr, for VobSub (DVD bitmap) tracks - only
    #                                    tried when there's no text/sidecar track AND no PGS track
    #                                    either (see vobsub_ocr.py; main()'s subtitle-discovery order)
    vobsub_ocr_lang: str = ""          # Tesseract language code, "" = derive from the track's own
    #                                    3-letter language tag (falls back to "eng" if unrecognised)

    opensubtitles: bool = True         # last resort in the subtitle-discovery chain, tried only when
    #                                    there's no usable text/CC608/sidecar/PGS/VobSub subtitle at
    #                                    all (the common case for a TV recording). Needs an API key -
    #                                    opensubtitles.py's docstring / README "OpenSubtitles setup".
    #                                    The fetched file's own timestamps are NEVER trusted - see
    #                                    flag_language.resync_units_to_transcript(). On success, adds
    #                                    TWO new subtitle tracks (unlike every other source above,
    #                                    which already has an "original" passing through untouched):
    #                                    "<lang> (OpenSubtitles)" (uncensored, non-default) and
    #                                    "<lang> (OpenSubtitles) (Cleaned)" (censored, default).
    opensubtitles_lang: str = "en"     # 2-letter language to search/download ("" = derive from the
    #                                    chosen audio track's own language)
    opensubtitles_query: str = ""      # override the title auto-guessed from the filename
    opensubtitles_id: str = ""         # exact OpenSubtitles file_id to download - bypasses search

    stem_retranscribe: bool = True     # absolute last resort, tried only when the ENTIRE subtitle-
    #                                    discovery chain above (embedded/sidecar/PGS/VobSub/
    #                                    OpenSubtitles) came up with nothing at all - meaning the
    #                                    transcript is the only detection safety net this file has.
    #                                    Isolates vocals out of the chosen audio track (the stemmer's
    #                                    "Vocals" stem, not "Instrumental" - see build_vocals_stem)
    #                                    and re-transcribes just that, cached as "<name>.vocals.json"
    #                                    next to the input. Measured (voice_to_text/AGENTS.md,
    #                                    "Reducing the Whisper miss rate") to recover real misses -
    #                                    dialogue masked by music/effects that Whisper never decodes
    #                                    from the original mixed track at all - worth the extra
    #                                    transcription pass specifically here, where any independent
    #                                    catch matters most, even though it's too costly to run as a
    #                                    library-wide default. No-ops (with a warning) when no
    #                                    stemmer is installed - see locate_stem_tool().
    stem_retranscribe_min_gain_s: float = 1.0  # a vocals-stem hit within this many seconds of an
    #                                    already-found hit (same word) is treated as the same
    #                                    occurrence, not a new catch - see _dedupe_vocals_hits()

    output_dir: str = "out"             # scratch dir for temp files + --keep-temp debug artifacts only -
    #                                    the cleaned result itself replaces the source in place (see
    #                                    send_to_recycle_bin); this is NOT where it ends up
    retranscribe: bool = False
    overwrite: bool = False


def load_config(path: Path) -> Config:
    cfg = Config()
    if path.is_file() and tomllib is not None:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        known = {f.name for f in dataclasses.fields(Config)}
        for key, val in data.items():
            if key in known:
                setattr(cfg, key, val)
            else:
                print(f"[config] ignoring unknown key: {key}", file=sys.stderr)
    return cfg


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------
def _find(name: str, extra_dirs=()) -> str | None:
    for d in extra_dirs:
        for cand in (Path(d) / f"{name}.exe", Path(d) / name):
            if cand.is_file():
                return str(cand)
    return shutil.which(name)


# A dedicated venv for the stemmer (audio-separator, which pulls in torch/
# onnxruntime) so its dependency pins never touch the venv clean.py itself
# runs under, or any other tool's torch build. Not auto-installed; see
# README for the one-time setup command. Used by `mute_fill = "stems"` and
# `--method dialog`; both degrade gracefully (silence / a hard error,
# respectively) when it's missing.
STEM_VENV = HERE / ".venv-stem"


def locate_stem_tool() -> str | None:
    exe = STEM_VENV / "Scripts" / "audio-separator.exe"
    return str(exe) if exe.is_file() else None


def locate_tesseract() -> str | None:
    """Tesseract OCR binary for pgs_ocr.py (PGS bitmap-subtitle OCR fallback -
    see Config.pgs_ocr). Checked at fixed install locations first, not just
    PATH: a winget install updates the registry-level user PATH, which an
    already-running shell/process won't see until it restarts - shutil.which
    is still tried last as a fallback for a PATH-based install."""
    for cand in (HERE / "bin" / "tesseract.exe",
                Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
                Path.home() / "AppData" / "Local" / "Programs" / "Tesseract-OCR" / "tesseract.exe"):
        if cand.is_file():
            return str(cand)
    return shutil.which("tesseract")


def locate_tools() -> dict:
    bins = [HERE / "bin", HERE / "bin" / "mkvtoolnix"]
    mkv_dirs = bins + [Path(r"C:\Program Files\MKVToolNix"),
                       Path(r"C:\Program Files (x86)\MKVToolNix")]
    tools = {
        "ffmpeg": _find("ffmpeg", bins),
        "ffprobe": _find("ffprobe", bins),
        "mkvmerge": _find("mkvmerge", mkv_dirs),
        "mkvextract": _find("mkvextract", mkv_dirs),
    }
    missing = [n for n, v in tools.items() if not v]
    if missing:
        raise SystemExit(
            f"[error] tool(s) not found: {', '.join(missing)}\n"
            f"        install MKVToolNix / ffmpeg on PATH, or drop the .exe(s) "
            f"in {HERE / 'bin'}")
    return tools


def _q(s) -> str:
    s = str(s)
    return f'"{s}"' if (" " in s or not s) else s


def run(cmd: list, **kw) -> subprocess.CompletedProcess:
    print("  $ " + " ".join(_q(c) for c in cmd), flush=True)
    return subprocess.run(cmd, check=True, **kw)


def run_mkvmerge(cmd: list) -> None:
    # mkvmerge's own exit codes: 0 = success, 1 = success but warnings were
    # issued, 2 = real error. Treating 1 as fatal (like a generic check=True
    # run() call would) causes false failures on multiplexes that actually
    # completed fine - this bit us repeatedly on real movies.
    print("  $ " + " ".join(_q(c) for c in cmd), flush=True)
    result = subprocess.run(cmd)
    if result.returncode == 1:
        print("  [warn] mkvmerge exited 1 (warnings only, multiplexing completed) - continuing",
              file=sys.stderr)
    elif result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, cmd)


def run_json(cmd: list) -> dict:
    out = subprocess.run(cmd, check=True, capture_output=True, text=True,
                         encoding="utf-8", errors="replace").stdout
    return json.loads(out)


def send_to_recycle_bin(path: Path) -> None:
    """Move `path` to the Windows Recycle Bin (recoverable) instead of
    permanently deleting it, via the shell32 SHFileOperationW API. Kept in
    ctypes/stdlib rather than adding a `send2trash` dependency, matching this
    project's no-pip-install-needed design. Windows-only, same as the rest of
    this toolkit."""
    import ctypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", ctypes.c_void_p),
            ("wFunc", ctypes.c_uint),
            ("pFrom", ctypes.c_wchar_p),
            ("pTo", ctypes.c_wchar_p),
            ("fFlags", ctypes.c_uint16),
            ("fAnyOperationsAborted", ctypes.c_int),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", ctypes.c_wchar_p),
        ]

    FO_DELETE = 0x0003
    FOF_ALLOWUNDO = 0x0040       # send to Recycle Bin instead of a hard delete
    FOF_NOCONFIRMATION = 0x0010
    FOF_SILENT = 0x0004
    FOF_NOERRORUI = 0x0400

    # pFrom must be double-null-terminated for SHFileOperationW; ctypes'
    # c_wchar_p marshalling appends its own trailing null on top of the one
    # embedded here, satisfying that even for a single path.
    op = SHFILEOPSTRUCTW(
        hwnd=None, wFunc=FO_DELETE, pFrom=str(path) + "\0", pTo=None,
        fFlags=FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI,
        fAnyOperationsAborted=0, hNameMappings=None, lpszProgressTitle=None,
    )
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if result != 0 or op.fAnyOperationsAborted:
        raise OSError(f"could not move {path} to the Recycle Bin "
                     f"(SHFileOperationW code {result})")


def probe_streams(ffprobe: str, media: Path) -> list[dict]:
    return run_json([ffprobe, "-v", "error", "-show_streams", "-of", "json", str(media)])["streams"]


def has_video_track(streams: list[dict]) -> bool:
    """A real video stream - not just embedded cover art (attached_pic), which
    mp3/m4a/m4b audiobooks routinely carry and which is not a reason to treat
    the file as video."""
    return any(s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")
              for s in streams)


# ---------------------------------------------------------------------------
# steps
# ---------------------------------------------------------------------------
def ensure_transcript(media: Path, cfg: Config) -> Path:
    js = media.with_suffix(".json")
    if js.is_file() and not cfg.retranscribe:
        print(f"  transcript: {js.name} (reusing)")
        return js
    py = VOICE_TO_TEXT / ".venv" / "Scripts" / "python.exe"
    script = VOICE_TO_TEXT / "transcribe.py"
    if not py.is_file() or not script.is_file():
        raise SystemExit(f"[error] no '{js.name}' next to the input and "
                         f"Voice_to_Text not found at {VOICE_TO_TEXT}")
    print(f"  transcript: running Voice_to_Text on {media.name} ...")
    run([str(py), "-X", "utf8", str(script), str(media),
         "--formats", "json", "--no-diarize"], cwd=str(VOICE_TO_TEXT))
    if not js.is_file():
        raise SystemExit("[error] transcription produced no .json")
    return js


def load_matchers(cfg: Config) -> dict:
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    import flag_language  # lives in this folder
    return flag_language.load_matchers(only=set(cfg.categories))


def load_extra_spans(path: Path) -> list[dict]:
    """Hand-reviewed spans that no wordlist can safely catch (e.g. sexual
    content using otherwise-ordinary words) - a JSON list of
    {"start": seconds, "end": seconds, "label": "...", "category": "..."}
    in the TARGET FILE's own local timeline. Turned into hit-shaped dicts so
    they flow through the exact same padding/merge/report path as a real
    wordlist hit; unlike wordlist hits they're never filtered by
    --categories, since picking one by hand already *is* the categorisation."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    hits = []
    for e in data:
        label = e.get("label") or e.get("text") or "manual"
        hits.append({
            "time": float(e["start"]), "end": float(e.get("end", e["start"] + 0.6)),
            "categories": [e.get("category", "manual")], "match": label,
            "context": e.get("text", label), "speaker": None,
            "locator": "manual", "source": "manual",
        })
    return hits


def _dedupe_vocals_hits(primary_hits: list[dict], vocals_hits: list[dict],
                        window: float) -> list[dict]:
    """Keep only vocals-stem hits (see scan_vocals_transcript) that don't
    already have a same-word hit in `primary_hits` within `window` seconds -
    the point of the vocals-stem rescan is to catch words the ORIGINAL
    (mixed-audio) transcript - and any srt backfill already merged into
    `primary_hits` - missed entirely, not to duplicate what's already
    found."""
    kept = []
    for vh in vocals_hits:
        vt = vh["time"]
        if vt is None:
            continue
        dup = any(ph["time"] is not None and abs(ph["time"] - vt) <= window
                  and ph["match"].lower() == vh["match"].lower() for ph in primary_hits)
        if not dup:
            kept.append(vh)
    return kept


def find_spans(js: Path, cfg: Config, matchers: dict, srt_path: Path | None = None,
               extra_hits: list[dict] | None = None, vocals_hits: list[dict] | None = None):
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    import flag_language

    cats = set(cfg.categories)
    hits, _fmt = flag_language.scan_file(js, matchers)

    if cfg.srt_backfill and srt_path is not None and srt_path.is_file():
        try:
            words = flag_language.load_word_timeline(js)
            extra = flag_language.backfill_from_srt(srt_path, matchers, words)
            n_before = len(hits)
            hits = hits + extra
            if extra:
                print(f"  srt backfill: +{len(extra)} word(s) the transcript missed entirely "
                      f"({n_before} -> {len(hits)})")
        except Exception as exc:
            print(f"  [warn] srt backfill failed ({exc!r})", file=sys.stderr)

    if vocals_hits:
        new_from_vocals = _dedupe_vocals_hits(hits, vocals_hits, cfg.stem_retranscribe_min_gain_s)
        if new_from_vocals:
            hits = hits + new_from_vocals
            print(f"  vocals-stem rescan: +{len(new_from_vocals)} word(s) the original transcript "
                  f"missed entirely (isolated-vocals re-transcription)")
        else:
            print("  vocals-stem rescan: found nothing the original transcript hadn't already caught")

    extra_hits = extra_hits or []
    if extra_hits:
        print(f"  extra spans: +{len(extra_hits)} hand-reviewed span(s) not from a wordlist")
    all_hits = hits + extra_hits

    raw = []
    for h in hits:                        # wordlist-driven hits respect --categories
        if not set(h["categories"]) & cats:
            continue
        st = h["time"]
        if st is None:
            continue
        en = h["end"] if h["end"] is not None else st + 0.6
        raw.append([max(0.0, st - cfg.pad_start), en + cfg.pad_end, [h]])
    for h in extra_hits:                  # hand-reviewed: always included
        st = h["time"]
        en = h["end"] if h["end"] is not None else st + 0.6
        raw.append([max(0.0, st - cfg.pad_start), en + cfg.pad_end, [h]])
    raw.sort(key=lambda r: (r[0], r[1]))

    merged: list[list] = []
    for s, e, hs in raw:
        if merged and s - merged[-1][1] <= cfg.merge_gap:
            merged[-1][1] = max(merged[-1][1], e)
            merged[-1][2] += hs
        else:
            merged.append([s, e, list(hs)])
    return merged, all_hits


def _pick(pool: list, want: str, label: str):
    """Pick one track from `pool` by 'default' / index / language."""
    if want == "default":
        return next((t for t in pool if t["properties"].get("default_track")), pool[0])
    if str(want).isdigit():
        i = int(want)
        if i >= len(pool):
            raise SystemExit(f"[error] {label} track {i} out of range (0..{len(pool) - 1})")
        return pool[i]
    t = next((t for t in pool
              if (t["properties"].get("language", "") or "").lower() == want.lower()), None)
    if not t:
        raise SystemExit(f"[error] no {label} track with language '{want}'")
    return t


def choose_audio(tracks: list, want: str):
    audio = [t for t in tracks if t["type"] == "audio"]
    if not audio:
        raise SystemExit("[error] input has no audio tracks")
    # Never treat a "(No Narration)"/"(Wordless)" alt track as the thing to
    # detect profanity in or default to - even if IT'S the one flagged
    # default in the container, or explicitly requested by index/language
    # (see no_narration_batch.py) - prefer a real narrated track when one
    # exists alongside it. Applies to every selection mode, not just
    # "default": a no-narration track is never a valid cleaning source.
    narrated = [t for t in audio if not is_no_narration_track(t)]
    candidates = narrated or audio   # only a file that's ENTIRELY no-narration tracks falls back
    if want == "default":
        # Don't just trust the container's default-track flag - a rip's default
        # is often an arbitrary/compatibility stereo track sitting next to a
        # real 5.1+ track of the same language that never gets touched. Prefer
        # the highest-channel-count track among whatever language the default
        # pick would have used.
        default_t = next((t for t in candidates if t["properties"].get("default_track")), candidates[0])
        lang = (default_t["properties"].get("language") or "").lower()
        same_lang = [t for t in candidates
                     if (t["properties"].get("language") or "").lower() == lang] if lang else candidates
        t = max(same_lang, key=lambda x: x["properties"].get("audio_channels") or 0)
        if (t["properties"].get("audio_channels") or 0) > (default_t["properties"].get("audio_channels") or 0):
            print(f"  [note] source_track=default: using the "
                  f"{t['properties'].get('audio_channels')}ch track (id {t['id']}) instead of the "
                  f"{default_t['properties'].get('audio_channels')}ch one flagged default (id {default_t['id']}) "
                  f"- preferring higher channel count")
    else:
        t = _pick(candidates, want, "audio")
    return t, audio.index(t)


def choose_subs(tracks: list, want: str, prefer_lang: str = ""):
    """The text subtitle track to clean, or (None, None) to skip. Accepts
    Matroska SubRip tracks (extracted via mkvextract) and MP4 "Timed Text"/
    tx3g/mov_text tracks (extracted via ffmpeg's built-in mov_text->srt
    conversion) - see extract_subs_srt(). Image-based subtitle formats
    (PGS/VobSub) aren't text, so there's nothing to censor - excluded."""
    if str(want).lower() in ("none", "skip", "off", ""):
        return None, None
    srt = [t for t in tracks if t["type"] == "subtitles"
           and ((t["properties"].get("codec_id") or "").upper().startswith("S_TEXT/UTF8")
                or (t.get("codec") or "").lower() == "timed text")]
    if not srt:
        return None, None
    if want == "default":
        # Don't just trust the container's default-track flag (or, failing
        # that, whichever track happens to sit first) - a disc/rip's default
        # subtitle flag reflects the AUTHORING STUDIO's own pick (routinely a
        # French/dub-market default even on an English-audio release), not
        # the language actually being cleaned. Mirrors choose_audio()'s same
        # non-trust of the raw default flag. Prefer a track whose language
        # matches the audio track already chosen for cleaning; only fall
        # back to the raw default-flag/first-track pick when nothing matches.
        lang = (prefer_lang or "").lower()
        same_lang = [t for t in srt if (t["properties"].get("language", "") or "").lower() == lang] if lang else []
        pool = same_lang or srt
        t = next((x for x in pool if x["properties"].get("default_track")), pool[0])
    else:
        t = _pick(srt, want, "subtitle")
    return t, t["id"]


def choose_pgs(tracks: list, want: str):
    """The image-based (PGS/Blu-ray) subtitle track to feed pgs_ocr.py, or
    (None, None) if there isn't one - see Config.pgs_ocr."""
    if str(want).lower() in ("none", "skip", "off", ""):
        return None, None
    pgs = [t for t in tracks if t["type"] == "subtitles"
           and (t["properties"].get("codec_id") or "").upper() == "S_HDMV/PGS"]
    if not pgs:
        return None, None
    t = _pick(pgs, want, "subtitle")
    return t, t["id"]


def choose_vobsub(tracks: list, want: str):
    """The image-based (VobSub/DVD) subtitle track to feed vobsub_ocr.py, or
    (None, None) if there isn't one - see Config.vobsub_ocr. Checked only
    after choose_pgs finds nothing (see main()'s subtitle-discovery order) -
    PGS is the newer, higher-resolution format, so it wins when a source
    somehow has both."""
    if str(want).lower() in ("none", "skip", "off", ""):
        return None, None
    vobsub = [t for t in tracks if t["type"] == "subtitles"
             and (t["properties"].get("codec_id") or "").upper() == "S_VOBSUB"]
    if not vobsub:
        return None, None
    t = _pick(vobsub, want, "subtitle")
    return t, t["id"]


def find_cc608_track(ffprobe: str, media: Path, want_lang: str | None) -> dict | None:
    """mkvmerge silently drops MP4 CEA-608 closed-caption tracks (codec
    eia_608/c608) from its track list entirely - some rips (e.g. iTunes
    purchases) only carry real dialogue there, with any mov_text track
    present being just a near-empty "forced" placeholder (confirmed on a
    real iTunes movie rip). Probe with ffprobe instead and
    return a synthetic track dict flagged with '_cc608_pos', the ffmpeg
    subtitle-stream position needed to extract it - or None if there isn't
    one."""
    streams = probe_streams(ffprobe, media)
    subs = [s for s in streams if s.get("codec_type") == "subtitle"]
    candidates = [(i, s) for i, s in enumerate(subs)
                 if "608" in (s.get("codec_name") or "").lower()]
    if not candidates:
        return None
    if want_lang:
        by_lang = [(i, s) for i, s in candidates
                  if (s.get("tags", {}).get("language") or "").lower() == want_lang.lower()]
        if by_lang:
            candidates = by_lang
    i, s = candidates[0]
    lang = s.get("tags", {}).get("language") or "und"
    return {"id": None, "type": "subtitles", "codec": "EIA-608",
            "properties": {"language": lang, "track_name": None},
            "_cc608_pos": i}


def find_sidecar_subtitle(media: Path, want_lang: str) -> tuple[Path, str] | None:
    """An external subtitle FILE sitting next to `media` (same stem) - e.g.
    "Movie.srt" or "Movie.en.srt" - the sidecar rung of the subtitle-
    discovery chain (see Config.sidecar_subs / main()'s discovery order):
    tried after an embedded text/CC608 track and before falling back to
    OCR'ing an embedded bitmap (PGS/VobSub) track.

    Only two naming patterns are recognised: "<stem><ext>" (no language
    tag) and "<stem>.<lang><ext>" for a bare 2- or 3-letter language code
    (e.g. "Movie.en.srt", "Movie.eng.srt"). `ext` must be one of
    SIDECAR_SUB_EXTS. Returns (path, 3-letter language - "und" if
    untagged) for whichever candidate ranks best, or None if there's no
    sidecar at all. Ranking: a language tag matching `want_lang` first,
    then an untagged file, then any other language; ties beyond that
    resolve by extension preference (SIDECAR_SUB_EXTS order) then name.

    Note that finding a sidecar here says nothing about whether it's
    trustworthy for THIS file's exact cut/timing - see the resync step in
    main() and SIDECAR_TRACK_SUFFIX."""
    stem = media.stem
    try:
        entries = list(media.parent.iterdir())
    except OSError:
        return None

    candidates: list[tuple[Path, str]] = []  # (path, 3-letter lang or "und")
    for p in entries:
        if p == media or not p.is_file():
            continue
        suf = p.suffix.lower()
        if suf not in SIDECAR_SUB_EXTS:
            continue
        name_no_ext = p.name[:-len(suf)]
        if name_no_ext == stem:
            candidates.append((p, "und"))
        elif name_no_ext.startswith(stem + "."):
            tag = name_no_ext[len(stem) + 1:].lower()
            if re.fullmatch(r"[a-z]{2,3}", tag):
                lang3 = tag if len(tag) == 3 else LANG_2TO3.get(tag, tag)
                candidates.append((p, lang3))
    if not candidates:
        return None

    want3 = (LANG_2TO3.get(want_lang.lower(), want_lang.lower()) if len(want_lang) == 2
             else want_lang.lower()) if want_lang else ""
    ext_rank = {ext: i for i, ext in enumerate(SIDECAR_SUB_EXTS)}

    def score(c: tuple[Path, str]):
        p, lang3 = c
        lang_rank = 0 if (want3 and lang3 == want3) else (1 if lang3 == "und" else 2)
        return (lang_rank, ext_rank.get(p.suffix.lower(), 9), p.name)

    candidates.sort(key=score)
    return candidates[0]


_MIN_USABLE_SRT_CHARS = 200  # below this, treat an extraction as an empty/placeholder track


def srt_text_len(srt_path: Path) -> int:
    """Rough count of actual dialogue characters in an SRT, ignoring cue
    numbers, timestamps, and markup - the only way to tell a real subtitle
    track apart from a near-empty placeholder is by what's actually in it,
    which is only known after extraction (see find_cc608_track's
    docstring)."""
    text = srt_path.read_text(encoding="utf-8-sig", errors="replace")
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\{[^}]*\}", "", text)
    lines = [ln for ln in text.splitlines()
            if ln.strip() and "-->" not in ln and not ln.strip().isdigit()]
    return sum(len(ln.strip()) for ln in lines)


def extract_subs_srt(ffmpeg: str, mkvextract: str, media: Path, tracks: list,
                     chosen_subs: dict, subs_tid: int, out_srt: Path) -> None:
    """Matroska SubRip tracks extract directly with mkvextract; MP4 "Timed
    Text"/tx3g tracks and CEA-608 closed-caption tracks aren't Matroska SRT,
    so mkvextract can't pull them - use ffmpeg's built-in conversion
    instead, mapping by the track's position among subtitle streams
    (ffmpeg's -map indexes per stream type, not by mkvmerge's global track
    id - and CEA-608 tracks aren't in mkvmerge's list at all, see
    find_cc608_track)."""
    if "_cc608_pos" in chosen_subs:
        run([ffmpeg, "-hide_banner", "-y", "-i", str(media),
             "-map", f"0:s:{chosen_subs['_cc608_pos']}", str(out_srt)])
        return
    codec_id = (chosen_subs["properties"].get("codec_id") or "").upper()
    if codec_id.startswith("S_TEXT/UTF8"):
        run([mkvextract, "tracks", str(media), f"{subs_tid}:{out_srt}"])
    else:
        subs = [t for t in tracks if t["type"] == "subtitles"]
        subs_pos = subs.index(chosen_subs)
        run([ffmpeg, "-hide_banner", "-y", "-i", str(media),
             "-map", f"0:s:{subs_pos}", str(out_srt)])


def stage_sidecar_srt(ffmpeg: str, sidecar: Path, tmp: Path) -> Path:
    """Normalise a sidecar subtitle file to plain "-->"-cue SRT text so
    flag_language.resync_units_to_transcript (which parses with parse_srt)
    can read it. .srt/.vtt already parse as-is (parse_srt's timestamp regex
    accepts either decimal separator) - copied into `tmp` anyway so callers
    always get a fresh, disposable path. .ass/.ssa use a completely
    different cue syntax, so those go through ffmpeg's built-in conversion
    first, same idea as extract_subs_srt's embedded-track conversion."""
    out = tmp / f"sidecar_raw{sidecar.suffix.lower()}.srt"
    if sidecar.suffix.lower() in (".srt", ".vtt"):
        shutil.copy2(sidecar, out)
    else:
        run([ffmpeg, "-hide_banner", "-y", "-i", str(sidecar), str(out)])
    return out


def resync_external_srt(raw_srt_path: Path, js: Path, lang3: str, track_suffix: str,
                        synced_path: Path, log_label: str) -> tuple[Path, str] | None:
    """Realign an externally-sourced subtitle - an OpenSubtitles download or
    a sidecar file next to the input (see Config.sidecar_subs) - against the
    transcript's own word timings via flag_language.resync_units_to_
    transcript(), since neither source is guaranteed to already match THIS
    file's exact cut/timing the way an embedded track is. Writes the
    resynced cues to `synced_path` and returns (synced_path, track_name) if
    there's enough usable text left afterwards, else None (a warning is
    printed either way, distinguishing "nothing lined up" from "too little
    text").
    """
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    import flag_language
    cues = flag_language.resync_units_to_transcript(raw_srt_path, flag_language.load_word_timeline(js))
    if not cues:
        print(f"  [warn] {log_label}: nothing in the subtitle lined up with this recording's "
              f"transcript - discarding", file=sys.stderr)
        return None
    flag_language.write_srt_cues(cues, synced_path)
    if srt_text_len(synced_path) < _MIN_USABLE_SRT_CHARS:
        print(f"  [warn] {log_label}: resync produced too little usable text - discarding",
              file=sys.stderr)
        return None
    track_name = f"{LANG_NAMES.get(lang3, lang3) or lang3}{track_suffix}".strip()
    print(f"  {log_label}: resynced {len(cues)} cue(s) to this recording's own timeline")
    return synced_path, track_name


def clean_label(track: dict, suffix: str) -> str:
    p = track["properties"]
    name = p.get("track_name") or ""
    lang = p.get("language") or "und"
    if name:
        return name + suffix
    if LANG_NAMES.get(lang):
        return LANG_NAMES[lang] + suffix
    return suffix.strip()


NO_NARRATION_NAME_MARKERS = ("no narration", "wordless")  # matched case-insensitively against a
#                                                              track's name to recognize the "dialog"
#                                                              method's own narration-free alt track,
#                                                              under either the default "(Wordless)"
#                                                              suffix or a project-specific override
#                                                              (e.g. "(No Narration)" - see
#                                                              no_narration_batch.py). Name substring
#                                                              rather than cfg.dialog_track_suffix so
#                                                              it's recognized regardless of which
#                                                              config produced it.


def is_no_narration_track(t: dict) -> bool:
    name = (t.get("properties", {}) or {}).get("track_name") or ""
    nl = name.lower()
    return any(m in nl for m in NO_NARRATION_NAME_MARKERS)


def is_own_output_track(t: dict, cfg: Config) -> bool:
    """True for an audio/subtitle track this tool itself added on a PREVIOUS
    run of THIS SAME method - its track_name ends with the suffix
    clean_label() gives new tracks for cfg.method ("(Cleaned)" for mute/
    bleed/cut, "(No Narration)"/"(Wordless)" for dialog). Used by main() to
    always re-detect from the true original source on a rerun (never
    re-clean an already-cleaned track) and to replace, rather than pile up
    alongside, a stale one - see "Re-running on an already-cleaned file" in
    AGENTS.md.

    Only the audio suffix that matches cfg.method is checked - a mute/bleed
    run must never treat an existing "(No Narration)" track (built by a
    SEPARATE dialog run, e.g. the no-narration sweep) as its own stale output:
    that used to make stale_ids/exclude_audio_ids drop the no-narration track
    from the rebuilt file entirely. It's a different track type, produced by
    a different method - left alone here, and never chosen as cleaning
    source either (see choose_audio's is_no_narration_track exclusion). The
    subtitle suffixes stay method-independent - opensubtitles/sidecar
    passthrough tracks aren't tied to which audio method is active."""
    name = t.get("properties", {}).get("track_name") or ""
    audio_suffix = cfg.dialog_track_suffix if cfg.method == "dialog" else cfg.track_name_suffix
    return (name.endswith(audio_suffix)
            or name.endswith(OPENSUBS_TRACK_SUFFIX) or name.endswith(SIDECAR_TRACK_SUFFIX))


def _span_expr(spans) -> str:
    return "+".join(f"between(t,{s:.3f},{e:.3f})" for s, e, _ in spans)


def build_bleep_filter(spans, cfg: Config, n_channels: int, sample_rate: int, audio_pos: int) -> str:
    expr = _span_expr(spans)
    layout = {1: "mono", 2: "stereo", 3: "2.1", 4: "quad", 6: "5.1", 8: "7.1"}.get(
        n_channels, f"{n_channels}c")
    pan = "|".join([f"pan={layout}"] + [f"c{i}=c0" for i in range(n_channels)])
    af = f"aformat=sample_fmts=fltp:sample_rates={sample_rate}"
    amp = 10.0 ** (cfg.beep_gain_db / 20.0)  # beep_gain_db is target peak dBFS
    tone = f"aevalsrc=exprs='{amp:.4f}*sin(2*PI*{cfg.beep_hz}*t)':sample_rate={sample_rate}"
    return (
        f"[0:a:{audio_pos}]volume=0:enable='{expr}',{af}[main];\n"
        f"{tone},{pan},volume=0:enable='not({expr})',{af}[beep];\n"
        f"[main][beep]amix=inputs=2:normalize=0:duration=first[out]"
    )


def build_mute_filter(spans, audio_pos: int, n_channels: int,
                      center_idx: int | None, layout_name: str | None) -> str:
    """Silence `spans`. If `center_idx` is given (a recognised named layout
    with a center channel), only that channel is muted - every other channel
    passes through untouched, sample for sample. Otherwise the whole track
    is muted."""
    expr = _span_expr(spans)
    if center_idx is None:
        return f"[0:a:{audio_pos}]volume=0:enable='{expr}'[out]"

    names = CHANNEL_LAYOUTS[layout_name]
    pads = "".join(f"[cs{i}]" for i in range(n_channels))
    lines = [f"[0:a:{audio_pos}]channelsplit=channel_layout={layout_name}{pads};"]
    for i in range(n_channels):
        if i == center_idx:
            lines.append(f"[cs{i}]volume=0:enable='{expr}'[cm{i}];")
        else:
            lines.append(f"[cs{i}]anull[cm{i}];")
    joins = "".join(f"[cm{i}]" for i in range(n_channels))
    chan_map = "|".join(f"{i}.0-{names[i]}" for i in range(n_channels))
    lines.append(f"{joins}join=inputs={n_channels}:channel_layout={layout_name}:map={chan_map}[out]")
    return "\n".join(lines)


_ASTATS_CHANNEL_RE = re.compile(r"Channel:\s*(\d+)")
_ASTATS_RMS_RE = re.compile(r"RMS level dB:\s*(-?[\d.]+|-inf)")


def _parse_astats_rms(stderr_text: str) -> dict[int, float]:
    """{0-based channel index: RMS level dB} from ffmpeg astats stderr text
    (stops at the "Overall" section)."""
    levels: dict[int, float] = {}
    cur: int | None = None
    for line in stderr_text.splitlines():
        m = _ASTATS_CHANNEL_RE.search(line)
        if m:
            cur = int(m.group(1)) - 1
            continue
        if "Overall" in line:
            cur = None
            continue
        if cur is not None:
            r = _ASTATS_RMS_RE.search(line)
            if r:
                levels[cur] = float("-inf") if r.group(1) == "-inf" else float(r.group(1))
    return levels


# ffmpeg's `enable` option runs its value through the AVExpr boolean parser,
# which hard-fails ("Error when evaluating the expression") once a chained
# between(...)+between(...)+... expression passes a fixed term count -
# bisected empirically against a real ffmpeg build: 99 terms parse fine, 100
# fails every time, so this really is a hard-coded limit in ffmpeg itself,
# not a length/performance thing. A span list past this size (one span per
# subtitle cue across a whole episode routinely runs into the hundreds) is
# processed in batches and the per-batch RMS levels combined - see
# _combine_batch_rms. Kept comfortably under the observed 99-term ceiling.
_ASTATS_BATCH_LIMIT = 80


def _combine_batch_rms(per_batch: list[dict[int, float]], whole_file_duration: float,
                       span_duration: float) -> dict[int, float]:
    """Combine per-channel RMS-dB readings from several `detect_center_dominance`
    batches into the single figure one pass over ALL spans together would
    have produced, correcting for each batch's own dilution.

    Each batch's astats RMS is computed by ffmpeg over the WHOLE file
    duration with everything outside that batch's spans zeroed (`volume=0:
    enable=`doesn't drop samples, it silences them in place) - so a batch's
    reported dB is `10*log10(sum_of_squares_in_its_spans / whole_file_samples)`,
    diluted by however much of the file its spans don't cover. Converting
    back to linear power, undoing that per-batch dilution (multiply by
    whole_file_duration), summing across batches, then dividing by the TRUE
    total span duration (not the whole file) reconstructs the correct
    mean-square over just the dialogue time - physically the same number a
    single ffmpeg pass over every span at once would report, if ffmpeg's
    expression parser could actually take that many terms."""
    if span_duration <= 0:
        return {}
    power_sum: dict[int, float] = {}
    for levels in per_batch:
        for ch, db in levels.items():
            p = 0.0 if db == float("-inf") else 10.0 ** (db / 10.0)
            power_sum[ch] = power_sum.get(ch, 0.0) + p * whole_file_duration
    return {ch: (10.0 * math.log10(p / span_duration) if p > 0 else float("-inf"))
            for ch, p in power_sum.items()}


def detect_center_dominance(ffmpeg: str, ffprobe: str, media: Path, audio_pos: int, spans,
                            center_idx: int, cfg: Config) -> tuple[bool, dict[int, float]]:
    """During exactly `spans` (every channel silenced everywhere else, so the
    comparison is unaffected by the rest of the film), is the center
    channel's RMS level clearly the loudest? That means dialogue in this
    track really does live on the center channel alone.

    `spans` can be arbitrarily long - see _ASTATS_BATCH_LIMIT/_combine_batch_rms.
    For mute_track this is the flagged-word spans being muted; for
    dialog_remove_track it's normally the whole episode's dialogue moments
    derived from its subtitles (see subtitle_dialogue_spans) rather than the
    whole file - testing across the whole file dilutes the comparison with
    every narration-free stretch and can hide a center channel that's
    genuinely dominant specifically while someone is talking."""
    span_duration = sum(e - s for s, e, *_ in spans)
    if span_duration <= 0:
        return False, {}
    whole_file_duration = probe_duration(ffprobe, media)
    per_batch: list[dict[int, float]] = []
    for i in range(0, len(spans), _ASTATS_BATCH_LIMIT):
        batch = spans[i:i + _ASTATS_BATCH_LIMIT]
        expr = _span_expr(batch)
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", str(media), "-map", f"0:a:{audio_pos}",
             "-af", f"volume=0:enable='not({expr})',astats=metadata=0", "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        per_batch.append(_parse_astats_rms(proc.stderr))
    levels = _combine_batch_rms(per_batch, whole_file_duration, span_duration)
    if center_idx not in levels:
        return False, levels
    center = levels[center_idx]
    others = [v for i, v in levels.items() if i != center_idx]
    if not others:
        return False, levels
    loudest_other = max(others)
    dominant = center - loudest_other >= cfg.center_margin_db or (center > -90 and loudest_other <= -90)
    return dominant, levels


def _encode_track(ffmpeg: str, media: Path, graph: str, n_channels: int, cfg: Config, tmp: Path,
                  extra_inputs: list[Path] | None = None) -> Path:
    (tmp / "filter.txt").write_text(graph, encoding="utf-8")  # kept for --keep-temp / debugging
    bitrate = cfg.clean_bitrate if n_channels <= 2 else cfg.clean_bitrate_surround
    codec_args = {
        "flac": ["-c:a", "flac", "-compression_level", "5"],
        "ac3": ["-c:a", "ac3", "-b:a", bitrate],
        "eac3": ["-c:a", "eac3", "-b:a", bitrate],
        "aac": ["-c:a", "aac", "-b:a", bitrate],
    }[cfg.clean_codec]
    out = tmp / f"clean{CODEC_EXT[cfg.clean_codec]}"
    extra: list[str] = []
    for p in (extra_inputs or []):
        extra += ["-i", str(p)]
    run([ffmpeg, "-hide_banner", "-y", "-i", str(media), *extra,
         "-filter_complex", graph,
         "-map", "[out]", "-map_metadata", "-1", *codec_args, str(out)])
    return out


def _encode_track_from_wav(ffmpeg: str, wav_path: Path, n_channels: int, cfg: Config, tmp: Path) -> Path:
    """Same codec/bitrate selection as _encode_track, but for an already-
    finished audio file (e.g. splice_stemmed_spans's output) instead of a
    filter graph applied to the source media."""
    bitrate = cfg.clean_bitrate if n_channels <= 2 else cfg.clean_bitrate_surround
    codec_args = {
        "flac": ["-c:a", "flac", "-compression_level", "5"],
        "ac3": ["-c:a", "ac3", "-b:a", bitrate],
        "eac3": ["-c:a", "eac3", "-b:a", bitrate],
        "aac": ["-c:a", "aac", "-b:a", bitrate],
    }[cfg.clean_codec]
    out = tmp / f"clean{CODEC_EXT[cfg.clean_codec]}"
    run([ffmpeg, "-hide_banner", "-y", "-i", str(wav_path), *codec_args, str(out)])
    return out


def bleep_track(ffmpeg: str, media: Path, audio_pos: int, spans, cfg: Config,
                chosen: dict, tmp: Path) -> Path:
    props = chosen["properties"]
    n_ch = int(props.get("audio_channels") or 2)
    sr = int(props.get("audio_sampling_frequency") or 48000)
    graph = build_bleep_filter(spans, cfg, n_ch, sr, audio_pos)
    return _encode_track(ffmpeg, media, graph, n_ch, cfg, tmp)


def _center_channel_dominance(ffmpeg: str, ffprobe: str, media: Path, audio_pos: int, spans,
                              n_ch: int, cfg: Config):
    """For a >2ch track: is there a named layout with a center (FC) channel,
    and - during `spans` - is it clearly the loudest? Returns (center_idx,
    layout_name, dominant, levels, raw_layout); center_idx/layout_name/
    dominant are all None when the probed layout isn't a recognised one with
    a center channel at all (raw_layout is ffprobe's string in that case, for
    logging - may be empty)."""
    raw = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", f"a:{audio_pos}",
         "-show_entries", "stream=channel_layout", "-of", "csv=p=0", str(media)],
        capture_output=True, text=True, check=True).stdout.strip()
    names = CHANNEL_LAYOUTS.get(raw)
    if not (names and len(names) == n_ch and "FC" in names):
        return None, None, None, {}, raw
    center_idx = names.index("FC")
    dominant, levels = detect_center_dominance(ffmpeg, ffprobe, media, audio_pos, spans, center_idx, cfg)
    return center_idx, raw, dominant, levels, raw


def stem_clip_all_channels(ffmpeg: str, stem_tool: str, clip_wav: Path, n_ch: int,
                           sample_rate: int, tmp: Path, cfg: Config,
                           layout_name: str | None) -> Path:
    """Same idea as build_instrumental_stem, but for an already-extracted
    short clip file instead of the whole media file - see
    splice_stemmed_spans, which is what actually calls this."""
    if n_ch <= 2:
        if n_ch == 1:
            fake = tmp / "clip_stereo.wav"
            _mono_to_fake_stereo(ffmpeg, clip_wav, fake)
            inst = _run_separator(stem_tool, fake, tmp / "clip_out", cfg)
            mono_out = tmp / "clip_instrumental.wav"
            _stereo_instrumental_to_mono(ffmpeg, inst, mono_out)
            return mono_out
        return _run_separator(stem_tool, clip_wav, tmp / "clip_out", cfg)

    ch_dir = tmp / "clip_channels"
    ch_dir.mkdir(exist_ok=True)
    inst_paths = []
    for i in range(n_ch):
        mono = ch_dir / f"ch{i}.wav"
        run([ffmpeg, "-hide_banner", "-y", "-i", str(clip_wav),
             "-af", f"pan=mono|c0=c{i}", str(mono)])
        fake = ch_dir / f"ch{i}_stereo.wav"
        _mono_to_fake_stereo(ffmpeg, mono, fake)
        inst = _run_separator(stem_tool, fake, ch_dir / f"out{i}", cfg)
        mono_inst = ch_dir / f"ch{i}_inst.wav"
        _stereo_instrumental_to_mono(ffmpeg, inst, mono_inst)
        inst_paths.append(mono_inst)

    layout = layout_name if layout_name in CHANNEL_LAYOUTS else f"{n_ch}c"
    merged = tmp / "clip_instrumental.wav"
    inputs = [a for p in inst_paths for a in ("-i", str(p))]
    pads = "".join(f"[{i}:a]" for i in range(n_ch))
    graph = f"{pads}join=inputs={n_ch}:channel_layout={layout}[out]"
    run([ffmpeg, "-hide_banner", "-y", *inputs,
         "-filter_complex", graph, "-map", "[out]", "-ar", str(sample_rate), str(merged)])
    return merged


def splice_stemmed_spans(ffmpeg: str, ffprobe: str, media: Path, audio_pos: int, spans, n_ch: int,
                         sample_rate: int, layout_name: str | None, stem_tool: str, cfg: Config,
                         tmp: Path, context: float = SPLICE_CONTEXT_S) -> Path:
    """Replace every flagged span with a freshly-stemmed (vocals removed,
    ambient/music kept - never dead silence) version of just that short clip;
    everything outside the flagged spans is the untouched original, byte for
    byte. Built by splicing many short, independently-seeked segments rather
    than applying one volume=0:enable='between(...)' filter across the whole
    file: that mechanism was found to NOT achieve true silence/precision when
    evaluated from the start of a long file - confirmed reproducible even
    with a single span and with lossless FLAC (rules out both the ~100-term
    expression limit and codec artifacts as the cause). The identical filter
    on a pre-seeked short clip is exact, so every extraction here is done
    that way - short, independently seeked, never touching the whole-file
    timeline in one filter pass."""
    total_dur = probe_duration(ffprobe, media)
    seg_dir = tmp / "splice_segments"
    seg_dir.mkdir(parents=True, exist_ok=True)
    fmt_args = ["-ar", str(sample_rate), "-ac", str(n_ch)]
    segments: list[Path] = []
    cursor = 0.0

    def extract_original(s: float, e: float) -> Path:
        out = seg_dir / f"orig_{len(segments)}.wav"
        run([ffmpeg, "-hide_banner", "-y", "-ss", f"{s:.6f}", "-to", f"{e:.6f}",
             "-i", str(media), "-map", f"0:a:{audio_pos}", *fmt_args, str(out)])
        return out

    for i, (s, e, hits) in enumerate(spans):
        if s > cursor:
            segments.append(extract_original(cursor, s))

        clip_s = max(0.0, s - context)
        clip_e = min(total_dur, e + context)
        raw_clip = seg_dir / f"span{i}_raw.wav"
        run([ffmpeg, "-hide_banner", "-y", "-ss", f"{clip_s:.6f}", "-to", f"{clip_e:.6f}",
             "-i", str(media), "-map", f"0:a:{audio_pos}", *fmt_args, str(raw_clip)])

        span_tmp = seg_dir / f"span{i}_stem"
        span_tmp.mkdir(exist_ok=True)
        inst_clip = stem_clip_all_channels(ffmpeg, stem_tool, raw_clip, n_ch, sample_rate,
                                           span_tmp, cfg, layout_name)

        # the extra `context` was only so the stemmer had enough material to
        # work with - trim back down to exactly the flagged span before splicing
        trimmed = seg_dir / f"span{i}_trimmed.wav"
        run([ffmpeg, "-hide_banner", "-y", "-ss", f"{s - clip_s:.6f}", "-to", f"{e - clip_s:.6f}",
             "-i", str(inst_clip), *fmt_args, str(trimmed)])
        segments.append(trimmed)
        cursor = e

    if cursor < total_dur:
        segments.append(extract_original(cursor, total_dur))

    list_file = seg_dir / "concat_list.txt"
    list_file.write_text(
        "\n".join(f"file '{p.resolve().as_posix()}'" for p in segments), encoding="utf-8")
    spliced = tmp / "spliced.wav"
    run([ffmpeg, "-hide_banner", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
         *fmt_args, str(spliced)])
    return spliced


def splice_whole_track_stem(ffmpeg: str, ffprobe: str, media: Path, audio_pos: int, spans, n_ch: int,
                            sample_rate: int, layout_name: str | None, stem_tool: str, cfg: Config,
                            tmp: Path, whole_inst: Path | None = None) -> Path:
    """Same precise seek-and-concat splicing as splice_stemmed_spans (never
    the buggy whole-file volume=0:enable='between(...)' filter) - but for
    movies with a lot of flagged spans, where paying a fresh separator
    invocation's model-load overhead per span (splice_stemmed_spans) adds up
    past a certain point: stem the WHOLE track's vocals out ONCE
    (build_instrumental_stem - a fixed cost regardless of span count) and
    slice the flagged spans out of that instead. See _predict_stem_seconds
    for the time-cost model mute_track() uses to pick between the two.

    `whole_inst`, when given, is an ALREADY-STEMMED whole-track Instrumental
    wav to splice from directly instead of stemming the track again - see
    mute_track's cached_whole_instrumental (reusing the stem already
    produced for Config.stem_retranscribe's vocals pass on a <=2ch track, via
    build_vocals_stem's `also_instrumental`)."""
    total_dur = probe_duration(ffprobe, media)
    if whole_inst is None:
        print(f"  stemming the whole track once (~{total_dur / 60:.0f} min, {n_ch}ch)...")
        whole_inst = build_instrumental_stem(ffmpeg, stem_tool, media, audio_pos, n_ch, sample_rate,
                                             tmp, cfg, layout_name)

    seg_dir = tmp / "splice_segments"
    seg_dir.mkdir(parents=True, exist_ok=True)
    fmt_args = ["-ar", str(sample_rate), "-ac", str(n_ch)]
    segments: list[Path] = []
    cursor = 0.0

    def extract_original(s: float, e: float) -> Path:
        out = seg_dir / f"orig_{len(segments)}.wav"
        run([ffmpeg, "-hide_banner", "-y", "-ss", f"{s:.6f}", "-to", f"{e:.6f}",
             "-i", str(media), "-map", f"0:a:{audio_pos}", *fmt_args, str(out)])
        return out

    def extract_instrumental(s: float, e: float) -> Path:
        out = seg_dir / f"inst_{len(segments)}.wav"
        run([ffmpeg, "-hide_banner", "-y", "-ss", f"{s:.6f}", "-to", f"{e:.6f}",
             "-i", str(whole_inst), *fmt_args, str(out)])
        return out

    for s, e, hits in spans:
        if s > cursor:
            segments.append(extract_original(cursor, s))
        segments.append(extract_instrumental(s, e))
        cursor = e

    if cursor < total_dur:
        segments.append(extract_original(cursor, total_dur))

    list_file = seg_dir / "concat_list.txt"
    list_file.write_text(
        "\n".join(f"file '{p.resolve().as_posix()}'" for p in segments), encoding="utf-8")
    spliced = tmp / "spliced.wav"
    run([ffmpeg, "-hide_banner", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
         *fmt_args, str(spliced)])
    return spliced


def _predict_stem_seconds(n_ch: int, spans, total_dur: float) -> tuple[float, float]:
    """Predict total separator wall-clock time for the per-span approach
    (splice_stemmed_spans: n_ch invocations per span, one per padded clip)
    vs. the whole-track approach (splice_whole_track_stem: n_ch invocations
    total, one per channel, covering the whole file) using the
    STEM_STARTUP_S/STEM_RATE cost model. mute_track() picks whichever comes
    out lower - see that model's derivation above."""
    per_span = 0.0
    for s, e, _ in spans:
        clip_s = max(0.0, s - SPLICE_CONTEXT_S)
        clip_e = min(total_dur, e + SPLICE_CONTEXT_S)
        per_span += n_ch * (STEM_STARTUP_S + STEM_RATE * (clip_e - clip_s))
    whole_track = n_ch * (STEM_STARTUP_S + STEM_RATE * total_dur)
    return per_span, whole_track


def mute_track(ffmpeg: str, ffprobe: str, media: Path, audio_pos: int, spans, cfg: Config,
              chosen: dict, tmp: Path, stem_tool: str | None = None,
              cached_whole_instrumental: Path | None = None) -> tuple[Path, bool]:
    """Never leaves dead air and never mutes a channel that doesn't need it:
    every flagged span gets vocals stemmed out of EVERY channel for just that
    short clip (ambient noise/music keeps playing, exactly like the
    stems-fill fallback always did) and gets spliced back in; everything
    else is the untouched original. See splice_stemmed_spans for why this
    replaced the old whole-file volume=0:enable=... approach entirely (center
    -channel-only muting included) - that mechanism doesn't reach true
    silence when evaluated across a long file, discovered on a real 5.1
    movie track. Returns (path, False) - the bool is a holdover
    for the report's used_center_channel_trick field, permanently False now
    that mute never does the center-only trick.

    `cached_whole_instrumental`: an already-stemmed whole-track Instrumental
    wav to splice straight from - see main()'s cached_instrumental, produced
    "for free" alongside a Config.stem_retranscribe vocals pass on a <=2ch
    track (build_vocals_stem's `also_instrumental`). When set, this always
    wins over the normal per-span-vs-whole-track cost comparison below: the
    whole-track cost is already sunk, so splicing from it is now strictly
    cheaper than either option that cost model was ever choosing between."""
    props = chosen["properties"]
    n_ch = int(props.get("audio_channels") or 2)
    sr = int(props.get("audio_sampling_frequency") or 48000)

    if cfg.mute_fill == "stems":
        if cached_whole_instrumental is not None:
            print("  fill: reusing the whole-track instrumental stem already produced for the "
                  "vocals-stem re-transcription pass (Config.stem_retranscribe) - no need to stem "
                  "the track a second time")
            spliced = splice_whole_track_stem(ffmpeg, ffprobe, media, audio_pos, spans, n_ch, sr,
                                              None, stem_tool, cfg, tmp,
                                              whole_inst=cached_whole_instrumental)
            return _encode_track_from_wav(ffmpeg, spliced, n_ch, cfg, tmp), False
        if stem_tool is not None:
            layout_name = None
            if n_ch > 2:
                raw = subprocess.run(
                    [ffprobe, "-v", "error", "-select_streams", f"a:{audio_pos}",
                     "-show_entries", "stream=channel_layout", "-of", "csv=p=0", str(media)],
                    capture_output=True, text=True, check=True).stdout.strip()
                names = CHANNEL_LAYOUTS.get(raw)
                layout_name = raw if names and len(names) == n_ch else None
            total_dur = probe_duration(ffprobe, media)
            per_span_s, whole_track_s = _predict_stem_seconds(n_ch, spans, total_dur)
            if whole_track_s < per_span_s:
                print(f"  fill: predicted {whole_track_s / 60:.1f} min whole-track vs "
                      f"{per_span_s / 60:.1f} min per-span ({len(spans)} span(s)) - stemming the "
                      f"whole track once (mute_fill=stems, model={cfg.stem_model})")
                spliced = splice_whole_track_stem(ffmpeg, ffprobe, media, audio_pos, spans, n_ch, sr,
                                                  layout_name, stem_tool, cfg, tmp)
            else:
                print(f"  fill: predicted {per_span_s / 60:.1f} min per-span vs "
                      f"{whole_track_s / 60:.1f} min whole-track - stemming vocals out of every "
                      f"channel for just the {len(spans)} flagged span(s) (mute_fill=stems, "
                      f"model={cfg.stem_model}) - ambient noise/music keeps playing throughout, "
                      f"nothing is ever fully silenced")
                spliced = splice_stemmed_spans(ffmpeg, ffprobe, media, audio_pos, spans, n_ch, sr,
                                               layout_name, stem_tool, cfg, tmp)
            return _encode_track_from_wav(ffmpeg, spliced, n_ch, cfg, tmp), False
        print(f"  [note] mute_fill=stems but no stemmer found at {STEM_VENV} - falling back to "
              f"silence (see README for one-time setup)", file=sys.stderr)

    graph = build_mute_filter(spans, audio_pos, n_ch, None, None)
    return _encode_track(ffmpeg, media, graph, n_ch, cfg, tmp), False


# ---------------------------------------------------------------------------
# stemming: separate dialogue from "everything else" (music/effects/ambience)
# via audio-separator (see STEM_VENV / locate_stem_tool). Used by
# mute_track()'s mute_fill="stems" and by --method dialog.
# ---------------------------------------------------------------------------
STEM_RETRY_ATTEMPTS = 3    # a movie with many flagged spans makes hundreds of these
STEM_RETRY_DELAY_S = 5.0  # calls in a row; a rare transient one shouldn't sink the whole run


def _invoke_separator(stem_tool: str, wav_in: Path, out_dir: Path, cfg: Config,
                      single_stem: str | None) -> None:
    """Run audio-separator on a (fake-)stereo wav, writing into `out_dir`.
    `single_stem` ("Instrumental"/"Vocals") asks for just that one output;
    None omits --single_stem entirely so BOTH stems get written from the
    SAME inference pass - the model estimates one and derives the other by
    subtraction internally regardless of what's asked for, so this costs no
    more GPU time than requesting a single stem. See _run_separator /
    _run_separator_pair for the two shapes callers actually want, and
    build_vocals_stem's `also_instrumental` for why getting both matters.

    Holds the shared GPU lock (../gpu_lock, if present) for the duration -
    audio-separator uses CUDA the same as anything else that might be
    sharing the GPU, and running it with no coordination can crash outright
    under contention (a real access-violation crash was hit this way), not
    just run slowly.

    Retries a few times on failure: a movie with many flagged spans calls
    this hundreds of times in a row (once per channel per span), and a rare
    transient failure deep into that sequence shouldn't sink the entire run
    (observed once as a spurious "invalid audio data" error on an input that,
    re-run by hand afterward, turned out to be completely valid). A real,
    reproducible bad input still fails after retrying."""
    if str(GPU_LOCK_DIR) not in sys.path:
        sys.path.insert(0, str(GPU_LOCK_DIR))
    try:
        import gpu_lock
    except ImportError:
        gpu_lock = None
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [stem_tool, str(wav_in), "-m", cfg.stem_model,
           "--output_dir", str(out_dir), "--output_format", "WAV"]
    if single_stem is not None:
        cmd += ["--single_stem", single_stem]
    for attempt in range(1, STEM_RETRY_ATTEMPTS + 1):
        try:
            gpu_ctx = (gpu_lock.hold("Profanity_Filter", f"stemming {wav_in.name}")
                      if gpu_lock else contextlib.nullcontext())
            with gpu_ctx:
                run(cmd)
            return
        except subprocess.CalledProcessError:
            if attempt == STEM_RETRY_ATTEMPTS:
                raise
            print(f"  [warn] stemmer failed on {wav_in.name} (attempt {attempt}/"
                  f"{STEM_RETRY_ATTEMPTS}) - retrying in {STEM_RETRY_DELAY_S:.0f}s",
                  file=sys.stderr)
            time.sleep(STEM_RETRY_DELAY_S)


def _find_stem_output(out_dir: Path, stem: str, cfg: Config) -> Path:
    matches = sorted(out_dir.glob(f"*{stem}*.wav"))
    if not matches:
        raise SystemExit(f"[error] stemmer produced no {stem} output in {out_dir} - "
                         f"check that model {cfg.stem_model!r} labels its stems Vocals/Instrumental")
    return matches[0]


def _run_separator(stem_tool: str, wav_in: Path, out_dir: Path, cfg: Config,
                   stem: str = "Instrumental") -> Path:
    """Run the stemmer on a (fake-)stereo wav, asking for only the named
    `stem` ("Instrumental" for mute_fill/dialog removal, "Vocals" for
    build_vocals_stem's re-transcription use), and return its output path."""
    _invoke_separator(stem_tool, wav_in, out_dir, cfg, single_stem=stem)
    return _find_stem_output(out_dir, stem, cfg)


def _run_separator_pair(stem_tool: str, wav_in: Path, out_dir: Path, cfg: Config) -> tuple[Path, Path]:
    """Like _run_separator, but returns BOTH the Vocals and Instrumental
    stems from a single invocation - see _invoke_separator's docstring for
    why that's not twice the GPU cost of asking for one. Used when a caller
    needs both from the same source audio (build_vocals_stem's
    `also_instrumental`) instead of paying for two separate separator runs."""
    _invoke_separator(stem_tool, wav_in, out_dir, cfg, single_stem=None)
    return _find_stem_output(out_dir, "Vocals", cfg), _find_stem_output(out_dir, "Instrumental", cfg)


def _mono_to_fake_stereo(ffmpeg: str, mono_wav: Path, out_wav: Path) -> None:
    """Duplicate a mono channel to L=R - most separator models are trained on
    stereo and refuse (or do badly on) a true mono input."""
    run([ffmpeg, "-hide_banner", "-y", "-i", str(mono_wav),
         "-af", "pan=stereo|c0=c0|c1=c0", str(out_wav)])


def _stereo_instrumental_to_mono(ffmpeg: str, stereo_wav: Path, out_wav: Path) -> None:
    """Fold a fake-stereo separator output back down to the single real
    channel it came from."""
    run([ffmpeg, "-hide_banner", "-y", "-i", str(stereo_wav),
         "-af", "pan=mono|c0=0.5*c0+0.5*c1", str(out_wav)])


def build_instrumental_stem(ffmpeg: str, stem_tool: str, media: Path, audio_pos: int, n_ch: int,
                            sample_rate: int, tmp: Path, cfg: Config,
                            layout_name: str | None) -> Path:
    """A wav with the SAME channel count as the source audio track, holding
    only its non-dialogue content ("everything else" - music/effects/room
    tone) with the dialogue stemmed out.

    <=2 channels: the track goes through the separator as-is (mono is faked
    up to stereo first, then folded back down - see above).

    >2 channels: the separator model only knows stereo, so there's no single
    call that preserves a 5.1/7.1 layout. Instead each channel is split out,
    faked up to stereo, stemmed, and folded back to mono ON ITS OWN - then all
    of them are rejoined into one file with the original layout. Slower (one
    separator pass per channel) but it's the only way to keep the channel
    count the caller asked for."""
    if n_ch <= 2:
        src_wav = tmp / "stem_src.wav"
        run([ffmpeg, "-hide_banner", "-y", "-i", str(media),
             "-map", f"0:a:{audio_pos}", str(src_wav)])
        if n_ch == 1:
            fake = tmp / "stem_src_stereo.wav"
            _mono_to_fake_stereo(ffmpeg, src_wav, fake)
            inst = _run_separator(stem_tool, fake, tmp / "stem_out", cfg)
            mono_out = tmp / "instrumental.wav"
            _stereo_instrumental_to_mono(ffmpeg, inst, mono_out)
            return mono_out
        return _run_separator(stem_tool, src_wav, tmp / "stem_out", cfg)

    ch_dir = tmp / "stem_channels"
    ch_dir.mkdir(exist_ok=True)
    inst_paths = []
    for i in range(n_ch):
        mono = ch_dir / f"ch{i}.wav"
        run([ffmpeg, "-hide_banner", "-y", "-i", str(media),
             "-map", f"0:a:{audio_pos}", "-af", f"pan=mono|c0=c{i}", str(mono)])
        fake = ch_dir / f"ch{i}_stereo.wav"
        _mono_to_fake_stereo(ffmpeg, mono, fake)
        inst = _run_separator(stem_tool, fake, ch_dir / f"out{i}", cfg)
        mono_inst = ch_dir / f"ch{i}_inst.wav"
        _stereo_instrumental_to_mono(ffmpeg, inst, mono_inst)
        inst_paths.append(mono_inst)

    layout = layout_name if layout_name in CHANNEL_LAYOUTS else f"{n_ch}c"
    merged = tmp / "instrumental.wav"
    inputs = [a for p in inst_paths for a in ("-i", str(p))]
    pads = "".join(f"[{i}:a]" for i in range(n_ch))
    graph = f"{pads}join=inputs={n_ch}:channel_layout={layout}[out]"
    run([ffmpeg, "-hide_banner", "-y", *inputs,
         "-filter_complex", graph, "-map", "[out]", "-ar", str(sample_rate), str(merged)])
    return merged


def build_vocals_stem(ffmpeg: str, stem_tool: str, media: Path, audio_pos: int, tmp: Path,
                      cfg: Config, also_instrumental: bool = False) -> tuple[Path, Path | None]:
    """A vocals-only wav of the chosen audio track, for feeding a SECOND
    transcription pass (see Config.stem_retranscribe) - not for final output
    on its own; that's normally build_instrumental_stem/mute_track's job.
    Channel count doesn't matter for the vocals half (transcription
    downmixes to mono 16kHz internally regardless), so this always downmixes
    straight to plain stereo before stemming - one separator call, not one
    per channel.

    `also_instrumental=True` gets the whole-track Instrumental stem back too
    (second element), from the very SAME separator invocation - no extra GPU
    cost, see _run_separator_pair. Only pass this when the source track
    itself has <=2 channels: that's the only case where this stereo downmix
    IS exactly what mute_track's own whole-track stemming
    (build_instrumental_stem) would otherwise separately produce, so a file
    that already had to pay for vocal isolation to get a transcript (no
    subtitle safety net at all) can reuse this SAME pass for muting too
    instead of stemming the whole track twice - see main()'s
    cached_instrumental / mute_track's cached_whole_instrumental. For a >2ch
    track, a stereo downmix can't stand in for a real per-channel surround
    stem, so leave this False and mute_track will stem it separately as
    before.

    Measured (voice_to_text/AGENTS.md, "Reducing the Whisper miss rate") to
    recover real dialogue that Whisper never decodes at all from the original
    mixed track - masked by music/effects loud enough to fail VAD - which is
    exactly the class of miss worth paying for specifically when a file has
    no subtitle safety net left at all (Config.stem_retranscribe's only
    trigger)."""
    src_wav = tmp / "vocals_src.wav"
    run([ffmpeg, "-hide_banner", "-y", "-i", str(media),
         "-map", f"0:a:{audio_pos}", "-ac", "2", str(src_wav)])
    out_dir = tmp / "vocals_out"
    if also_instrumental:
        return _run_separator_pair(stem_tool, src_wav, out_dir, cfg)
    return _run_separator(stem_tool, src_wav, out_dir, cfg, stem="Vocals"), None


def ensure_vocals_transcript(media: Path, vocals_wav: Path, tmp: Path, cfg: Config) -> Path:
    """Transcribe `vocals_wav` (see build_vocals_stem) into its own sibling
    "<name>.vocals.json" - cached like the PGS/VobSub OCR outputs, so a rerun
    on the same file doesn't re-stem/re-transcribe unless --retranscribe.
    Mirrors ensure_transcript() but writes to a different name (never
    overwrites the primary "<name>.json") and via a staged copy, since
    Voice_to_Text names its output after ITS input's stem, not the original
    media's."""
    js = media.with_name(f"{media.stem}.vocals.json")
    if js.is_file() and not cfg.retranscribe:
        print(f"  transcript (vocals stem): {js.name} (reusing)")
        return js
    py = VOICE_TO_TEXT / ".venv" / "Scripts" / "python.exe"
    script = VOICE_TO_TEXT / "transcribe.py"
    if not py.is_file() or not script.is_file():
        raise SystemExit(f"[error] no '{js.name}' next to the input and "
                         f"Voice_to_Text not found at {VOICE_TO_TEXT}")
    staged = tmp / f"{media.stem}.wav"
    shutil.copy2(vocals_wav, staged)
    print(f"  transcript (vocals stem): running Voice_to_Text on the isolated vocal track...")
    run([str(py), "-X", "utf8", str(script), str(staged),
         "--formats", "json", "--no-diarize", "--output-dir", str(tmp)], cwd=str(VOICE_TO_TEXT))
    produced = tmp / f"{media.stem}.json"
    if not produced.is_file():
        raise SystemExit("[error] vocals-stem transcription produced no .json")
    shutil.copy2(produced, js)
    return js


def scan_vocals_transcript(js: Path, matchers: dict) -> list[dict]:
    """Same wordlist scan ensure_transcript's own .json gets (flag_language.
    scan_file), tagged "vocals-stem" so callers can tell these hits apart
    from the primary transcript's own - see _dedupe_vocals_hits()."""
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    import flag_language
    hits, _fmt = flag_language.scan_file(js, matchers)
    for h in hits:
        h["source"] = "vocals-stem"
    return hits


# subtitle codec IDs (mkvmerge's -J naming) that carry exact per-cue start/end
# timing as text - vs. image-based formats (PGS/VobSub) that only give a
# presentation start per cue, so an end has to be estimated (see _pgs_cue_starts).
_TEXT_SUB_CODECS = {"S_TEXT/UTF8", "S_TEXT/ASS", "S_TEXT/SSA", "S_TEXT/USF"}
_IMAGE_SUB_CODECS = {"S_HDMV/PGS", "S_VOBSUB"}


def _pick_dialogue_subs_track(tracks: list) -> dict | None:
    """The best subtitle track in `tracks` for deriving dialogue timing (see
    subtitle_dialogue_spans) - a text track over an image-based one (exact
    cue end times vs. an estimate), the default/English one when there's a
    choice. None if the file has no subtitle track at all."""
    subs = [t for t in tracks if t["type"] == "subtitles"]
    if not subs:
        return None

    def rank(t: dict) -> tuple[int, int, int]:
        p = t["properties"]
        is_text = (p.get("codec_id") or "").upper() in _TEXT_SUB_CODECS
        is_default = bool(p.get("default_track"))
        is_eng = (p.get("language") or "").lower() in ("eng", "en")
        return (0 if is_text else 1, 0 if is_default else 1, 0 if is_eng else 1)

    return sorted(subs, key=rank)[0]


def _pgs_cue_starts(ffprobe: str, sup_path: Path) -> list[float]:
    """Distinct subtitle-presentation start times (s) in a PGS/VobSub .sup -
    each on-screen cue shows up as several packets sharing one PTS (its
    separate PCS/WDS/PDS/ODS segments), so this only needs the packet
    timestamps, not their (image) content."""
    out = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "s", "-show_entries", "packet=pts_time",
         "-of", "csv=p=0", str(sup_path)],
        capture_output=True, text=True, check=True).stdout
    return sorted(set(round(float(x), 3) for x in out.split() if x.strip()))


def subtitle_dialogue_spans(mkvextract: str, ffprobe: str, media: Path, track: dict, tmp: Path,
                            merge_gap: float = 1.5, max_cue_dur: float = 4.0) -> list[tuple]:
    """[(start, end, None), ...] for every dialogue moment in subtitle
    `track` of `media`, nearby cues merged into one span (gap <= `merge_gap`)
    - used to test center-channel dominance only where someone is actually
    talking (see detect_center_dominance) instead of being diluted by long
    narration-free/music-only stretches, which a nature documentary can have
    a lot of. A text track (SRT/ASS/...) gives exact cue end times via
    flag_language.parse_srt; an image-based one (PGS/VobSub) only carries a
    presentation start per cue, so its end is estimated as either
    `max_cue_dur` later or the next cue's start, whichever comes first.
    Can run into the hundreds of spans for a full episode - that's fine,
    detect_center_dominance batches internally (see _ASTATS_BATCH_LIMIT)."""
    codec = (track["properties"].get("codec_id") or "").upper()
    if codec in _TEXT_SUB_CODECS:
        srt_path = tmp / "dialogue_subs.srt"
        run([mkvextract, "tracks", str(media), f"{track['id']}:{srt_path}"])
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import flag_language  # lives in this folder
        starts_ends = [(u.start, u.end) for u in flag_language.parse_srt(srt_path)
                       if u.start is not None and u.end is not None]
    else:
        sup_path = tmp / "dialogue_subs.sup"
        run([mkvextract, "tracks", str(media), f"{track['id']}:{sup_path}"])
        starts = _pgs_cue_starts(ffprobe, sup_path)
        starts_ends = []
        for i, s in enumerate(starts):
            nxt = starts[i + 1] if i + 1 < len(starts) else s + max_cue_dur
            e = min(s + max_cue_dur, nxt)
            if e > s:
                starts_ends.append((s, e))

    starts_ends.sort()
    merged: list[list[float]] = []
    for s, e in starts_ends:
        if merged and s - merged[-1][1] <= merge_gap:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(s, e, None) for s, e in merged]


def dialog_remove_track(ffmpeg: str, ffprobe: str, mkvextract: str, media: Path, audio_pos: int,
                        cfg: Config, chosen: dict, tmp: Path, stem_tool: str | None,
                        tracks: list | None = None) -> tuple[Path, bool]:
    """--method dialog: strip ALL dialogue from the track (not just flagged
    words). Returns (clean_audio_path, used_center_trick).

    If the track has a recognised center channel and dialogue is dominant
    there DURING THE TRACK'S DIALOGUE MOMENTS (derived from an embedded
    subtitle track via subtitle_dialogue_spans - falls back to testing the
    whole file when there's no usable subtitle track), only that channel is
    muted for the entire runtime - every other channel's music/effects play
    through untouched, and the channel count is trivially preserved. If not
    (stereo/mono, or a >2ch track without a clean center), the stemmer
    extracts the non-dialogue content from every channel instead (see
    build_instrumental_stem) and that becomes the whole output track.

    Testing against the whole file instead of just the dialogue moments is a
    real trap on this kind of source: a nature-documentary mix can spend most
    of its runtime on narration-free music/effects, which dilutes an
    over-the-whole-file measurement enough to hide a center channel that's
    actually clearly dominant specifically while the narrator is talking -
    confirmed on a real nature-documentary track, where the whole-file reading
    put the center channel as the QUIETEST of the six (~-86 dB, everything
    else ~-30 to -43 dB) while its subtitle-gated reading told the opposite
    story once the astats batching fix (_ASTATS_BATCH_LIMIT) made measuring
    that many spans possible at all."""
    props = chosen["properties"]
    n_ch = int(props.get("audio_channels") or 2)
    sr = int(props.get("audio_sampling_frequency") or 48000)
    duration = probe_duration(ffprobe, media)
    whole_span = [(0.0, duration, None)]

    dialogue_spans = whole_span
    span_note = "the whole file (no usable subtitle track found)"
    subs_track = _pick_dialogue_subs_track(tracks) if tracks else None
    if subs_track is not None:
        try:
            derived = subtitle_dialogue_spans(mkvextract, ffprobe, media, subs_track, tmp)
        except Exception as exc:
            derived = []
            print(f"  [warn] couldn't derive dialogue timing from subtitles ({exc!r}) - "
                  f"testing the center channel across the whole file instead", file=sys.stderr)
        if derived:
            dialogue_spans = derived
            cov = sum(e - s for s, e, _ in derived)
            span_note = f"{len(derived)} dialogue span(s) from subtitles ({cov:.0f}s covered)"
    print(f"  center-channel test uses: {span_note}")

    center_idx = layout_name = None
    if n_ch > 2:
        center_idx, layout_name, dominant, levels, raw = _center_channel_dominance(
            ffmpeg, ffprobe, media, audio_pos, dialogue_spans, n_ch, cfg)
        if layout_name is None:
            print(f"  channels: {n_ch}ch, layout {raw or 'unknown'} has no recognised center "
                  f"channel -> stemming dialogue out of every channel")
        else:
            lv = ", ".join(f"ch{i}={v:.1f}dB" for i, v in sorted(levels.items()))
            if dominant:
                print(f"  channels: {layout_name}, center=FC(#{center_idx}) carries the dialogue "
                      f"alone ({lv}) -> muting center channel only, for the whole file")
            else:
                print(f"  channels: {layout_name}, center=FC(#{center_idx}) NOT clearly dialogue-only "
                      f"({lv}) -> stemming dialogue out of every channel")
                center_idx = None

    if center_idx is not None:
        graph = build_mute_filter(whole_span, audio_pos, n_ch, center_idx, layout_name)
        return _encode_track(ffmpeg, media, graph, n_ch, cfg, tmp), True

    if stem_tool is None:
        raise SystemExit(
            f"[error] --method dialog needs the stemmer for this source (no clean center channel "
            f"to mute alone) but none was found at {STEM_VENV} - see README for one-time setup")
    inst_wav = build_instrumental_stem(ffmpeg, stem_tool, media, audio_pos, n_ch, sr, tmp, cfg,
                                       layout_name)
    bitrate = cfg.clean_bitrate if n_ch <= 2 else cfg.clean_bitrate_surround
    codec_args = {
        "flac": ["-c:a", "flac", "-compression_level", "5"],
        "ac3": ["-c:a", "ac3", "-b:a", bitrate],
        "eac3": ["-c:a", "eac3", "-b:a", bitrate],
        "aac": ["-c:a", "aac", "-b:a", bitrate],
    }[cfg.clean_codec]
    out = tmp / f"clean{CODEC_EXT[cfg.clean_codec]}"
    run([ffmpeg, "-hide_banner", "-y", "-i", str(inst_wav), *codec_args, str(out)])
    return out, False


def build_cut_filter(spans, audio_pos: int) -> str:
    """Splice the flagged spans out entirely and close the resulting gaps -
    the standard ffmpeg aselect+asetpts idiom."""
    expr = _span_expr(spans)
    return f"[0:a:{audio_pos}]aselect='not({expr})',asetpts=N/SR/TB[out]"


def remap_time(t: float, spans) -> float:
    """Where original timestamp `t` lands on the post-cut timeline: shifted
    left by the total duration of every cut span before it. A `t` that falls
    inside a cut span collapses to that span's (already-remapped) start,
    matching where aselect+asetpts actually puts the surrounding audio."""
    cut = sum(max(0.0, min(t, e) - s) for s, e, _ in spans)
    return max(0.0, t - cut)


def build_remapped_chapters(chapters: list[dict], spans) -> tuple[str | None, int]:
    """FFMETADATA1 text with every chapter's start/end shifted onto the
    post-cut timeline. A chapter entirely swallowed by cut span(s) - its
    remapped start >= end - is dropped. Returns (text_or_None, kept_count)."""
    lines = [";FFMETADATA1", ""]
    kept = 0
    for i, ch in enumerate(chapters):
        start = remap_time(float(ch["start_time"]), spans)
        end = remap_time(float(ch["end_time"]), spans)
        if end <= start:
            continue
        title = (ch.get("tags") or {}).get("title") or f"Chapter {i + 1}"
        lines += ["[CHAPTER]", "TIMEBASE=1/1000",
                  f"START={round(start * 1000)}", f"END={round(end * 1000)}",
                  f"title={title}", ""]
        kept += 1
    return ("\n".join(lines) if kept else None), kept


def cut_track(ffmpeg: str, ffprobe: str, media: Path, audio_pos: int, spans, cfg: Config,
             tmp: Path) -> tuple[Path, str]:
    """Cut the flagged spans out of the audio. Returns (file, output extension)
    - the output keeps the source file's own container/extension, re-encoded
    with a matching codec (cutting requires a decode, so a bit-exact stream
    copy isn't possible)."""
    streams = probe_streams(ffprobe, media)
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    src = audio_streams[audio_pos] if audio_pos < len(audio_streams) else {}
    encoder, lossless = CUT_CODEC_MAP.get(src.get("codec_name", ""), CUT_FALLBACK_CODEC)
    if src.get("codec_name") not in CUT_CODEC_MAP:
        print(f"  [note] unrecognised source codec {src.get('codec_name')!r} - "
              f"cutting to mp3 instead of matching it")

    graph = build_cut_filter(spans, audio_pos)
    (tmp / "filter.txt").write_text(graph, encoding="utf-8")

    # chapters: remap every mark onto the post-cut timeline rather than drop
    # them - cutting shifts every timestamp after the first cut, so they'd
    # otherwise silently go out of sync with the (now shorter) audio.
    chapters = run_json([ffprobe, "-v", "error", "-show_chapters", "-of", "json",
                         str(media)]).get("chapters", [])
    extra_inputs: list[str] = []
    chapter_args = ["-map_chapters", "-1"]
    if chapters:
        meta_text, kept = build_remapped_chapters(chapters, spans)
        if meta_text:
            meta_path = tmp / "chapters.ffmeta"
            meta_path.write_text(meta_text, encoding="utf-8")
            extra_inputs = ["-i", str(meta_path)]
            chapter_args = ["-map_chapters", "1"]
            dropped = len(chapters) - kept
            note = f" ({dropped} dropped - entirely inside a cut span)" if dropped else ""
            print(f"  chapters: remapped {kept}/{len(chapters)} through the cut{note}")
        else:
            print(f"  chapters: all {len(chapters)} chapter(s) were entirely inside cut "
                  f"spans - none carried over")

    ext = media.suffix if src.get("codec_name") in CUT_CODEC_MAP else ".mp3"
    codec_args = ["-c:a", encoder] if lossless else ["-c:a", encoder, "-b:a", cfg.cut_bitrate]
    out = tmp / f"cut{ext}"
    run([ffmpeg, "-hide_banner", "-y", "-i", str(media), *extra_inputs,
         "-filter_complex", graph, "-map", "[out]",
         "-map_metadata", "0", *chapter_args, *codec_args, str(out)])
    return out, ext


# ---------------------------------------------------------------------------
# "cut" on an mp3 source: genuinely lossless splice
# ---------------------------------------------------------------------------
# MP3 is one of the few codecs where cutting doesn't have to mean decoding:
# every KEPT region can be a plain stream copy (bit-identical to the source,
# snapped to the nearest frame boundary) and only the flagged spans actually
# disappear - nothing is ever decoded or re-encoded. cut_track() above still
# exists as the general fallback for every other codec (AAC/Opus/etc. don't
# splice cleanly - inter-frame prediction/priming makes a naive concat click
# or glitch at the seams - so they go through the old decode+re-encode path).
def probe_duration(ffprobe: str, media: Path) -> float:
    d = run_json([ffprobe, "-v", "error", "-show_format", "-of", "json", str(media)])
    return float(d["format"]["duration"])


def build_keep_regions(spans, duration: float, min_len: float = 0.05) -> list[tuple[float, float]]:
    """The complement of the flagged spans within [0, duration] - the audio
    that survives. A sliver shorter than `min_len` (back-to-back cuts, or a
    cut flush against either end of the file) is dropped rather than kept as
    a near-zero-length segment."""
    regions: list[tuple[float, float]] = []
    cursor = 0.0
    for s, e, _ in spans:
        s, e = max(0.0, s), min(duration, e)
        if s > cursor + min_len:
            regions.append((cursor, s))
        cursor = max(cursor, e)
    if duration - cursor > min_len:
        regions.append((cursor, duration))
    return regions


def mp3_splice_cut(ffmpeg: str, ffprobe: str, media: Path, audio_pos: int,
                   spans, tmp: Path) -> tuple[Path, float, float]:
    """Remove `spans` from an mp3 with no re-encoding at all. Returns
    (spliced_file, source_duration, new_duration).

    Each kept region is extracted with `-c copy` (output-side -ss/-to, which
    for a stream copy just picks the first/last whole MP3 frame in range -
    exact even on a VBR source, unlike input-side seeking which trusts a
    possibly-stale Xing TOC). The regions are then joined with ffmpeg's
    concat demuxer, still `-c copy` - MP3 frames are independently decodable,
    so a frame-aligned concat plays back seamlessly.

    Known imperfection: the source's Xing/LAME VBR header (a fake first
    frame carrying the original total frame/byte count for fast duration
    display and gapless-playback trimming) is dropped rather than
    regenerated - regenerating it correctly requires re-encoding, which
    would defeat the point. Every player this was tested with falls back to
    scanning frame headers - ffprobe's own reported duration on the spliced
    file matches the sum of the kept regions exactly - but a strict/old
    player that trusts a Xing TOC without a sanity check could show a wrong
    duration or seek slightly off. Gapless-playback padding metadata (LAME's
    encoder delay/padding) is also lost for the same reason.
    """
    duration = probe_duration(ffprobe, media)
    regions = build_keep_regions(spans, duration)
    if not regions:
        raise SystemExit("[error] every second of the file is flagged - nothing would remain")

    seg_paths = []
    for i, (s, e) in enumerate(regions):
        seg = tmp / f"keep_{i:04d}.mp3"
        run([ffmpeg, "-hide_banner", "-y", "-i", str(media), "-map", f"0:a:{audio_pos}",
             "-ss", f"{s:.3f}", "-to", f"{e:.3f}", "-c:a", "copy", "-map_metadata", "-1", str(seg)])
        seg_paths.append(seg)

    list_path = tmp / "concat_list.txt"
    list_path.write_text(
        "\n".join(f"file '{p.as_posix()}'" for p in seg_paths) + "\n", encoding="utf-8")
    spliced = tmp / "spliced.mp3"
    run([ffmpeg, "-hide_banner", "-y", "-f", "concat", "-safe", "0", "-i", str(list_path),
         "-c", "copy", "-map_metadata", "-1", str(spliced)])

    new_duration = probe_duration(ffprobe, spliced)
    return spliced, duration, new_duration


def preserve_id3_tags(source: Path, dest: Path, spans=None) -> dict:
    """Copy every ID3v2 frame from `source` onto `dest` byte-for-byte via
    mutagen - title/artist/album/track/genre/comment, cover art (APIC),
    nonstandard frames (Audible rips carry WOAS/UFID/NARRATEDBY, etc.), the
    lot. ffmpeg's -map_metadata only round-trips the text frames it
    recognises and drops attached pictures entirely, which is exactly the
    "careful not to lose metadata" failure mode this avoids.

    If `spans` (the removed audio spans) is given and the source has ID3v2
    chapter frames (CHAP/CTOC - rare for a book already split one-file-per-
    chapter, but common for a single-file audiobook), each CHAP's start/end
    is remapped onto the post-cut timeline with the same remap_time() used
    for ffprobe chapters elsewhere in this file; a chapter entirely swallowed
    by a cut span is dropped. Byte offsets in remapped CHAP frames (an
    optional, rarely-used alternative to time offsets) are set to the ID3
    "not used" sentinel (0xFFFFFFFF) since splicing invalidates them."""
    try:
        from mutagen.id3 import ID3, CHAP
    except ImportError:
        return {"copied": False, "reason": "mutagen not installed"}

    try:
        src_id3 = ID3(source)
    except Exception as exc:
        return {"copied": False, "reason": f"source has no readable ID3 tag ({exc!r})"}

    n_chapters = 0
    if spans:
        for key in list(src_id3.keys()):
            frame = src_id3[key]
            if not isinstance(frame, CHAP):
                continue
            new_start = round(remap_time(frame.start_time / 1000.0, spans) * 1000)
            new_end = round(remap_time(frame.end_time / 1000.0, spans) * 1000)
            if new_end <= new_start:
                del src_id3[key]
                continue
            frame.start_time, frame.end_time = new_start, new_end
            frame.start_offset = frame.end_offset = 0xFFFFFFFF
            n_chapters += 1

    src_id3.save(dest, v2_version=3)
    return {"copied": True, "frames": len(src_id3.keys()), "chapters_remapped": n_chapters}


_SRT_TIME = re.compile(r"(\d{1,3}):([0-5]?\d):([0-5]?\d)[,.](\d{1,3})")


def _srt_sec(m) -> float:
    h, mm, ss, ms = m
    return int(h) * 3600 + int(mm) * 60 + int(ss) + int(ms.ljust(3, "0")) / 1000.0


def censor_srt(text: str, spans, matchers: dict, mask: str, pad: float):
    """Replace matcher hits with `mask` in every cue that overlaps a bleep
    span. mask="" (used for method="mute" - see main()) deletes the flagged
    word entirely rather than visibly marking where it was, matching audio
    that's been stemmed/silenced rather than replaced with an audible tone -
    the leftover double space/edge space from the deletion is collapsed."""
    ivals = [(s - pad, e + pad) for s, e, _ in spans]
    blocks = re.split(r"\r?\n\r?\n", text.lstrip("﻿").strip())
    out, cues_hit, words = [], 0, 0
    for block in blocks:
        lines = [ln.rstrip("\r") for ln in block.split("\n")]
        ti = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        times = _SRT_TIME.findall(lines[ti]) if ti is not None else []
        if ti is None or len(times) < 2:
            out.append(block)
            continue
        cs, ce = _srt_sec(times[0]), _srt_sec(times[1])
        if not any(cs < ie and lo < ce for lo, ie in ivals):
            out.append(block)
            continue
        body = lines[ti + 1:]
        new_body = []
        for ln in body:
            for rx in matchers.values():
                if rx is not None:
                    ln, n = rx.subn(mask, ln)
                    words += n
            if mask == "":
                ln = re.sub(r"[ \t]{2,}", " ", ln).strip()
            new_body.append(ln)
        if new_body != body:
            cues_hit += 1
        out.append("\n".join(lines[:ti + 1] + new_body))
    return "\n\n".join(out) + "\n", cues_hit, words


def remux(mkvmerge: str, media: Path, tracks: list, cfg: Config, out_mkv: Path,
          clean_audio: Path, chosen_audio: dict,
          clean_srt: Path | None, chosen_subs: dict | None,
          audio_suffix: str | None = None, audio_default: bool = True,
          exclude_audio_ids=(), exclude_subs_ids=(),
          extra_srt: tuple[Path, dict] | None = None) -> None:
    """`tracks` must already have any stale own-output track (see
    is_own_output_track) filtered OUT - it drives both the default-flag
    loop below and --track-order, so a stale track left in would still get
    an order/flag entry for a track this call is about to drop entirely.
    `exclude_audio_ids`/`exclude_subs_ids` (from main()'s `tracks_all`,
    where those ids still exist) are what actually drop them from `media`'s
    import via mkvmerge's `!id,id` negation syntax - a rerun's fresh
    (Cleaned)/(Wordless) track REPLACES the stale one instead of piling up
    alongside it.

    `extra_srt` is an extra, always-non-default subtitle track added as-is
    (no censoring) alongside `clean_srt`/`chosen_subs` - `(path, {"language":
    ..., "track_name": ...})`. Used only for a resynced external source -
    OpenSubtitles or a sidecar file (see resync_external_srt): unlike every
    other subtitle source in main() (an embedded/OCR'd track whose original
    already passes through the container untouched), neither of those exists
    as a track in the source file at all, so their "original" (uncensored,
    resynced) counterpart has to be muxed in as a new track too, not just
    its "(Cleaned)" one."""
    suffix = cfg.track_name_suffix if audio_suffix is None else audio_suffix
    a_lang = chosen_audio["properties"].get("language") or "und"
    a_name = clean_label(chosen_audio, suffix)

    # If the file already carries a "(No Narration)"/"(Wordless)" alt track
    # and this call isn't itself adding another one (i.e. this is a normal
    # profanity-cleaning run landing on a file the no-narration batch already
    # touched), its CURRENT disposition decides where the new cleaned track
    # lands - never chosen as the cleaning source either way (see
    # is_no_narration_track/choose_audio):
    #   - already the default (container) track -> stays default/first; the
    #     new cleaned track is added right after it as a non-default alt.
    #   - not the default (some other track, usually the original mix, is)
    #     -> the new cleaned track BECOMES the default/first audio track
    #     instead, with the no-narration track staying right after it -
    #     "cleaned, no narration, original, others".
    existing_no_narr = [t for t in tracks if t["type"] == "audio" and is_no_narration_track(t)]
    adding_no_narration_track = any(m in suffix.lower() for m in NO_NARRATION_NAME_MARKERS)
    no_narr_is_default = bool(existing_no_narr and existing_no_narr[0]["properties"].get("default_track"))
    keep_no_narration_primary = bool(
        audio_default and existing_no_narr and not adding_no_narration_track and no_narr_is_default)
    no_narr_becomes_second = bool(
        audio_default and existing_no_narr and not adding_no_narration_track and not no_narr_is_default)
    if keep_no_narration_primary:
        print(f'  [note] existing No Narration/Wordless track (id {existing_no_narr[0]["id"]}) '
              f'kept primary - new "{a_name}" track added as alt, not default')
        audio_default = False
    elif no_narr_becomes_second:
        print(f'  [note] existing No Narration/Wordless track (id {existing_no_narr[0]["id"]}) '
              f'was not the container default - new "{a_name}" track becomes the new default, '
              f'No Narration moved to second')

    args = [mkvmerge, "-o", str(out_mkv)]
    if audio_default:                                   # clear default on the track(s) it replaces
        for t in tracks:
            if t["type"] == "audio":
                args += ["--default-track-flag", f"{t['id']}:0"]
    # Always explicitly set every passthrough subtitle track's default flag
    # rather than leaving it for mkvmerge to guess - some containers (e.g.
    # MP4) don't carry default-track info the same way Matroska does, and
    # letting mkvmerge infer it here has been observed to mark EVERY
    # subtitle track as default. If we're adding a new cleaned subtitle
    # track it becomes the sole default; otherwise each original track keeps
    # exactly the default state it already had.
    for t in tracks:
        if t["type"] == "subtitles":
            keep = 0 if clean_srt else (1 if t["properties"].get("default_track") else 0)
            args += ["--default-track-flag", f"{t['id']}:{keep}"]
    if exclude_audio_ids:
        args += ["--audio-tracks", "!" + ",".join(str(i) for i in exclude_audio_ids)]
    if exclude_subs_ids:
        args += ["--subtitle-tracks", "!" + ",".join(str(i) for i in exclude_subs_ids)]
    args += [str(media)]

    # Every new file appended to `args` below becomes the next mkvmerge input
    # index after `media` (input 0) - tracked explicitly rather than
    # hard-coded (1, 2, ...) since extra_srt makes the new-input count
    # variable now.
    next_input = 1

    args += ["--language", f"0:{a_lang}", "--track-name", f"0:{a_name}",
             "--default-track-flag", f"0:{1 if audio_default else 0}",
             "--sync", f"0:{cfg.sync_ms}", str(clean_audio)]
    audio_ref = f"{next_input}:0"
    next_input += 1
    print(f'  new audio track: "{a_name}"  [{a_lang}]  {"default" if audio_default else "alt (non-default)"}')

    subs_ref = None
    if clean_srt and chosen_subs is not None:
        s_lang = chosen_subs["properties"].get("language") or "und"
        s_name = clean_label(chosen_subs, cfg.track_name_suffix)
        args += ["--language", f"0:{s_lang}", "--track-name", f"0:{s_name}",
                 "--default-track-flag", "0:1", str(clean_srt)]
        subs_ref = f"{next_input}:0"
        next_input += 1
        print(f'  new subtitle track: "{s_name}"  [{s_lang}]  default')

    extra_subs_ref = None
    if extra_srt is not None:
        e_path, e_meta = extra_srt
        e_lang = e_meta.get("language") or "und"
        e_name = e_meta.get("track_name") or "extra"
        args += ["--language", f"0:{e_lang}", "--track-name", f"0:{e_name}",
                 "--default-track-flag", "0:0", str(e_path)]
        extra_subs_ref = f"{next_input}:0"
        next_input += 1
        print(f'  new subtitle track: "{e_name}"  [{e_lang}]  alt (non-default)')

    order = [f"0:{t['id']}" for t in tracks if t["type"] == "video"]
    orig_audio = [t for t in tracks if t["type"] == "audio"]
    if keep_no_narration_primary:
        no_narr_ids = {t["id"] for t in existing_no_narr}
        order += [f"0:{t['id']}" for t in orig_audio if t["id"] in no_narr_ids]
        order += [audio_ref]
        order += [f"0:{t['id']}" for t in orig_audio if t["id"] not in no_narr_ids]
    elif no_narr_becomes_second:
        no_narr_ids = {t["id"] for t in existing_no_narr}
        order += [audio_ref]
        order += [f"0:{t['id']}" for t in orig_audio if t["id"] in no_narr_ids]
        order += [f"0:{t['id']}" for t in orig_audio if t["id"] not in no_narr_ids]
    else:
        order += [audio_ref] + [f"0:{t['id']}" for t in orig_audio]
    if subs_ref:
        order.append(subs_ref)
    if extra_subs_ref:
        order.append(extra_subs_ref)
    order += [f"0:{t['id']}" for t in tracks if t["type"] == "subtitles"]
    order += [f"0:{t['id']}" for t in tracks
             if t["type"] not in ("video", "audio", "subtitles")]
    args += ["--track-order", ",".join(order)]

    run_mkvmerge(args)


def _load_prev_flagged_words(report_path: Path) -> list[str] | None:
    """Sorted, lowercased list of every word matched across `report_path`'s
    spans (a prior run's own .bleeps.json) - or None if it doesn't exist or
    can't be parsed as one. Used by main() to compare a fresh detection
    pass's word set against what the current (Cleaned) track already
    covers, so a rerun only rebuilds when that set actually changed - see
    is_own_output_track."""
    if not report_path.is_file():
        return None
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    spans = data.get("spans")
    if not isinstance(spans, list):
        return None
    return sorted({str(m).lower() for span in spans for m in span.get("matches", [])})


# ---------------------------------------------------------------------------
def fmt_hms(t: float) -> str:
    h, rem = divmod(max(0.0, t), 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="clean.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("input", help="media file (mkv recommended)")
    p.add_argument("--method", choices=["mute", "bleep", "cut", "dialog"],
                   help="removal method: 'mute' (default, silence, center-channel-aware), "
                        "'bleep', 'cut' (splice out entirely - audio-only inputs only), or "
                        "'dialog' (strip ALL dialogue - not just flagged words - into a new "
                        "(Wordless) track; ignores the wordlists/--categories entirely)")
    p.add_argument("--categories", help="comma list: profanity,irreverence")
    p.add_argument("--extra-spans", dest="extra_spans",
                   help="JSON file of hand-reviewed [{start,end,label,category}] spans (input's "
                        "own local timeline) to remove in addition to whatever the wordlists find "
                        "- e.g. sexual content that can't be safely regex-matched. Always included, "
                        "regardless of --categories.")
    p.add_argument("--pad", type=float, help="shortcut: set --pad-start and --pad-end")
    p.add_argument("--pad-start", dest="pad_start", type=float)
    p.add_argument("--pad-end", dest="pad_end", type=float)
    p.add_argument("--merge-gap", dest="merge_gap", type=float)
    p.add_argument("--beep-hz", dest="beep_hz", type=int, help="'bleep' only")
    p.add_argument("--beep-gain-db", dest="beep_gain_db", type=float, help="'bleep' only")
    p.add_argument("--center-margin-db", dest="center_margin_db", type=float,
                   help="'mute'/'dialog' only: how many dB louder the center channel must be than "
                        "every other channel to mute it alone (default 6)")
    p.add_argument("--mute-fill", dest="mute_fill", choices=["stems", "silence"],
                   help="'mute' only: what plays during a muted span with no clean center channel "
                        "to mute alone - the stemmed-out ambient noise/music (default) or dead "
                        "silence (old behaviour)")
    p.add_argument("--stem-model", dest="stem_model",
                   help="audio-separator model filename used for stemming (mute_fill=stems, and "
                        "--method dialog whenever it can't just mute a center channel)")
    p.add_argument("--dialog-default", dest="dialog_track_default", action="store_true", default=None,
                   help="'dialog' only: make the new (Wordless) track the default audio track "
                        "instead of adding it as a non-default alt track")
    p.add_argument("--clean-codec", dest="clean_codec", choices=["flac", "ac3", "eac3", "aac"],
                   help="'mute'/'bleep' only")
    p.add_argument("--clean-bitrate", dest="clean_bitrate", help="'mute'/'bleep' only, for a <=2ch source")
    p.add_argument("--clean-bitrate-surround", dest="clean_bitrate_surround",
                   help="'mute'/'bleep' only, for a >2ch source")
    p.add_argument("--cut-bitrate", dest="cut_bitrate", help="'cut' only, for a lossy source codec")
    p.add_argument("--source-track", dest="source_track",
                   help='"default", an audio index (0,1,...), or a language (eng)')
    p.add_argument("--sync-ms", dest="sync_ms", type=int, help="delay the clean track by N ms")
    p.add_argument("--subs-track", dest="subs_track",
                   help='SubRip track to clean: "default", index, language, or "none"')
    p.add_argument("--no-subs", dest="subs_track", action="store_const", const="none",
                   help="do not touch subtitles")
    p.add_argument("--no-srt-backfill", dest="srt_backfill", action="store_false", default=None,
                   help="don't cross-check the embedded SRT for words the transcript missed")
    p.add_argument("--no-sidecar-subs", dest="sidecar_subs", action="store_false", default=None,
                   help="don't look for a sidecar subtitle file (e.g. \"Movie.srt\") next to the "
                        "input when there's no usable embedded text/CC608 track to use for "
                        "srt_backfill/censoring")
    p.add_argument("--sidecar-lang", dest="sidecar_lang",
                   help="2- or 3-letter language to prefer when more than one sidecar file exists "
                        "(default: derive from the chosen audio track's own language)")
    p.add_argument("--no-pgs-ocr", dest="pgs_ocr", action="store_false", default=None,
                   help="don't OCR a PGS (Blu-ray bitmap) subtitle track when there's no text/"
                        "sidecar track to use for srt_backfill/censoring")
    p.add_argument("--pgs-ocr-lang", dest="pgs_ocr_lang",
                   help="Tesseract language code for PGS OCR (default: eng)")
    p.add_argument("--no-vobsub-ocr", dest="vobsub_ocr", action="store_false", default=None,
                   help="don't OCR a VobSub (DVD bitmap) subtitle track when there's no text/"
                        "sidecar or PGS track to use for srt_backfill/censoring")
    p.add_argument("--vobsub-ocr-lang", dest="vobsub_ocr_lang",
                   help="Tesseract language code for VobSub OCR (default: eng)")
    p.add_argument("--no-opensubtitles", dest="opensubtitles", action="store_false", default=None,
                   help="don't fall back to OpenSubtitles when there's no local text/sidecar/PGS/"
                        "VobSub subtitle to backfill/censor from (needs an API key - see "
                        "opensubtitles.py)")
    p.add_argument("--opensubtitles-lang", dest="opensubtitles_lang",
                   help='2-letter language to search/download (default "en")')
    p.add_argument("--opensubtitles-query", dest="opensubtitles_query",
                   help="override the OpenSubtitles search title auto-guessed from the filename")
    p.add_argument("--opensubtitles-id", dest="opensubtitles_id",
                   help="exact OpenSubtitles file_id to download - bypasses search entirely")
    p.add_argument("--no-stem-retranscribe", dest="stem_retranscribe", action="store_false",
                   default=None,
                   help="don't isolate vocals and re-transcribe as an absolute last resort when "
                        "the entire subtitle-discovery chain above found nothing at all (needs the "
                        "stemmer - see README)")
    p.add_argument("--output-dir", dest="output_dir",
                   help="scratch dir for temp files + --keep-temp debug artifacts (default 'out') - "
                        "the cleaned result itself replaces the source file in place, it is never "
                        "written here")
    p.add_argument("--retranscribe", action="store_true", default=None)
    p.add_argument("--overwrite", action="store_true", default=None,
                   help="allow clobbering a leftover file at the destination path from an earlier "
                        "run where the extension changed (e.g. a non-.mkv source under mute/bleep/"
                        "dialog); irrelevant when the destination IS the source's own path, since "
                        "that's always replaced")
    p.add_argument("--keep-temp", action="store_true", help="copy the bleeped track + filter graph to output-dir")
    p.add_argument("--dry-run", action="store_true", help="print the spans and stop before ffmpeg/mkvmerge")
    p.add_argument("--force", action="store_true",
                   help="rebuild and replace the existing (Cleaned)/(Wordless) track even if a "
                        "fresh detection pass would flag the exact same set of words - by default "
                        "a rerun on an already-cleaned file skips the rebuild in that case ('mute'/"
                        "'bleep' only; 'cut'/'dialog' always rerun, no word-set to compare)")
    p.add_argument("--record-clean", action="store_true",
                   help="when a fresh (non-already-cleaned) file has nothing flagged, still write "
                        "a <name>.bleeps.json with hits:0/spans:[] instead of writing nothing - "
                        "lets a caller that only checks for that file's existence (e.g. a batch "
                        "driver deciding what's already been checked) tell 'checked, genuinely "
                        "clean' apart from 'never checked' without separate bookkeeping of its "
                        "own. Off by default so a plain interactive run's silence keeps meaning "
                        "'nothing happened here'")
    p.add_argument("--config", default=str(HERE / "config.toml"))
    return p


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    args = build_arg_parser().parse_args(argv)
    cfg = load_config(Path(args.config))

    if args.pad is not None:
        cfg.pad_start = cfg.pad_end = args.pad
    for name in ["method", "pad_start", "pad_end", "merge_gap", "beep_hz",
                 "beep_gain_db", "center_margin_db", "mute_fill", "stem_model",
                 "dialog_track_default", "clean_codec", "clean_bitrate",
                 "clean_bitrate_surround", "cut_bitrate", "source_track",
                 "sync_ms", "subs_track", "srt_backfill", "sidecar_subs", "sidecar_lang",
                 "pgs_ocr", "pgs_ocr_lang", "vobsub_ocr", "vobsub_ocr_lang", "opensubtitles",
                 "opensubtitles_lang", "opensubtitles_query", "opensubtitles_id",
                 "stem_retranscribe", "output_dir", "retranscribe", "overwrite"]:
        val = getattr(args, name, None)
        if val is not None:
            setattr(cfg, name, val)
    if args.categories:
        cfg.categories = [c.strip() for c in args.categories.split(",") if c.strip()]

    tools = locate_tools()
    ffmpeg, ffprobe = tools["ffmpeg"], tools["ffprobe"]
    mkvmerge, mkvextract = tools["mkvmerge"], tools["mkvextract"]

    media = Path(args.input).expanduser().resolve()
    if not media.is_file():
        raise SystemExit(f"[error] not found: {media}")

    is_video = has_video_track(probe_streams(ffprobe, media))
    if cfg.method == "cut":
        if is_video:
            raise SystemExit(
                f"[error] --method cut only supports audio-only input (e.g. audiobooks) - "
                f"{media.name} has a video track, and cutting audio out of a video would "
                f"desync it from the picture. Use --method mute or --method bleep instead.")
        out_ext = media.suffix or ".mp3"
    else:
        if media.suffix.lower() != ".mkv":
            print(f"  [note] input is {media.suffix}, not .mkv - mkvmerge will still "
                  f"try to read it; output is always .mkv")
        out_ext = ".mkv"

    out_dir = Path(cfg.output_dir)
    if not out_dir.is_absolute():
        out_dir = HERE / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    name_suffix = cfg.dialog_track_suffix if cfg.method == "dialog" else cfg.track_name_suffix
    # The cleaned result replaces the source in place - same folder, same
    # stem. Only the extension can change (mkvmerge always writes .mkv), so
    # final_dest usually IS media's own path. out_dir is scratch space only
    # now (temp files during the run, plus --keep-temp debug artifacts) -
    # the source is never overwritten while still being read from; a build
    # is finished in a temp dir first, then the original is moved to the
    # Recycle Bin and the build takes its place (see the end of this
    # function / send_to_recycle_bin).
    final_dest = media.with_name(f"{media.stem}{out_ext}")
    if final_dest != media and final_dest.exists() and not cfg.overwrite:
        raise SystemExit(f"[error] {final_dest} exists (use --overwrite)")

    print(f"== {media.name} ==")
    matchers, js = {}, None
    if cfg.method != "dialog":                    # 'dialog' strips ALL dialogue - no wordlists needed
        matchers = load_matchers(cfg)

    def get_transcript() -> Path:
        """Lazily transcribes the ORIGINAL (mixed) audio, memoized - deferred
        until something below actually needs it (a sidecar/OpenSubtitles
        resync, or confirming a subtitle was found after all) so a file with
        no subtitle candidate anywhere never pays for this AND a second,
        vocals-stem transcription (see Config.stem_retranscribe below): if
        nothing below ever calls this, `js` staying None is exactly the
        signal used to skip straight to stemming instead of transcribing
        twice."""
        nonlocal js
        if js is None:
            js = ensure_transcript(media, cfg)
        return js

    tracks_all = run_json([mkvmerge, "-J", str(media)])["tracks"]
    stale_ids = {t["id"] for t in tracks_all
                if t["type"] in ("audio", "subtitles") and is_own_output_track(t, cfg)}
    already_cleaned = bool(stale_ids)
    # Every track-selection function below gets the STALE-FILTERED list, so a
    # rerun always re-detects from the true original source - never treats a
    # previous run's own (Cleaned)/(Wordless) track as if it were the thing
    # to clean (or, for subtitles, as a real text/PGS track to re-censor).
    # tracks_all (with the stale ids still in it) is kept around for remux()'s
    # exclude_audio_ids/exclude_subs_ids, so a fresh rebuild REPLACES the
    # stale track instead of piling up another one alongside it.
    tracks = [t for t in tracks_all if t["id"] not in stale_ids]
    stale_audio_ids = [t["id"] for t in tracks_all if t["type"] == "audio" and t["id"] in stale_ids]
    stale_subs_ids = [t["id"] for t in tracks_all if t["type"] == "subtitles" and t["id"] in stale_ids]
    if already_cleaned:
        print(f"  [note] {len(stale_ids)} pre-existing (Cleaned)/(Wordless) track(s) found - "
              f"re-detecting from the original source; a fresh build only replaces them if the "
              f"flagged words differ (--force to always replace)")
    chosen, audio_pos = choose_audio(tracks, cfg.source_track)
    cp = chosen["properties"]
    print(f"  cleaning audio #{audio_pos}: "
          f"{cp.get('language', 'und')} / {cp.get('track_name') or '<no name>'} / "
          f"{cp.get('audio_channels', '?')}ch {cp.get('codec_id', '')}")

    subs_enabled = cfg.method not in ("cut", "dialog") and str(cfg.subs_track).lower() not in ("none", "skip", "off", "")
    chosen_subs, subs_tid = (None, None)
    if subs_enabled:
        chosen_subs, subs_tid = choose_subs(tracks, cfg.subs_track, prefer_lang=cp.get("language", ""))

    stem_tool = locate_stem_tool()
    subs_stats = None
    with tempfile.TemporaryDirectory(prefix="pf_", dir=out_dir) as td:
        tmp = Path(td)
        build_path = tmp / f"build{out_ext}"

        raw_srt = None
        resynced_extra_srt = None   # set below only when a sidecar file or OpenSubtitles supplied
        #                             the subtitle source - the resynced-but-UNCENSORED copy, muxed
        #                             in as its own extra non-default track alongside the usual
        #                             (Cleaned) one (see remux()'s extra_srt param)
        if chosen_subs is not None:
            raw_srt = tmp / "orig.srt"
            extract_subs_srt(ffmpeg, mkvextract, media, tracks, chosen_subs, subs_tid, raw_srt)
            if srt_text_len(raw_srt) < _MIN_USABLE_SRT_CHARS:
                print(f"  subtitles: track id {chosen_subs['id']} looks like an empty/placeholder "
                      f"track - trying embedded closed captions instead")
                chosen_subs, subs_tid, raw_srt = None, None, None

        if subs_enabled and chosen_subs is None:
            want_lang = cp.get("language") if cfg.subs_track == "default" else str(cfg.subs_track)
            cc = find_cc608_track(ffprobe, media, want_lang)
            if cc is not None:
                raw_srt = tmp / "orig.srt"
                extract_subs_srt(ffmpeg, mkvextract, media, tracks, cc, None, raw_srt)
                if srt_text_len(raw_srt) >= _MIN_USABLE_SRT_CHARS:
                    chosen_subs, subs_tid = cc, None
                    print(f"  subtitles: using embedded CEA-608 closed captions "
                          f"[{cc['properties']['language']}] (mkvmerge doesn't list this track type)")
                else:
                    raw_srt = None

        if subs_enabled and chosen_subs is None and cfg.sidecar_subs:
            want_lang = cfg.sidecar_lang or cp.get("language") or ""
            found = find_sidecar_subtitle(media, want_lang)
            if found is not None:
                sidecar_path, lang3 = found
                print(f"  subtitles: found sidecar file {sidecar_path.name} [{lang3}] - treating "
                      f"it like an OpenSubtitles download (its timestamps aren't assumed to match "
                      f"this exact file, so it's resynced to the transcript instead of used as-is)")
                raw_for_resync = stage_sidecar_srt(ffmpeg, sidecar_path, tmp)
                synced_path = media.with_name(f"{media.stem}.{lang3}.sidecar.srt")
                result = resync_external_srt(raw_for_resync, get_transcript(), lang3, SIDECAR_TRACK_SUFFIX,
                                             synced_path, f"sidecar ({sidecar_path.name})")
                if result is not None:
                    synced_path, sc_name = result
                    raw_srt = synced_path
                    chosen_subs = {"id": None, "type": "subtitles",
                                  "properties": {"language": lang3, "track_name": sc_name}}
                    subs_tid = None
                    resynced_extra_srt = (synced_path, {"language": lang3, "track_name": sc_name})

        pgs_source_track = None  # set below when a PGS track is what raw_srt/chosen_subs came from -
        #                          censor_pgs_track() (later) needs its cached .sup/.words.json, not clean_srt
        pgs_cache = None  # {"sup_path", "json_path"} from analyze_pgs_track(), for the same reason
        if subs_enabled and chosen_subs is None and cfg.pgs_ocr:
            pgs_track, _pgs_tid = choose_pgs(tracks, cfg.subs_track)
            if pgs_track is not None:
                tesseract_cmd = locate_tesseract()
                if tesseract_cmd is None:
                    print("  [warn] a PGS (Blu-ray bitmap) subtitle track exists but Tesseract "
                          "OCR isn't installed - skipping the OCR fallback "
                          "(winget install --id UB-Mannheim.TesseractOCR)", file=sys.stderr)
                else:
                    if str(HERE) not in sys.path:
                        sys.path.insert(0, str(HERE))
                    import pgs_ocr
                    lang3 = pgs_track["properties"].get("language") or "und"
                    ocr_lang = cfg.pgs_ocr_lang or "eng"
                    cache_base = media.with_name(f"{media.stem}.{lang3}.pgsocr")
                    print(f"  subtitles: no text/sidecar track - OCR'ing PGS track id "
                          f"{pgs_track['id']} [{lang3}] with Tesseract (this can take a "
                          f"while on a full film; cached for reruns)...")
                    stats = pgs_ocr.analyze_pgs_track(
                        mkvextract, media, pgs_track["id"], cache_base,
                        lang=ocr_lang, tesseract_cmd=tesseract_cmd, force=cfg.retranscribe)
                    action = "reusing cached" if stats["cached"] else "OCR'd"
                    print(f"  subtitles: {action} {stats['text_cues']}/{stats['image_cues']} "
                          f"image cue(s) to text ({stats['display_sets']} display sets)")
                    if srt_text_len(stats["srt_path"]) >= _MIN_USABLE_SRT_CHARS:
                        raw_srt = stats["srt_path"]
                        chosen_subs = {"id": None, "type": "subtitles",
                                      "properties": {"language": lang3,
                                                     "track_name": LANG_NAMES.get(lang3, lang3) or lang3}}
                        subs_tid = None
                        pgs_source_track = pgs_track
                        pgs_cache = {"sup_path": stats["sup_path"], "json_path": stats["json_path"]}
                        print("  [note] this subtitle track was OCR'd from bitmap images for srt "
                              "backfill purposes only - the actual (Cleaned) subtitle track will be "
                              "the ORIGINAL bitmap with flagged words redacted in the image itself, "
                              "not a text conversion; expect occasional misreads/missed redactions")
                    else:
                        print("  [warn] PGS OCR produced too little usable text - discarding",
                              file=sys.stderr)

        vobsub_source_track = None  # set below when a VobSub track is what raw_srt/chosen_subs came
        #                             from - censor_vobsub_track() (later) needs its cached
        #                             .idx/.sub/.words.json, not clean_srt
        vobsub_cache = None  # {"idx_path", "sub_path", "json_path"} from analyze_vobsub_track()
        if subs_enabled and chosen_subs is None and cfg.vobsub_ocr:
            vobsub_track, _vobsub_tid = choose_vobsub(tracks, cfg.subs_track)
            if vobsub_track is not None:
                tesseract_cmd = locate_tesseract()
                if tesseract_cmd is None:
                    print("  [warn] a VobSub (DVD bitmap) subtitle track exists but Tesseract "
                          "OCR isn't installed - skipping the OCR fallback "
                          "(winget install --id UB-Mannheim.TesseractOCR)", file=sys.stderr)
                else:
                    if str(HERE) not in sys.path:
                        sys.path.insert(0, str(HERE))
                    import vobsub_ocr
                    lang3 = vobsub_track["properties"].get("language") or "und"
                    ocr_lang = cfg.vobsub_ocr_lang or "eng"
                    cache_base = media.with_name(f"{media.stem}.{lang3}.vobsubocr")
                    print(f"  subtitles: no text/sidecar/PGS track - OCR'ing VobSub track id "
                          f"{vobsub_track['id']} [{lang3}] with Tesseract (this can take a "
                          f"while on a full film; cached for reruns)...")
                    stats = vobsub_ocr.analyze_vobsub_track(
                        mkvextract, media, vobsub_track["id"], cache_base,
                        lang=ocr_lang, tesseract_cmd=tesseract_cmd, force=cfg.retranscribe)
                    action = "reusing cached" if stats["cached"] else "OCR'd"
                    print(f"  subtitles: {action} {stats['text_cues']}/{stats['image_cues']} "
                          f"image cue(s) to text ({stats['entries']} entries)")
                    if srt_text_len(stats["srt_path"]) >= _MIN_USABLE_SRT_CHARS:
                        raw_srt = stats["srt_path"]
                        chosen_subs = {"id": None, "type": "subtitles",
                                      "properties": {"language": lang3,
                                                     "track_name": LANG_NAMES.get(lang3, lang3) or lang3}}
                        subs_tid = None
                        vobsub_source_track = vobsub_track
                        vobsub_cache = {"idx_path": stats["idx_path"], "sub_path": stats["sub_path"],
                                       "json_path": stats["json_path"]}
                        print("  [note] this subtitle track was OCR'd from bitmap images for srt "
                              "backfill purposes only - the actual (Cleaned) subtitle track will be "
                              "the ORIGINAL bitmap with flagged words redacted in the image itself, "
                              "not a text conversion; expect occasional misreads/missed redactions")
                    else:
                        print("  [warn] VobSub OCR produced too little usable text - discarding",
                              file=sys.stderr)

        if subs_enabled and chosen_subs is None and cfg.opensubtitles:
            if str(HERE) not in sys.path:
                sys.path.insert(0, str(HERE))
            import opensubtitles
            lang2 = (cfg.opensubtitles_lang or "").lower() or opensubtitles.LANG_3TO2.get(
                (cp.get("language") or "").lower(), "en")
            print(f"  subtitles: no local text/sidecar/PGS/VobSub track - trying OpenSubtitles "
                  f"[{lang2}]...")
            fetched = opensubtitles.fetch_subtitle(media, cfg, force=cfg.retranscribe)
            if fetched is not None:
                raw_os_srt, os_meta = fetched
                lang3 = opensubtitles.LANG_2TO3.get(lang2, "und")
                synced_path = media.with_name(f"{media.stem}.{lang2}.opensubtitles.srt")
                label = f"OpenSubtitles ({os_meta.get('release') or os_meta.get('file_id')})"
                result = resync_external_srt(raw_os_srt, get_transcript(), lang3, OPENSUBS_TRACK_SUFFIX,
                                             synced_path, label)
                if result is not None:
                    synced_path, os_name = result
                    raw_srt = synced_path
                    chosen_subs = {"id": None, "type": "subtitles",
                                  "properties": {"language": lang3, "track_name": os_name}}
                    subs_tid = None
                    resynced_extra_srt = (synced_path, {"language": lang3, "track_name": os_name})

        vocals_hits = None
        cached_instrumental = None   # a whole-track Instrumental stem produced "for free"
        #   alongside a Config.stem_retranscribe vocals pass below (build_vocals_stem's
        #   also_instrumental) - handed to mute_track() so mute_fill="stems" doesn't stem the
        #   SAME track a second time; only possible when the chosen track has <=2 channels
        #   (see build_vocals_stem's docstring for why a >2ch track can't reuse this)
        if cfg.method != "dialog" and (not subs_enabled or chosen_subs is not None):
            get_transcript()   # normal case: nothing below needed a transcript yet (a subtitle
            #   was already found by embedded/PGS/VobSub, or subs are deliberately disabled) -
            #   get one now, the plain single-STT-call way
        elif subs_enabled and chosen_subs is None:
            print("  subtitles: no usable text subtitle track to clean - skipping")
            print("  [warn] no text subtitle available to cross-check the transcript against - "
                  "words the transcript mis-heard or dropped entirely can't be caught "
                  "automatically for this file (only srt_backfill's safety net is affected; "
                  "muting/bleeping still covers everything the transcript did catch). If you can "
                  "find a subtitle for this movie elsewhere, it can be used to backfill "
                  "and re-run the missed spots.")

            n_ch = int(cp.get("audio_channels") or 2)
            also_instrumental = cfg.method == "mute" and cfg.mute_fill == "stems" and n_ch <= 2

            if not cfg.stem_retranscribe:
                get_transcript()
            elif stem_tool is None:
                print("  [warn] no stemmer installed - can't isolate vocals for a second "
                      "transcription pass on this file with no other safety net (see README "
                      "for one-time stemmer setup)", file=sys.stderr)
                get_transcript()
            elif js is None:
                # Nothing above ever needed a transcript - no embedded/sidecar/PGS/VobSub/
                # OpenSubtitles candidate existed anywhere at all - so THIS is knowable before
                # ever running Voice_to_Text: skip transcribing the original mix entirely and
                # transcribe the isolated vocals instead, exactly once, not twice.
                print("  subtitles: no candidate found anywhere - isolating vocals from the "
                      "audio and transcribing THAT instead of the original mix, since it's the "
                      "only detection pass this file is going to get")
                vocals_wav, cached_instrumental = build_vocals_stem(
                    ffmpeg, stem_tool, media, audio_pos, tmp, cfg,
                    also_instrumental=also_instrumental)
                js = ensure_vocals_transcript(media, vocals_wav, tmp, cfg)
            else:
                # A candidate WAS found (sidecar/OpenSubtitles) and already cost one
                # transcription to try resyncing against - it just didn't line up. Try again on
                # the isolated vocals and merge in only what THAT catches which the first pass
                # missed entirely.
                print("  subtitles: every method above came up empty - isolating vocals from "
                      "the audio and re-transcribing, to give word detection its best possible "
                      "chance on a file with no other safety net")
                vocals_wav, cached_instrumental = build_vocals_stem(
                    ffmpeg, stem_tool, media, audio_pos, tmp, cfg,
                    also_instrumental=also_instrumental)
                vocals_js = ensure_vocals_transcript(media, vocals_wav, tmp, cfg)
                vocals_hits = scan_vocals_transcript(vocals_js, matchers)

        if cfg.method == "dialog":
            print("  method: dialog removal - stripping ALL dialogue (not just flagged words) "
                  "into a new (Wordless) track")
            if args.dry_run:
                print("  (dry run - stopping before ffmpeg / mkvmerge)")
                return 0
            spans, all_hits, total = [], [], 0.0
            clean_audio, used_center = dialog_remove_track(
                ffmpeg, ffprobe, mkvextract, media, audio_pos, cfg, chosen, tmp, stem_tool, tracks)
            clean_srt = None
        else:
            extra_hits = load_extra_spans(args.extra_spans) if args.extra_spans else None
            spans, all_hits = find_spans(js, cfg, matchers, raw_srt, extra_hits, vocals_hits)
            total = sum(e - s for s, e, _ in spans)
            print(f"  flagged: {len(all_hits)} hit(s) -> {len(spans)} span(s) to {cfg.method}, {total:.1f}s")
            for s, e, hs in spans:
                words = ", ".join(sorted({h["match"] for h in hs}))
                tag = "  [srt-only]" if hs and all(h.get("source") == "srt-backfill" for h in hs) else ""
                print(f"    {fmt_hms(s)} - {fmt_hms(e)}  {words}{tag}")
            new_words = sorted({h["match"].lower() for s, e, hs in spans for h in hs})
            if already_cleaned:
                prev_report = final_dest.parent / f"{final_dest.stem}.bleeps.json"
                old_words = _load_prev_flagged_words(prev_report)
                if old_words is not None and old_words == new_words and not args.force:
                    shown = ", ".join(new_words) if new_words else "(none)"
                    print(f"  no change: a fresh detection pass flags the exact same "
                          f"{len(new_words)} word(s) the current (Cleaned) track already covers "
                          f"({shown}) - skipping rebuild (--force to replace anyway)")
                    return 0
                if old_words is not None and old_words != new_words:
                    added = sorted(set(new_words) - set(old_words))
                    removed = sorted(set(old_words) - set(new_words))
                    print(f"  change detected vs. the current (Cleaned) track: "
                          f"+{added or '[]'} -{removed or '[]'} - rebuilding")
                elif args.force:
                    print("  --force: rebuilding regardless of whether the flagged words changed")

            if not spans:
                if already_cleaned:
                    print("  nothing flagged on this pass - leaving the existing (Cleaned) track "
                          "as-is (nothing to rebuild it from)")
                elif args.record_clean:
                    report = final_dest.parent / f"{final_dest.stem}.bleeps.json"
                    report.write_text(json.dumps({
                        "source": str(media),
                        "output": None,
                        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "hits": len(all_hits),
                        "hits_from_srt_backfill": sum(
                            1 for h in all_hits if h.get("source") == "srt-backfill"),
                        "hits_from_vocals_stem": sum(
                            1 for h in all_hits if h.get("source") == "vocals-stem"),
                        "spans": [],
                    }, indent=2), encoding="utf-8")
                    print(f"  nothing flagged - wrote empty {report.name} (--record-clean)")
                else:
                    print("  nothing flagged - no output written")
                return 0
            if args.dry_run:
                print("  (dry run - stopping before ffmpeg / mkvmerge)")
                return 0

            src_codec = None
            if cfg.method == "cut":
                src_audio_streams = [s for s in probe_streams(ffprobe, media) if s.get("codec_type") == "audio"]
                if audio_pos < len(src_audio_streams):
                    src_codec = src_audio_streams[audio_pos].get("codec_name")

            lossless_splice = False
            used_center = None
            if cfg.method == "bleep":
                clean_audio = bleep_track(ffmpeg, media, audio_pos, spans, cfg, chosen, tmp)
            elif cfg.method == "mute":
                clean_audio, used_center = mute_track(ffmpeg, ffprobe, media, audio_pos, spans, cfg,
                                                      chosen, tmp, stem_tool, cached_instrumental)
            elif src_codec == "mp3":
                clean_audio, src_dur, new_dur = mp3_splice_cut(ffmpeg, ffprobe, media, audio_pos, spans, tmp)
                lossless_splice = True
                print(f"  lossless splice (mp3, no re-encode): {src_dur:.1f}s -> {new_dur:.1f}s")
            else:
                clean_audio, _ext = cut_track(ffmpeg, ffprobe, media, audio_pos, spans, cfg, tmp)

            clean_srt = None
            if pgs_source_track is not None:
                # The source track is bitmap (PGS), not text - redact flagged
                # words DIRECTLY IN THE IMAGE (same wordlist match, same span
                # overlap test as censor_srt below) rather than muxing in a
                # text conversion. raw_srt/the OCR'd text above was only ever
                # needed for srt_backfill; the (Cleaned) track that actually
                # gets muxed in stays image-based. See pgs_ocr.censor_pgs_track.
                clean_sup = tmp / "clean.sup"
                pgs_stats = pgs_ocr.censor_pgs_track(
                    pgs_cache["sup_path"], pgs_cache["json_path"], spans, matchers,
                    clean_sup, subs_pad=cfg.subs_pad)
                clean_srt = clean_sup
                subs_stats = {"words_redacted": pgs_stats["words_redacted"]}
                print(f"  subtitles (PGS bitmap): {pgs_stats['words_redacted']} word(s) redacted "
                      f"directly in the image across {pgs_stats['display_sets_touched']} display "
                      f"set(s)")
            elif vobsub_source_track is not None:
                # Same idea as the PGS branch above, for a VobSub (DVD
                # bitmap) source - see vobsub_ocr.censor_vobsub_track.
                clean_idx = tmp / "clean.idx"
                vobsub_stats = vobsub_ocr.censor_vobsub_track(
                    vobsub_cache["idx_path"], vobsub_cache["sub_path"], vobsub_cache["json_path"],
                    spans, matchers, clean_idx, subs_pad=cfg.subs_pad)
                clean_srt = clean_idx
                subs_stats = {"words_redacted": vobsub_stats["words_redacted"]}
                print(f"  subtitles (VobSub bitmap): {vobsub_stats['words_redacted']} word(s) "
                      f"redacted directly in the image across {vobsub_stats['entries_touched']} "
                      f"entries")
            elif raw_srt is not None:
                # mute: audio has no audible trace of the word left, so the
                # subtitle removes it entirely rather than visibly marking
                # it; bleep: the word is audibly replaced by a tone, so the
                # subtitle marks it the same way with subs_mask ("***").
                subs_mask = "" if cfg.method == "mute" else cfg.subs_mask
                new_text, ncues, nwords = censor_srt(
                    raw_srt.read_text(encoding="utf-8-sig"), spans, matchers,
                    subs_mask, cfg.subs_pad)
                clean_srt = tmp / "clean.srt"
                clean_srt.write_text(new_text, encoding="utf-8")
                subs_stats = {"cues": ncues, "words": nwords}
                action = "removed" if subs_mask == "" else f"masked ({subs_mask!r})"
                print(f"  subtitles: {nwords} word(s) {action} across {ncues} cue(s)")

        report = final_dest.parent / f"{final_dest.stem}.bleeps.json"

        def _build_report() -> dict:
            return {
                "source": str(media),
                "output": str(final_dest),
                "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "method": cfg.method,
                "beep_hz": cfg.beep_hz if cfg.method == "bleep" else None,
                "beep_gain_db": cfg.beep_gain_db if cfg.method == "bleep" else None,
                "pad_start": cfg.pad_start if cfg.method != "dialog" else None,
                "pad_end": cfg.pad_end if cfg.method != "dialog" else None,
                "hits": len(all_hits),
                "hits_from_srt_backfill": sum(1 for h in all_hits if h.get("source") == "srt-backfill"),
                "hits_from_vocals_stem": sum(1 for h in all_hits if h.get("source") == "vocals-stem"),
                "subtitles": subs_stats,
                "lossless_splice": lossless_splice if cfg.method == "cut" else None,
                "id3_tags": id3_info if cfg.method == "cut" else None,
                "used_center_channel_trick": used_center,
                "spans": [{"start": round(s, 3), "end": round(e, 3),
                           "matches": sorted({h["match"] for h in hs})} for s, e, hs in spans],
            }

        id3_info = None
        if cfg.method == "cut":
            shutil.copy2(clean_audio, build_path)
            print(f"  cut {total:.1f}s of flagged audio out of the file (no alt track - "
                  f"the duration changed)")
            if src_codec == "mp3":
                id3_info = preserve_id3_tags(media, build_path, spans)
                if id3_info.get("copied"):
                    print(f"  id3 tags: {id3_info['frames']} frame(s) carried over from the source"
                          + (f", {id3_info['chapters_remapped']} chapter mark(s) remapped"
                             if id3_info["chapters_remapped"] else ""))
                else:
                    print(f"  [warn] id3 tags NOT copied ({id3_info.get('reason')})", file=sys.stderr)
            if args.keep_temp and (tmp / "filter.txt").is_file():
                shutil.copy2(tmp / "filter.txt", out_dir / f"{media.stem}.filter.txt")
        elif cfg.method == "dialog":
            try:
                report.write_text(json.dumps(_build_report(), indent=2), encoding="utf-8")
            except OSError:
                pass
            remux(mkvmerge, media, tracks, cfg, build_path, clean_audio, chosen, None, None,
                  audio_suffix=cfg.dialog_track_suffix, audio_default=cfg.dialog_track_default,
                  exclude_audio_ids=stale_audio_ids, exclude_subs_ids=stale_subs_ids)
            if args.keep_temp:
                shutil.copy2(clean_audio, out_dir / f"{media.stem}{name_suffix}{clean_audio.suffix}")
                if (tmp / "filter.txt").is_file():
                    shutil.copy2(tmp / "filter.txt", out_dir / f"{media.stem}.filter.txt")
        else:
            try:
                report.write_text(json.dumps(_build_report(), indent=2), encoding="utf-8")
            except OSError:
                pass
            remux(mkvmerge, media, tracks, cfg, build_path,
                  clean_audio, chosen, clean_srt, chosen_subs,
                  exclude_audio_ids=stale_audio_ids, exclude_subs_ids=stale_subs_ids,
                  extra_srt=resynced_extra_srt)
            if args.keep_temp:
                shutil.copy2(clean_audio, out_dir / f"{media.stem}{cfg.track_name_suffix}{clean_audio.suffix}")
                if (tmp / "filter.txt").is_file():  # not written by mute_track's stemmed-splice path
                    shutil.copy2(tmp / "filter.txt", out_dir / f"{media.stem}.filter.txt")
                if clean_srt:
                    shutil.copy2(clean_srt, out_dir / f"{media.stem}{cfg.track_name_suffix}{clean_srt.suffix}")

        # Build succeeded - now, and only now, touch the source. Stage the
        # finished build next to the destination FIRST (this can be a slow
        # cross-drive copy if out_dir's drive differs from media's - e.g. a
        # scratch dir on D: for a source on J:) while the original is still
        # completely untouched; only once that's safely on disk do we move
        # the pre-clean original to the Recycle Bin (recoverable, never a
        # hard delete) and rename staging over it - a same-drive rename,
        # about as close to atomic as this gets. Done while `tmp` is still
        # alive, before the TemporaryDirectory context below tears it down.
        staging = final_dest.with_name(final_dest.name + ".pf-staging")
        shutil.move(str(build_path), str(staging))
        print(f"  moving original to the Recycle Bin: {media}")
        send_to_recycle_bin(media)
        staging.replace(final_dest)

    report.write_text(json.dumps(_build_report(), indent=2), encoding="utf-8")

    print(f"  wrote: {final_dest}")
    print(f"         {report.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
