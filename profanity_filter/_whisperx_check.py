"""Validate a center-channel mute with WhisperX instead of (or alongside) the
dB-margin heuristic: pick the longest subtitle-confirmed dialogue spans,
transcribe the real audio and a center-muted version of the same spans, and
compare. A clean mute leaves only short generic hallucinated phrases with
near-zero word overlap against the real transcript; real narration bleeding
through shows up as an actual on-topic sentence, high word overlap.

Standalone (not wired into clean.py yet) - used to make faster, safer
fast/slow-path decisions when batch-processing many files. See AGENTS.md
if/when this gets promoted into a real clean.py feature.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import clean

VOICE_TO_TEXT_PY = str(HERE.parent / "voice_to_text" / ".venv" / "Scripts" / "python.exe")
TRANSCRIBE_PY = str(HERE.parent / "voice_to_text" / "transcribe.py")
_WORD_RE = re.compile(r"[a-z']+")


def _words(text: str) -> set[str]:
    return set(_WORD_RE.findall(text.lower()))


def whisperx_validate_center_mute(mkv: Path, audio_pos: int, center_idx: int, layout_name: str,
                                  n_ch: int, tmp: Path, n_windows: int = 3,
                                  overlap_threshold: float = 0.2) -> tuple[bool, list[dict]]:
    """(ok, per_window_detail). ok is True only if EVERY tested window shows a
    clean mute (word overlap between the real and muted transcripts under
    `overlap_threshold`, and the muted transcript isn't suspiciously long)."""
    tools = clean.locate_tools()
    ffmpeg, ffprobe, mkvmerge, mkvextract = (tools["ffmpeg"], tools["ffprobe"],
                                             tools["mkvmerge"], tools["mkvextract"])

    tracks = json.loads(subprocess.run([mkvmerge, "-J", str(mkv)], capture_output=True,
                                       text=True, check=True).stdout)["tracks"]
    subs_track = clean._pick_dialogue_subs_track(tracks)
    if subs_track is None:
        return False, [{"error": "no subtitle track to derive dialogue spans from"}]
    spans = clean.subtitle_dialogue_spans(mkvextract, ffprobe, mkv, subs_track, tmp, merge_gap=0.8)
    if not spans:
        return False, [{"error": "no dialogue spans found"}]
    spans = sorted(spans, key=lambda s: s[1] - s[0], reverse=True)[:n_windows]

    orig_paths, muted_paths = [], []
    for i, (s, e, _) in enumerate(spans):
        orig_51 = tmp / f"w{i}_orig_51.wav"
        subprocess.run([ffmpeg, "-hide_banner", "-y", "-ss", str(s), "-to", str(e),
                        "-i", str(mkv), "-map", f"0:a:{audio_pos}", str(orig_51)],
                       check=True, capture_output=True)
        orig_mono = tmp / f"w{i}_orig_mono.wav"
        subprocess.run([ffmpeg, "-hide_banner", "-y", "-i", str(orig_51),
                        "-ac", "1", "-ar", "16000", str(orig_mono)], check=True, capture_output=True)

        whole = [(0.0, e - s, None)]
        graph = clean.build_mute_filter(whole, 0, n_ch, center_idx, layout_name)
        muted_51 = tmp / f"w{i}_muted_51.wav"
        subprocess.run([ffmpeg, "-hide_banner", "-y", "-ss", str(s), "-to", str(e),
                        "-i", str(mkv), "-filter_complex", graph, "-map", "[out]", str(muted_51)],
                       check=True, capture_output=True)
        muted_mono = tmp / f"w{i}_muted_mono.wav"
        subprocess.run([ffmpeg, "-hide_banner", "-y", "-i", str(muted_51),
                        "-ac", "1", "-ar", "16000", str(muted_mono)], check=True, capture_output=True)

        orig_paths.append(orig_mono)
        muted_paths.append(muted_mono)

    all_paths = orig_paths + muted_paths
    proc = subprocess.run([VOICE_TO_TEXT_PY, "-X", "utf8", TRANSCRIBE_PY, *[str(p) for p in all_paths],
                           "--formats", "txt", "--no-diarize", "--output-dir", str(tmp),
                           "--overwrite", "--quiet"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        return False, [{"error": f"whisperx failed: {proc.stderr[-500:]}"}]

    detail = []
    all_ok = True
    for i, (s, e, _) in enumerate(spans):
        orig_txt = (tmp / f"w{i}_orig_mono.txt").read_text(encoding="utf-8", errors="replace")
        muted_txt = (tmp / f"w{i}_muted_mono.txt").read_text(encoding="utf-8", errors="replace")
        orig_words, muted_words = _words(orig_txt), _words(muted_txt)
        overlap = len(orig_words & muted_words) / max(1, len(orig_words))
        ok = overlap < overlap_threshold and len(muted_words) < 0.5 * max(1, len(orig_words))
        all_ok = all_ok and ok
        detail.append({"span": (round(s, 1), round(e, 1)), "orig": orig_txt.strip(),
                       "muted": muted_txt.strip(), "overlap": round(overlap, 3), "ok": ok})
    return all_ok, detail


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("mkv")
    ap.add_argument("--audio-pos", type=int, default=0)
    ap.add_argument("--center-idx", type=int, default=2)
    ap.add_argument("--layout", default="5.1(side)")
    ap.add_argument("--n-ch", type=int, default=6)
    ap.add_argument("--tmp", default=None)
    args = ap.parse_args()

    tmp = Path(args.tmp) if args.tmp else HERE / "out" / "planet_earth_iii" / "whisperx_check_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    ok, detail = whisperx_validate_center_mute(Path(args.mkv), args.audio_pos, args.center_idx,
                                               args.layout, args.n_ch, tmp)
    for d in detail:
        print(json.dumps(d, indent=2))
    print(f"\nVALIDATED: {ok}")
