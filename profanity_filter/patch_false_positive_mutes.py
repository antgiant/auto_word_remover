"""Rapid targeted repair for a file the Parakeet combined-mute audit (parakeet_audit_combined_
mutes, --stt-only run - see AGENTS.md's "Transcription-hallucination detection") found false-
positive mutes in. Restores the ORIGINAL (unmuted) audio for just the specific spans no longer
flagged, spliced in directly from the file's own kept original audio track (mute/bleep always
keeps the pre-clean original as a non-default alt track - nothing external needed). No
re-transcription, no re-stemming, no full --force rebuild: this is a direct, sample-accurate
splice against the EXISTING (Cleaned) track, the cheapest possible fix for a file where only a
handful of spans (or, commonly in this backlog, every span) turned out to be hallucinated.

How it decides what to restore: diffs the file's existing "<name>.bleeps.json" report (the
spans actually muted) against a FRESH --stt-only run's own final "flagged: N hit(s) -> M
span(s)" listing (the spans that would be muted if rebuilt today, with no re-stemming at all -
run this file through clean.py --stt-only first, same as the backlog audit already did, and
pass its log here). Any original span with no corresponding survivor in the new listing is
"confirmed removable" and gets its region restored to the original audio; any span WITH a
surviving match stays muted untouched. This is robust to span-merge boundary shifts (overlap-
based matching, not exact equality) without needing to parse every differently-worded
exclusion-reason block in the log individually.

Usage:
  patch_false_positive_mutes.py <media.mkv> --stt-log <path to the --stt-only run's saved log>
     [--dry-run] [--original-track-id N] [--cleaned-track-id N]

--dry-run prints exactly what would be restored/kept without touching anything - always run
this first.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import clean

HMS_RANGE_RE = re.compile(
    r"^\s+(\d\d:\d\d:\d\d\.\d\d\d) - (\d\d:\d\d:\d\d\.\d\d\d)\s+(.+?)\s*(?:\[srt-only\])?\s*$")


def _hms_to_s(hms: str) -> float:
    h, m, s = hms.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def find_bleeps_report(media: Path) -> Path:
    return media.parent / f"{media.stem}.bleeps.json"


def parse_survivors(log_text: str) -> list[tuple[float, float]]:
    """The final "flagged: N hit(s) -> M span(s) to mute" block's own printed span list - the
    authoritative "what would actually be muted if this file were rebuilt right now" answer,
    straight from clean.py's own find_spans() caller print loop. Returns [(start, end), ...]."""
    idx = log_text.rfind("flagged:")
    if idx == -1:
        raise ValueError("no 'flagged:' line found in this log - is it a real clean.py run?")
    tail = log_text[idx:]
    survivors = []
    for line in tail.splitlines()[1:]:
        m = HMS_RANGE_RE.match(line)
        if not m:
            if line.strip() and not line.startswith(" "):
                break  # ran past the span list into the next section
            continue
        survivors.append((_hms_to_s(m.group(1)), _hms_to_s(m.group(2))))
    return survivors


def diff_spans(bleeps: dict, survivors: list[tuple[float, float]],
               tolerance: float = 2.0) -> tuple[list[dict], list[dict]]:
    """(restore, keep) - an original span counts as kept if ANY survivor span overlaps it
    (within `tolerance` seconds either way, to absorb harmless span-merge boundary shifts);
    otherwise it's confirmed removable."""
    restore, keep = [], []
    for sp in bleeps["spans"]:
        s, e = sp["start"], sp["end"]
        overlaps = any(vs - tolerance <= e and ve + tolerance >= s for vs, ve in survivors)
        (keep if overlaps else restore).append(sp)
    return restore, keep


def find_cleaned_and_original_tracks(tracks: list, cleaned_id: int | None, original_id: int | None):
    audio = [t for t in tracks if t["type"] == "audio"]
    if cleaned_id is not None:
        cleaned = next(t for t in audio if t["id"] == cleaned_id)
    else:
        candidates = [t for t in audio if "(Cleaned)" in (t["properties"].get("track_name") or "")]
        if len(candidates) != 1:
            raise SystemExit(f"[error] expected exactly one '(Cleaned)' audio track, found "
                             f"{len(candidates)} - pass --cleaned-track-id explicitly")
        cleaned = candidates[0]
    if original_id is not None:
        original = next(t for t in audio if t["id"] == original_id)
    else:
        cp = cleaned["properties"]
        candidates = [t for t in audio if t["id"] != cleaned["id"]
                     and "(Cleaned)" not in (t["properties"].get("track_name") or "")
                     and "(Wordless)" not in (t["properties"].get("track_name") or "")
                     and (t["properties"].get("language") or "") == (cp.get("language") or "")
                     and (t["properties"].get("audio_channels") or 0) == (cp.get("audio_channels") or 0)]
        if len(candidates) != 1:
            raise SystemExit(f"[error] expected exactly one matching original audio track, "
                             f"found {len(candidates)} - pass --original-track-id explicitly")
        original = candidates[0]
    return cleaned, original


def patch_audio(media: Path, cleaned: dict, original: dict, restore: list[dict],
                tools: dict, tmp: Path) -> Path:
    """Builds a patched copy of the (Cleaned) track's own audio: every `restore` span's region
    comes from `original`, everything else comes from `cleaned` unchanged - same precise
    seek-and-concat splicing clean.py's own splice_stemmed_spans/splice_whole_track_stem use,
    never the whole-file volume-filter approach (confirmed elsewhere in this codebase to not
    reach true sample-accuracy across a long file)."""
    ffmpeg = tools["ffmpeg"]
    audio_all = sorted((t for t in clean.run_json([tools["mkvmerge"], "-J", str(media)])["tracks"]
                        if t["type"] == "audio"), key=lambda t: t["id"])
    cleaned_pos = [t["id"] for t in audio_all].index(cleaned["id"])
    original_pos = [t["id"] for t in audio_all].index(original["id"])
    n_ch = int(cleaned["properties"].get("audio_channels") or 2)
    sr = int(cleaned["properties"].get("audio_sampling_frequency") or 48000)
    fmt_args = ["-ar", str(sr), "-ac", str(n_ch)]

    total_dur = clean.probe_duration(tools["ffprobe"], media)
    cleaned_wav = tmp / "cleaned_full.wav"
    clean.run([ffmpeg, "-hide_banner", "-y", "-i", str(media), "-map", f"0:a:{cleaned_pos}",
              *fmt_args, str(cleaned_wav)])
    original_wav = tmp / "original_full.wav"
    clean.run([ffmpeg, "-hide_banner", "-y", "-i", str(media), "-map", f"0:a:{original_pos}",
              *fmt_args, str(original_wav)])

    restore_sorted = sorted(restore, key=lambda sp: sp["start"])
    segments: list[Path] = []
    cursor = 0.0

    def extract(src: Path, s: float, e: float) -> Path:
        out = tmp / f"seg_{len(segments)}.wav"
        clean.run([ffmpeg, "-hide_banner", "-y", "-ss", f"{s:.6f}", "-to", f"{e:.6f}",
                  "-i", str(src), *fmt_args, str(out)])
        return out

    for sp in restore_sorted:
        s, e = sp["start"], sp["end"]
        if s > cursor:
            segments.append(extract(cleaned_wav, cursor, s))
        segments.append(extract(original_wav, s, e))
        cursor = e
    if cursor < total_dur:
        segments.append(extract(cleaned_wav, cursor, total_dur))

    list_file = tmp / "patch_concat_list.txt"
    list_file.write_text(
        "\n".join(f"file '{p.resolve().as_posix()}'" for p in segments), encoding="utf-8")
    patched_wav = tmp / "patched.wav"
    clean.run([ffmpeg, "-hide_banner", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
              *fmt_args, str(patched_wav)])
    return patched_wav


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("media", type=Path)
    ap.add_argument("--stt-log", type=Path, required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--original-track-id", type=int, default=None)
    ap.add_argument("--cleaned-track-id", type=int, default=None)
    args = ap.parse_args()

    bleeps_path = find_bleeps_report(args.media)
    bleeps = json.loads(bleeps_path.read_text(encoding="utf-8"))
    log_text = args.stt_log.read_text(encoding="utf-8", errors="replace")
    survivors = parse_survivors(log_text)
    restore, keep = diff_spans(bleeps, survivors)

    print(f"{len(bleeps['spans'])} span(s) currently muted, {len(survivors)} survivor(s) in "
         f"the fresh detection pass -> {len(keep)} span(s) stay muted, {len(restore)} span(s) "
         f"to restore to original audio:")
    for sp in restore:
        print(f"  RESTORE {clean.fmt_hms(sp['start'])} - {clean.fmt_hms(sp['end'])}  "
             f"{', '.join(sp['matches'])}")
    for sp in keep:
        print(f"  keep    {clean.fmt_hms(sp['start'])} - {clean.fmt_hms(sp['end'])}  "
             f"{', '.join(sp['matches'])}")

    if not restore:
        print("\nNothing to restore - this file's existing clean is already correct.")
        return 0
    if args.dry_run:
        print("\n(dry run - stopping before touching anything)")
        return 0

    tools = clean.locate_tools()
    tracks = clean.run_json([tools["mkvmerge"], "-J", str(args.media)])["tracks"]
    cleaned, original = find_cleaned_and_original_tracks(
        tracks, args.cleaned_track_id, args.original_track_id)
    print(f"\ncleaned track: id {cleaned['id']} {cleaned['properties'].get('track_name')!r}")
    print(f"original track: id {original['id']} {original['properties'].get('track_name')!r}")

    import tempfile
    with tempfile.TemporaryDirectory(prefix="pf_patch_", dir=str(clean.HERE / "out")) as tmp_str:
        tmp = Path(tmp_str)
        patched_wav = patch_audio(args.media, cleaned, original, restore, tools, tmp)
        patched_track = clean._encode_track_from_wav(
            tools["ffmpeg"], patched_wav, int(cleaned["properties"].get("audio_channels") or 2),
            clean.Config(), tmp)

        build_path = tmp / f"{args.media.stem}.build.mkv"
        order_args = []
        for t in tracks:
            if t["id"] == cleaned["id"]:
                continue  # replaced below, in the SAME position
            order_args.append(f"0:{t['id']}")
        # Rebuild with every original track in its original relative order, swapping only the
        # stale (Cleaned) track's content for the patched one, in the exact same slot and with
        # the exact same name/language/default flag it already had.
        cp = cleaned["properties"]
        args_mkv = [tools["mkvmerge"], "-o", str(build_path)]
        for t in tracks:
            if t["type"] == "audio":
                is_default = (t["id"] == cleaned["id"])
                args_mkv += ["--default-track-flag", f"{t['id']}:{1 if is_default else 0}"]
        args_mkv += ["--audio-tracks", "!" + str(cleaned["id"]), str(args.media)]
        args_mkv += ["--track-name", f"0:{cp.get('track_name') or ''}",
                    "--language", f"0:{cp.get('language') or 'und'}",
                    "--default-track-flag", "0:1", str(patched_track)]
        # track order: everything else in its original position, with the new patched track
        # taking the old (Cleaned) track's exact slot
        order = []
        for t in tracks:
            if t["id"] == cleaned["id"]:
                order.append("1:0")
            else:
                order.append(f"0:{t['id']}")
        args_mkv += ["--track-order", ",".join(order)]
        clean.run_mkvmerge(args_mkv)

        staging = args.media.with_name(args.media.name + ".pf-patch-staging")
        shutil.move(str(build_path), str(staging))
        print(f"\nmoving original (pre-patch) file to the Recycle Bin: {args.media}")
        clean.send_to_recycle_bin(args.media)
        staging.replace(args.media)
    print(f"patched: {args.media}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
