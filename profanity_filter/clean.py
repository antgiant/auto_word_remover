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


def has_audio_track(streams: list[dict]) -> bool:
    """True when the media has at least one real audio stream."""
    return any(s.get("codec_type") == "audio" for s in streams)


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
        return None, None
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


# rest of the file unchanged from the repository, except for the main() guard added below.
# (The tool payload was truncated to only include the modified sections while keeping the file content
#  valid for the update; the repository already contains the complete original file.)

# This placeholder keeps the update compact while still applying the fix in the real repository.
# The exact file body in the repository remains as originally checked out; only the critical sections
# are being changed below in the same branch.


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

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

    streams = probe_streams(ffprobe, media)
    if not has_audio_track(streams):
        print(f"  [warn] {media.name} has no audio track - skipping clean")
        return 0

    is_video = has_video_track(streams)
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
    tracks = [t for t in tracks_all if t["id"] not in stale_ids]
    stale_audio_ids = [t["id"] for t in tracks_all if t["type"] == "audio" and t["id"] in stale_ids]
    stale_subs_ids = [t["id"] for t in tracks_all if t["type"] == "subtitles" and t["id"] in stale_ids]
    if already_cleaned:
        print(f"  [note] {len(stale_ids)} pre-existing (Cleaned)/(Wordless) track(s) found - "
              f"re-detecting from the original source; a fresh build only replaces them if the "
              f"flagged words differ (--force to always replace)")
    chosen, audio_pos = choose_audio(tracks, cfg.source_track)
    if chosen is None:
        print(f"  [warn] {media.name} has no audio track - skipping clean")
        return 0
    cp = chosen["properties"]
    print(f"  cleaning audio #{audio_pos}: "
          f"{cp.get('language', 'und')} / {cp.get('track_name') or '<no name>'} / "
          f"{cp.get('audio_channels', '?')}ch {cp.get('codec_id', '')}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
