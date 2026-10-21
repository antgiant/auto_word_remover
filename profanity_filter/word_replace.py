"""word_replace.py -- --method replace: swap a flagged word/phrase for a
different, non-profane one instead of removing it.

Standalone module clean.py wires in at a few specific points (same shallow-
coupling pattern as pgs_ocr.py/vobsub_ocr.py/opensubtitles.py/tmdb.py) - the
only thing clean.py imports from here is replace_track() and
censor_srt_with_replacements(). See AGENTS.md's "Word replacement" section
for the full design and known limitations; this docstring only covers the
mechanics.

Every step below degrades gracefully to muting a hit's own [time, end] span
when it can't produce a confident result - a lookup miss, no donor clip, no
trainable voice model, no .venv-tts installed, a low quality score, or any
unexpected exception. Nothing here should ever abort the whole file over one
bad occurrence.
"""
from __future__ import annotations

import dataclasses
import os
import random
import shutil
import subprocess
import sys
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import flag_language  # noqa: E402

VOICE_TO_TEXT = HERE.parent / "voice_to_text"
GPU_LOCK_DIR = HERE.parent / "gpu_lock"

REPLACEMENTS_PATH = flag_language.WORDLIST_DIR / "replacements.txt"

# Anything this module needs from clean.py's own internals is passed in by
# the caller rather than imported back - clean.py is the one importing THIS
# module (lazily, inside main()'s method dispatch), and clean.py is normally
# run as __main__, not as an importable "clean" module, so a reverse import
# would either be circular at module-load time or (done lazily) silently
# re-execute clean.py as a second, separate module object. See
# replace_track's stem_clip_fn parameter for the pattern this uses instead.


# ---------------------------------------------------------------------------
# Step 1: replacements.txt - load/normalize/select
# ---------------------------------------------------------------------------
_ENTRY_RE = re.compile(r"\s*=>\s*")


def load_replacement_map(path: Path | str = REPLACEMENTS_PATH) -> dict[str, list[str]]:
    """surface-form (normalized) -> ordered list of candidate replacement
    words/phrases, in the file's own priority order. Not reusing
    flag_language.compile_wordlist()/load_matchers() on purpose - those only
    ever produce a match/no-match regex with no concept of replacement
    text, which is a fundamentally different shape than this mapping."""
    path = Path(path)
    mapping: dict[str, list[str]] = {}
    if not path.is_file():
        print(f"[warn] replacements wordlist not found: {path}", file=sys.stderr)
        return mapping
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=>" not in line:
            continue
        left, right = _ENTRY_RE.split(line, maxsplit=1)
        replacements = [r.strip() for r in right.split(",") if r.strip()]
        for surface in (s.strip() for s in left.split(",")):
            key = normalize_surface(surface)
            if key:
                mapping[key] = replacements
    return mapping


def normalize_surface(text: str) -> str:
    """Case/punctuation/whitespace-insensitive key: each whitespace-split
    token is run through flag_language's own punctuation-stripping
    normalizer (_norm_tok) and rejoined with a single space, so "Oh My
    GOD!" and "oh   my god" both normalize to "oh my god" and hit the same
    replacements.txt entry regardless of the transcript's own
    casing/spacing - reusing that normalizer rather than writing a second
    one, since this project already hit a real bug once from two
    independent punctuation-stripping normalizers drifting apart (see
    flag_language's backfill_from_srt / AGENTS.md's matching note)."""
    toks = [flag_language._norm_tok(t) for t in text.split()]
    return " ".join(t for t in toks if t)


def select_replacement_words(hit: dict, repl_map: dict[str, list[str]],
                              max_candidates: int) -> list[str]:
    """Deterministic, rerun-stable: candidates whose character length is
    closest to the matched word's own length sort first; ties broken by
    the file's own left-to-right order. A lookup miss returns []."""
    key = normalize_surface(hit["match"])
    candidates = repl_map.get(key, [])
    if not candidates:
        return []
    original_len = len(key)
    order = sorted(range(len(candidates)),
                   key=lambda i: (abs(len(candidates[i]) - original_len), i))
    return [candidates[i] for i in order[:max_candidates]]


# ---------------------------------------------------------------------------
# Step 1b: pool mode - a flat, un-mapped bucket of replacement words/phrases,
# randomly assigned to EVERY hit regardless of which word was actually
# flagged there. This is a deliberately different shape from
# load_replacement_map/select_replacement_words above (mapped mode): mapped
# mode is reproducible and inspectable (you can always tell which
# replacement a given flagged word will get, which is exactly the property
# pool mode exists to NOT have - see cfg.replace_pool's docstring in
# clean.py's Config and AGENTS.md's "Word replacement" section).
# ---------------------------------------------------------------------------
def load_replacement_pool(path: Path | str) -> list[str]:
    """One replacement candidate (word or phrase) per line - no '=>'
    mapping, unlike load_replacement_map's format; '#' comments and blank
    lines are ignored the same way. Order in the file doesn't matter -
    pool mode draws from it randomly (see select_pool_candidates)."""
    path = Path(path)
    out: list[str] = []
    if not path.is_file():
        print(f"[warn] replacement pool not found: {path}", file=sys.stderr)
        return out
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def select_pool_candidates(hit: dict, pool: list[str], max_candidates: int,
                           seed_key: str) -> list[str]:
    """Randomly samples from the WHOLE pool, deliberately ignoring the
    matched word's own identity/length - the entire point of pool mode is
    that a listener can't infer anything about the original word from the
    replacement, so there is no length-closeness bias to apply here (that
    would itself be a faint signal: a longer replacement would correlate
    with a longer original word). Deterministic per (file, hit) via
    seed_key, not per-process-random - a rerun on unchanged input should
    still make the exact same picks rather than thrashing the rerun-dedup
    cache (see clean.py's already_cleaned word-set comparison) with a
    different output every time nothing actually changed."""
    if not pool:
        return []
    rng = random.Random(seed_key)
    k = min(max_candidates, len(pool))
    return rng.sample(pool, k)


# ---------------------------------------------------------------------------
# Speaker-interval lookup (diarization is forced on for this method - see
# clean.py's main())
# ---------------------------------------------------------------------------
def build_speaker_intervals(units) -> list[tuple[float, float, str]]:
    out = [(u.start, u.end, u.speaker) for u in units
          if u.speaker and u.start is not None and u.end is not None]
    out.sort(key=lambda t: t[0])
    return out


def speaker_at(t: float, intervals: list[tuple[float, float, str]]) -> str | None:
    """Linear scan - a film-length diarized transcript has, at most, a few
    thousand segments; not worth a binary-search structure for that."""
    for s, e, spk in intervals:
        if s <= t <= e:
            return spk
    return None


def collect_speaker_segments(units) -> dict[str, list[tuple[float, float]]]:
    """speaker -> list of (start, end) segment ranges, for ensure_voice_model
    to pull training audio from. Unassigned/unknown-speaker stretches are
    bucketed under "_default" - the rarer case now that diarization is
    forced on for this method, not the normal path it used to be."""
    out: dict[str, list[tuple[float, float]]] = {}
    for u in units:
        if u.start is None or u.end is None:
            continue
        key = u.speaker or "_default"
        out.setdefault(key, []).append((u.start, u.end))
    return out


# ---------------------------------------------------------------------------
# ffmpeg helpers shared by donor extraction and candidate rendering
# ---------------------------------------------------------------------------
def probe_duration(ffprobe: str, media: Path) -> float:
    """Strict duration probe for a real input file - raises on failure,
    since a wrong/unreadable path here is a real error, not something to
    paper over (unlike _probe_duration below, used for short rendered
    candidate clips where a soft 0.0 fallback is the safer default)."""
    out = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(media)],
        capture_output=True, text=True, check=True).stdout.strip()
    return float(out)


def _probe_duration(ffprobe: str, wav_path: Path) -> float:
    try:
        return probe_duration(ffprobe, wav_path)
    except (subprocess.CalledProcessError, ValueError):
        return 0.0


def _atempo_chain(ratio: float) -> list[str]:
    """ffmpeg's own atempo filter is only valid for a per-stage factor in
    [0.5, 2.0] - chain multiple stages for a ratio outside that range."""
    stages = []
    r = ratio
    while r > 2.0:
        stages.append(2.0)
        r /= 2.0
    while r < 0.5:
        stages.append(0.5)
        r /= 0.5
    stages.append(r)
    return [f"atempo={s:.6f}" for s in stages]


def fit_duration(ffmpeg: str, ffprobe: str, src_wav: Path, target_duration_s: float,
                 out_wav: Path) -> Path:
    """Time-stretch src_wav to approximately target_duration_s - used
    identically for a donor clip and a synthesized candidate, since by this
    point both are just "some rendered audio of approximately the right
    words" that needs to fit an exact time slot."""
    cur = _probe_duration(ffprobe, src_wav)
    if cur <= 0 or target_duration_s <= 0:
        shutil.copy2(src_wav, out_wav)
        return out_wav
    ratio = cur / target_duration_s
    filters = ",".join(_atempo_chain(ratio))
    subprocess.run([ffmpeg, "-hide_banner", "-y", "-i", str(src_wav),
                   "-filter:a", filters, str(out_wav)],
                  check=True, capture_output=True)
    return out_wav


def _extract_clip(ffmpeg: str, src: Path, audio_pos: int | None, start: float, end: float,
                  out_wav: Path) -> Path:
    cmd = [ffmpeg, "-hide_banner", "-y", "-ss", f"{max(0.0, start):.6f}",
          "-to", f"{end:.6f}", "-i", str(src)]
    if audio_pos is not None:
        cmd += ["-map", f"0:a:{audio_pos}"]
    cmd.append(str(out_wav))
    subprocess.run(cmd, check=True, capture_output=True)
    return out_wav


def _median_f0(wav_path: Path) -> float | None:
    """Best-effort median F0 for the donor-ranking pitch tiebreak - never
    raises; returns None (meaning "skip the pitch tiebreak") if pyworld
    isn't installed or extraction fails for any reason."""
    try:
        import numpy as np
        import pyworld as pw
        import soundfile as sf
    except ImportError:
        return None
    try:
        x, sr = sf.read(str(wav_path))
        if x.ndim > 1:
            x = x.mean(axis=1)
        x = np.ascontiguousarray(x, dtype=np.float64)
        f0, _t = pw.dio(x, sr)
        voiced = f0[f0 > 0]
        return float(np.median(voiced)) if voiced.size else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Step 3: donor-word search
# ---------------------------------------------------------------------------
@dataclasses.dataclass
class DonorCandidate:
    phrase: str          # the matched phrase, 1+ words, as it appears in the timeline
    start: float
    end: float
    time_distance_s: float
    pitch_distance_hz: float | None = None


def find_donor_clip(replacement_phrase: str, word_timeline: list[dict], all_spans,
                    hit_time: float, min_isolation_s: float, search_window_s: float,
                    media: Path, ffmpeg: str, ffprobe: str, audio_pos: int, tmp: Path,
                    speaker_intervals: list[tuple[float, float, str]] | None = None,
                    hit_speaker: str | None = None) -> DonorCandidate | None:
    """Slides a size-k window (k = word count of replacement_phrase) across
    the file's own word timeline looking for a real spoken instance of the
    exact phrase elsewhere in this same file. Speaker-aware: a candidate
    whose diarized speaker is confirmed DIFFERENT from the hit's own is
    rejected outright, not just deprioritized - a wrong-voice donor is
    never acceptable. When either speaker is unknown (diarization left that
    stretch unassigned), falls back to time-then-pitch proximity, same as
    the undiarized heuristic this replaces for that one candidate."""
    tokens = replacement_phrase.split()
    k = len(tokens)
    if k == 0 or not word_timeline:
        return None
    target = normalize_surface(replacement_phrase)
    ivals = [(s, e) for s, e, _ in all_spans]

    def overlaps_any_span(s: float, e: float) -> bool:
        return any(s < ie and lo < e for lo, ie in ivals)

    candidates: list[DonorCandidate] = []
    n = len(word_timeline)
    for i in range(n - k + 1):
        window = word_timeline[i:i + k]
        if any(w.get("start") is None or w.get("end") is None for w in window):
            continue
        joined = " ".join(w.get("word", "") for w in window)
        if normalize_surface(joined) != target:
            continue
        start, end = window[0]["start"], window[-1]["end"]
        if end - start < min_isolation_s:
            continue
        if overlaps_any_span(start, end):
            continue
        if search_window_s > 0 and abs(start - hit_time) > search_window_s:
            continue
        if speaker_intervals is not None and hit_speaker is not None:
            cand_speaker = speaker_at(start, speaker_intervals)
            if cand_speaker is not None and cand_speaker != hit_speaker:
                continue
        candidates.append(DonorCandidate(phrase=joined, start=start, end=end,
                                         time_distance_s=abs(start - hit_time)))

    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    hit_f0 = None
    try:
        hit_clip = tmp / "_donor_hit_ref.wav"
        _extract_clip(ffmpeg, media, audio_pos, max(0.0, hit_time - 0.5), hit_time + 0.5, hit_clip)
        hit_f0 = _median_f0(hit_clip)
    except Exception:
        hit_f0 = None

    if hit_f0 is not None:
        for i, c in enumerate(candidates):
            try:
                clip = tmp / f"_donor_cand_{i}.wav"
                _extract_clip(ffmpeg, media, audio_pos, c.start, c.end, clip)
                f0 = _median_f0(clip)
                c.pitch_distance_hz = abs(f0 - hit_f0) if f0 is not None else None
            except Exception:
                c.pitch_distance_hz = None

    candidates.sort(key=lambda c: (c.time_distance_s,
                                   c.pitch_distance_hz if c.pitch_distance_hz is not None else 1e9))
    return candidates[0]


def render_donor_candidate(donor: DonorCandidate, target_duration_s: float, media: Path,
                           ffmpeg: str, ffprobe: str, audio_pos: int, tmp: Path, tag: str) -> Path:
    raw = tmp / f"donor_{tag}_raw.wav"
    _extract_clip(ffmpeg, media, audio_pos, donor.start, donor.end, raw)
    fitted = tmp / f"donor_{tag}_fit.wav"
    return fit_duration(ffmpeg, ffprobe, raw, target_duration_s, fitted)


# ---------------------------------------------------------------------------
# Step 4/11 glue: synthesis (delegated to word_replace_synth)
# ---------------------------------------------------------------------------
def _speaker_training_wav(speaker_key: str, speaker_segments: dict[str, list[tuple[float, float]]],
                          vocals_wav: Path | None, ffmpeg: str, tmp: Path) -> Path | None:
    """Concatenate this speaker's segments out of the whole-file isolated
    vocals stem (vocals_wav) into one training clip. None if there's no
    vocals stem available (no stemmer installed) or this speaker has no
    recorded segments at all."""
    segs = speaker_segments.get(speaker_key) or []
    if not segs or vocals_wav is None:
        return None
    seg_dir = tmp / f"vc_train_{speaker_key.replace('/', '_')}"
    seg_dir.mkdir(parents=True, exist_ok=True)
    clips = []
    for i, (s, e) in enumerate(segs):
        clip = seg_dir / f"seg{i}.wav"
        try:
            _extract_clip(ffmpeg, vocals_wav, None, s, e, clip)
            clips.append(clip)
        except subprocess.CalledProcessError:
            continue
    if not clips:
        return None
    list_file = seg_dir / "concat_list.txt"
    list_file.write_text("\n".join(f"file '{p.resolve().as_posix()}'" for p in clips),
                         encoding="utf-8")
    out = seg_dir / "training.wav"
    subprocess.run([ffmpeg, "-hide_banner", "-y", "-f", "concat", "-safe", "0",
                   "-i", str(list_file), str(out)], check=True, capture_output=True)
    return out


def render_synth_candidate(word: str, target_duration_s: float, speaker_key: str,
                           speaker_segments: dict, vocals_wav: Path | None, media: Path,
                           cfg, ffmpeg: str, ffprobe: str, tmp: Path, tag: str,
                           target_f0_hz: float | None = None) -> Path | None:
    """None on any failure (no .venv-tts, not enough speaker audio, driver
    error, ...) - callers treat that exactly like "no candidate produced".

    target_f0_hz: the ORIGINAL word's own median pitch, if it could be read -
    real per-speaker voice conversion isn't available yet (see
    ensure_voice_model's docstring), but the driver can at least pick a
    same-sex neutral voice instead of one fixed voice for every speaker in
    the file. None leaves the driver's own default voice untouched."""
    import word_replace_synth as synth

    driver_py = synth.locate_tts_driver()
    if driver_py is None:
        return None
    training_wav = _speaker_training_wav(speaker_key, speaker_segments, vocals_wav, ffmpeg, tmp)
    voice_model = synth.ensure_voice_model(
        media, cfg, speaker_key, training_wav, tmp, driver_py)
    # voice_model may be None (not enough source audio, or training failed) -
    # synthesize_text still produces a neutral (un-converted) TTS rendering in
    # that case; it's just less likely to match the speaker's timbre, which
    # the scoring pass downstream will naturally penalize via intelligibility
    # if it's bad enough, same as any other candidate.
    raw = synth.synthesize_text(word, target_duration_s, voice_model, driver_py, tmp, tag,
                                target_f0_hz=target_f0_hz)
    if raw is None:
        return None
    fitted = tmp / f"synth_{tag}_fit.wav"
    return fit_duration(ffmpeg, ffprobe, raw, target_duration_s, fitted)


# ---------------------------------------------------------------------------
# Step 5 glue: prosody transplant (delegated to word_replace_prosody)
# ---------------------------------------------------------------------------
def _apply_prosody_if_enabled(cfg, original_clip: Path, candidate_wav: Path, out_wav: Path) -> Path:
    if not cfg.replace_prosody:
        shutil.copy2(candidate_wav, out_wav)
        return out_wav
    try:
        import word_replace_prosody as prosody
        contour = prosody.extract_prosody(original_clip)
        if contour is None:
            shutil.copy2(candidate_wav, out_wav)
            return out_wav
        return prosody.apply_prosody(candidate_wav, contour, out_wav)
    except Exception as exc:
        print(f"  [warn] replace: prosody transplant failed ({exc!r}) - using the "
              f"candidate's own natural prosody instead", file=sys.stderr)
        shutil.copy2(candidate_wav, out_wav)
        return out_wav


# ---------------------------------------------------------------------------
# Step 7 glue: scene blending (delegated to word_replace_blend)
# ---------------------------------------------------------------------------
def _apply_scene_blend_if_enabled(cfg, candidate_wav: Path, neighbor_before: Path,
                                  neighbor_after: Path, instrumental_clip: Path | None,
                                  ffmpeg: str, ffprobe: str, out_wav: Path) -> Path:
    if not cfg.replace_scene_blend:
        shutil.copy2(candidate_wav, out_wav)
        return out_wav
    try:
        import word_replace_blend as blend
        return blend.blend_candidate(candidate_wav, neighbor_before, neighbor_after,
                                     instrumental_clip, out_wav, ffmpeg, ffprobe)
    except Exception as exc:
        print(f"  [warn] replace: scene blending failed ({exc!r}) - using the "
              f"candidate unblended", file=sys.stderr)
        shutil.copy2(candidate_wav, out_wav)
        return out_wav


# ---------------------------------------------------------------------------
# Per-hit candidate generation (steps 3-5, 7 combined - the fallback chain's
# "produce some renders" half; scoring/picking is a separate, batched pass
# below so the whole file's ASR re-check happens in one subprocess call)
# ---------------------------------------------------------------------------
@dataclasses.dataclass
class CandidateRender:
    hit_idx: int
    word: str
    kind: str            # "donor" | "synth"
    wav_path: Path
    target_duration_s: float
    asr_text: str | None = None
    score: float | None = None


def _build_candidates_for_hit(hit_idx: int, hit_time: float, hit_end: float, hit_speaker: str | None,
                              words: list[str], word_timeline: list[dict], all_spans,
                              speaker_intervals, speaker_segments, vocals_wav: Path | None,
                              media: Path, cfg, ffmpeg: str, ffprobe: str, audio_pos: int,
                              tmp: Path, span_start: float, span_end: float) -> list[CandidateRender]:
    # Must match _splice_spans' own hs_/he_ clamp exactly - a candidate is
    # spliced into THAT padded, span-clamped slot, not the bare [hit_time,
    # hit_end] match window, so fitting it to the narrower unpadded window
    # left the final concat short by pad_start+pad_end every time a hit
    # was actually replaced - confirmed live 2026-10-21 (a delivered clip
    # came back 0.195s shorter than its source, matching pad_start=0.1 +
    # pad_end=0.1 almost exactly).
    hs_ = max(span_start, min(span_end, hit_time - cfg.pad_start))
    he_ = max(span_start, min(span_end, hit_end + cfg.pad_end))
    target_duration_s = max(0.05, he_ - hs_)
    original_clip = tmp / f"hit{hit_idx}_original.wav"
    try:
        _extract_clip(ffmpeg, media, audio_pos, hit_time, hit_end, original_clip)
    except subprocess.CalledProcessError:
        original_clip = None
    neighbor_before = tmp / f"hit{hit_idx}_nb_before.wav"
    neighbor_after = tmp / f"hit{hit_idx}_nb_after.wav"
    try:
        _extract_clip(ffmpeg, media, audio_pos, max(0.0, hit_time - 1.0), hit_time, neighbor_before)
        _extract_clip(ffmpeg, media, audio_pos, hit_end, hit_end + 1.0, neighbor_after)
    except subprocess.CalledProcessError:
        neighbor_before = neighbor_after = None

    # Crude but real: a neutral single-voice TTS can't match the speaker's
    # actual timbre without voice conversion (see ensure_voice_model's
    # docstring - rvc-python has no training API, so that path is dead for
    # now), but it CAN at least pick a plausible same-sex voice rather than
    # one fixed voice for everyone - confirmed live 2026-10-21 that using a
    # single male voice regardless of the original speaker reads as a real,
    # noticeable mismatch, not just a quality nitpick. speaker_target_f0_hz
    # feeds word_replace_tts_driver's voice pick; None if pitch couldn't be
    # read from the original clip, which just leaves the driver's own default.
    speaker_target_f0_hz = _median_f0(original_clip) if original_clip is not None else None

    renders: list[CandidateRender] = []
    speaker_key = hit_speaker or "_default"
    for wi, word in enumerate(words):
        tag = f"{hit_idx}_{wi}"

        donor = find_donor_clip(word, word_timeline, all_spans, hit_time,
                                cfg.replace_min_donor_isolation_s,
                                cfg.replace_donor_search_window_s, media, ffmpeg, ffprobe,
                                audio_pos, tmp, speaker_intervals, hit_speaker)
        if donor is not None:
            try:
                raw = render_donor_candidate(donor, target_duration_s, media, ffmpeg, ffprobe,
                                             audio_pos, tmp, f"{tag}_donor")
                renders.append(_finish_candidate(hit_idx, word, "donor", raw, target_duration_s,
                                                 original_clip, neighbor_before, neighbor_after,
                                                 cfg, ffmpeg, ffprobe, tmp, f"{tag}_donor"))
            except Exception as exc:
                print(f"  [warn] replace: donor render failed for {word!r} ({exc!r})",
                      file=sys.stderr)

        try:
            raw = render_synth_candidate(word, target_duration_s, speaker_key, speaker_segments,
                                         vocals_wav, media, cfg, ffmpeg, ffprobe, tmp,
                                         f"{tag}_synth", target_f0_hz=speaker_target_f0_hz)
            if raw is not None:
                renders.append(_finish_candidate(hit_idx, word, "synth", raw, target_duration_s,
                                                 original_clip, neighbor_before, neighbor_after,
                                                 cfg, ffmpeg, ffprobe, tmp, f"{tag}_synth"))
        except Exception as exc:
            print(f"  [warn] replace: synthesis failed for {word!r} ({exc!r})", file=sys.stderr)

    return renders


def _finish_candidate(hit_idx, word, kind, raw_wav, target_duration_s, original_clip,
                      neighbor_before, neighbor_after, cfg, ffmpeg, ffprobe, tmp, tag
                      ) -> CandidateRender:
    prosodied = tmp / f"cand_{tag}_prosody.wav"
    if original_clip is not None:
        prosodied = _apply_prosody_if_enabled(cfg, original_clip, raw_wav, prosodied)
    else:
        shutil.copy2(raw_wav, prosodied)
    blended = tmp / f"cand_{tag}_blend.wav"
    if neighbor_before is not None and neighbor_after is not None:
        blended = _apply_scene_blend_if_enabled(cfg, prosodied, neighbor_before, neighbor_after,
                                                None, ffmpeg, ffprobe, blended)
    else:
        shutil.copy2(prosodied, blended)
    return CandidateRender(hit_idx=hit_idx, word=word, kind=kind, wav_path=blended,
                           target_duration_s=target_duration_s)


# ---------------------------------------------------------------------------
# Step 6: batched multi-candidate scoring (delegated to word_replace_score /
# voice_to_text/transcribe_clip_batch.py)
# ---------------------------------------------------------------------------
def _score_all_candidates(all_renders: list[CandidateRender], cfg, ffprobe: str, tmp: Path) -> None:
    """Mutates each render's .asr_text/.score in place. One batched
    transcription subprocess for every candidate across the WHOLE file,
    regardless of hit count - see word_replace_score.run_batch_asr."""
    if not all_renders:
        return
    import word_replace_score as score_mod

    clips = [(f"c{i}", r.wav_path) for i, r in enumerate(all_renders)]
    texts = score_mod.run_batch_asr(clips, tmp, cfg.replace_score_model, VOICE_TO_TEXT)
    for i, r in enumerate(all_renders):
        asr_text = texts.get(f"c{i}", "")
        r.asr_text = asr_text
        actual = _probe_duration(ffprobe, r.wav_path) or r.target_duration_s
        sc = score_mod.score_candidate(asr_text, r.word, actual, r.target_duration_s)
        r.score = sc.combined
        print(f"    replace: candidate {r.word!r} ({r.kind}) heard as {asr_text!r} "
              f"-> intelligibility={sc.intelligibility:.2f} duration_fit={sc.duration_fit:.2f} "
              f"combined={sc.combined:.2f}")


# ---------------------------------------------------------------------------
# Step 8: orchestration entry point
# ---------------------------------------------------------------------------
def replace_track(ffmpeg: str, ffprobe: str, media: Path, audio_pos: int, spans, cfg,
                  chosen: dict, tmp: Path, js: Path, stem_tool: str | None = None,
                  cached_whole_instrumental: Path | None = None,
                  stem_clip_fn=None) -> tuple[Path, list[dict]]:
    """Returns (clean_audio_path, per_hit_outcomes). per_hit_outcomes feeds
    censor_srt_with_replacements() and clean.py's .bleeps.json report.

    Reuses the same seek-and-concat-demuxer splice skeleton clean.py's own
    splice_stemmed_spans/splice_whole_track_stem use (see AGENTS.md) at the
    SPAN level - the per-span "processing" step is swapped: instead of
    stemming the whole padded clip, each hit inside the span is resolved
    independently (steps 1-7 of the fallback chain) and spliced into the
    clip at its own position; gaps between hits (and the span's own
    padding) pass through untouched."""
    props = chosen["properties"]
    n_ch = int(props.get("audio_channels") or 2)
    sr = int(props.get("audio_sampling_frequency") or 48000)
    layout_name = None
    if n_ch > 2:
        # Same derivation mute_track() uses - a raw ffprobe channel_layout
        # string (e.g. "5.1(side)"), not an mkvmerge property; stem_clip_fn
        # (stem_clip_all_channels) falls back to a generic "<n>c" layout on
        # its own if this isn't one it recognizes, so passing it through
        # unchecked here is safe either way.
        layout_name = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", f"a:{audio_pos}",
             "-show_entries", "stream=channel_layout", "-of", "csv=p=0", str(media)],
            capture_output=True, text=True, check=True).stdout.strip() or None
    total_dur = probe_duration(ffprobe, media)

    repl_map = {} if cfg.replace_pool else load_replacement_map(cfg.replace_wordlist)
    pool = load_replacement_pool(cfg.replace_pool) if cfg.replace_pool else []
    word_timeline = flag_language.load_word_timeline(js)
    _fmt, units = flag_language.parse_transcript(js)
    speaker_intervals = build_speaker_intervals(units)
    speaker_segments = collect_speaker_segments(units)

    vocals_wav = None
    if stem_tool is not None:
        try:
            vocals_wav = _ensure_file_vocals_stem(ffmpeg, stem_tool, media, audio_pos, cfg, tmp)
        except Exception as exc:
            print(f"  [warn] replace: couldn't build an isolated vocals stem for "
                  f"voice-conversion training ({exc!r}) - synthesis will fall back to a "
                  f"neutral (un-converted) voice", file=sys.stderr)

    flat_hits = [(s, e, h) for s, e, hs in spans for h in hs]
    all_renders: list[CandidateRender] = []
    hit_meta: list[dict] = []
    hit_render_ranges: list[tuple[int, int]] = []  # [start, end) index into all_renders, per hit

    for idx, (s, e, h) in enumerate(flat_hits):
        hit_time = h["time"]
        hit_end = h["end"] if h["end"] is not None else hit_time + 0.6
        hit_meta.append({"time": hit_time, "end": hit_end, "match": h["match"],
                         "speaker": h.get("speaker")})
        if cfg.replace_pool:
            seed_key = f"{media}::{hit_time:.3f}::{h['match']}"
            words = select_pool_candidates(h, pool, cfg.replace_max_word_candidates, seed_key)
        else:
            words = select_replacement_words(h, repl_map, cfg.replace_max_word_candidates)
        start_i = len(all_renders)
        if words:
            try:
                renders = _build_candidates_for_hit(
                    idx, hit_time, hit_end, h.get("speaker"), words, word_timeline, spans,
                    speaker_intervals, speaker_segments, vocals_wav, media, cfg, ffmpeg,
                    ffprobe, audio_pos, tmp, s, e)
                all_renders.extend(renders)
            except Exception as exc:
                print(f"  [warn] replace: candidate generation failed for "
                      f"{h['match']!r} ({exc!r}) - muting this occurrence instead",
                      file=sys.stderr)
        hit_render_ranges.append((start_i, len(all_renders)))

    _score_all_candidates(all_renders, cfg, ffprobe, tmp)

    import word_replace_score as score_mod
    per_hit_outcomes: list[dict] = []
    winners: list[CandidateRender | None] = []
    for idx, meta in enumerate(hit_meta):
        start_i, end_i = hit_render_ranges[idx]
        candidates = all_renders[start_i:end_i]
        best = score_mod.pick_best_candidate(candidates, cfg.replace_quality_threshold)
        winners.append(best)
        per_hit_outcomes.append({
            "time": meta["time"], "end": meta["end"], "match": meta["match"],
            "outcome": best.kind if best else "mute",
            "replacement_word": best.word if best else None,
        })

    n_muted = sum(1 for o in per_hit_outcomes if o["outcome"] == "mute")
    if cfg.replace_pool:
        n_missed = len(flat_hits) if not pool else 0
        if n_missed:
            print(f"  replace: {n_missed} of {len(flat_hits)} word(s) had no replacement - "
                  f"{Path(cfg.replace_pool).name} is empty - muted instead")
    else:
        n_missed = sum(1 for h in flat_hits
                       if not select_replacement_words(h[2], repl_map, cfg.replace_max_word_candidates))
        if n_missed:
            print(f"  replace: {n_missed} of {len(flat_hits)} word(s) had no replacement authored "
                  f"in {Path(cfg.replace_wordlist).name} - muted instead")
    if n_muted - n_missed > 0:
        print(f"  replace: {n_muted - n_missed} more word(s) fell back to mute "
              f"(no usable donor/synthesis, or every candidate scored below "
              f"{cfg.replace_quality_threshold})")
    print(f"  replace: {len(flat_hits) - n_muted}/{len(flat_hits)} word(s) actually replaced")

    clean_wav = _splice_spans(ffmpeg, ffprobe, media, audio_pos, spans, flat_hits, winners,
                              n_ch, sr, layout_name, cfg, tmp, stem_tool,
                              cached_whole_instrumental, stem_clip_fn)
    return clean_wav, per_hit_outcomes


_VOCALS_STEM_CACHE: dict[str, Path] = {}


def _ensure_file_vocals_stem(ffmpeg: str, stem_tool: str, media: Path, audio_pos: int, cfg,
                             tmp: Path) -> Path:
    """Whole-file isolated vocals stem, built once per replace_track() call
    and reused across every speaker's voice-model training - reuses the
    same stemming infra build_vocals_stem relies on (a plain stereo
    downmix through audio-separator's Vocals output), just invoked
    directly here rather than importing clean.py (see this module's
    docstring on why that back-import is avoided)."""
    key = str(media)
    if key in _VOCALS_STEM_CACHE and _VOCALS_STEM_CACHE[key].is_file():
        return _VOCALS_STEM_CACHE[key]
    src_wav = tmp / "_replace_vocals_src.wav"
    subprocess.run([ffmpeg, "-hide_banner", "-y", "-i", str(media),
                   "-map", f"0:a:{audio_pos}", "-ac", "2", str(src_wav)],
                  check=True, capture_output=True)
    out_dir = tmp / "_replace_vocals_out"
    out_dir.mkdir(exist_ok=True)
    cmd = [stem_tool, str(src_wav), "-m", cfg.stem_model, "--output_dir", str(out_dir),
          "--output_format", "WAV", "--single_stem", "Vocals"]
    # audio-separator's pydub backend writes temp files via the OS default
    # temp dir regardless of --output_dir - redirect TEMP/TMP so they land
    # on the same (spacious) drive as everything else this run writes,
    # same fix _invoke_separator applies in clean.py.
    env = dict(os.environ, TEMP=str(out_dir), TMP=str(out_dir))
    with _gpu_lock_ctx("replace: vocals (voice-model training)"):
        subprocess.run(cmd, check=True, env=env)
    matches = sorted(out_dir.glob("*Vocals*.wav"))
    if not matches:
        raise RuntimeError(f"stemmer produced no Vocals output in {out_dir}")
    _VOCALS_STEM_CACHE[key] = matches[0]
    return matches[0]


def _gpu_lock_ctx(reason: str):
    import contextlib
    if str(GPU_LOCK_DIR) not in sys.path:
        sys.path.insert(0, str(GPU_LOCK_DIR))
    try:
        import gpu_lock
    except ImportError:
        return contextlib.nullcontext()
    return gpu_lock.hold("Profanity_Filter", reason)


def _mute_clip(ffmpeg: str, ffprobe: str, stem_tool: str | None, clip_wav: Path, n_ch: int,
              sr: int, layout_name: str | None, cfg, tmp: Path, stem_clip_fn, tag: str) -> Path:
    """Mute fallback for one hit's clip - reuses the caller's own
    stem_clip_all_channels (passed in as stem_clip_fn, see replace_track's
    docstring on why clean.py hands this in rather than word_replace.py
    importing clean.py back) when a stemmer is available, else plain
    silence of the same duration - the same two-tier fallback mute_track's
    own mute_fill="stems" path uses."""
    if stem_tool is not None and stem_clip_fn is not None:
        try:
            span_tmp = tmp / f"mute_{tag}"
            span_tmp.mkdir(exist_ok=True)
            return stem_clip_fn(ffmpeg, stem_tool, clip_wav, n_ch, sr, span_tmp, cfg, layout_name)
        except Exception as exc:
            print(f"  [warn] replace: stemmed mute fallback failed ({exc!r}) - using "
                  f"plain silence instead", file=sys.stderr)
    dur = _probe_duration(ffprobe, clip_wav)
    out = tmp / f"mute_{tag}_silence.wav"
    subprocess.run([ffmpeg, "-hide_banner", "-y", "-f", "lavfi",
                   "-i", f"anullsrc=channel_layout={n_ch}c:sample_rate={sr}",
                   "-t", f"{max(0.01, dur):.6f}", str(out)], check=True, capture_output=True)
    return out


def _splice_spans(ffmpeg: str, ffprobe: str, media: Path, audio_pos: int, spans, flat_hits,
                  winners: list[CandidateRender | None], n_ch: int, sr: int,
                  layout_name: str | None, cfg, tmp: Path, stem_tool: str | None,
                  cached_whole_instrumental: Path | None, stem_clip_fn) -> Path:
    seg_dir = tmp / "replace_splice_segments"
    seg_dir.mkdir(parents=True, exist_ok=True)
    fmt_args = ["-ar", str(sr), "-ac", str(n_ch)]
    total_dur = probe_duration(ffprobe, media)
    segments: list[Path] = []
    cursor = 0.0

    def extract_original(s: float, e: float) -> Path:
        out = seg_dir / f"orig_{len(segments)}.wav"
        subprocess.run([ffmpeg, "-hide_banner", "-y", "-ss", f"{s:.6f}", "-to", f"{e:.6f}",
                       "-i", str(media), "-map", f"0:a:{audio_pos}", *fmt_args, str(out)],
                      check=True, capture_output=True)
        return out

    def conform(src_wav: Path) -> Path:
        """Every spliced-in segment must share the concat demuxer's exact
        sample rate/channel count, same as splice_stemmed_spans already
        requires of its own stemmed clips."""
        out = seg_dir / f"conform_{len(segments)}.wav"
        subprocess.run([ffmpeg, "-hide_banner", "-y", "-i", str(src_wav), *fmt_args, str(out)],
                      check=True, capture_output=True)
        return out

    def background_for_span(hs_: float, he_: float, tag: str) -> Path | None:
        """The stemmed-out ambient/music bed for exactly [hs_, he_] - the
        SAME thing mute_fill="stems" plays through a muted span, which a
        winning replace candidate must ALSO be mixed over, or the
        replacement word plays in dead silence while the mute fallback
        right next to it keeps the scene's music/ambience going (confirmed
        live 2026-10-21: a real delivered clip went fully silent under the
        replaced word - this was the missing step). None if there's no
        stemmer available; the caller then uses the dry candidate as-is,
        same posture as mute_fill="stems" falling back to silence."""
        if cached_whole_instrumental is not None:
            out = seg_dir / f"bg_cached_{tag}.wav"
            subprocess.run([ffmpeg, "-hide_banner", "-y", "-ss", f"{hs_:.6f}", "-to", f"{he_:.6f}",
                           "-i", str(cached_whole_instrumental), *fmt_args, str(out)],
                          check=True, capture_output=True)
            return out
        if stem_tool is None or stem_clip_fn is None:
            return None
        try:
            raw = extract_original(hs_, he_)
            span_tmp = tmp / f"replace_bg_{tag}"
            span_tmp.mkdir(exist_ok=True)
            return stem_clip_fn(ffmpeg, stem_tool, raw, n_ch, sr, span_tmp, cfg, layout_name)
        except subprocess.CalledProcessError as exc:
            print(f"  [warn] replace: couldn't stem a background bed for the replaced word "
                  f"({exc!r}) - it will play without scene ambience under it", file=sys.stderr)
            return None

    def mix_with_background(candidate_wav: Path, bg_wav: Path, tag: str) -> Path:
        out = seg_dir / f"mixed_{tag}.wav"
        subprocess.run([ffmpeg, "-hide_banner", "-y", "-i", str(candidate_wav), "-i", str(bg_wav),
                       "-filter_complex", "amix=inputs=2:duration=first:dropout_transition=0",
                       *fmt_args, str(out)], check=True, capture_output=True)
        return out

    hit_i = 0
    for s, e, hs in spans:
        span_hits = [(flat_hits[hit_i + j][2]["time"], flat_hits[hit_i + j][2],
                     winners[hit_i + j]) for j in range(len(hs))]
        span_hits.sort(key=lambda t: t[0])

        if s > cursor:
            segments.append(extract_original(cursor, s))

        inner_cursor = s
        for j, (_t, h, winner) in enumerate(span_hits):
            hit_time = h["time"]
            hit_end = h["end"] if h["end"] is not None else hit_time + 0.6
            hs_ = max(s, min(e, hit_time - cfg.pad_start))
            he_ = max(s, min(e, hit_end + cfg.pad_end))
            if hs_ > inner_cursor:
                segments.append(extract_original(inner_cursor, hs_))
            if winner is not None:
                conformed = conform(winner.wav_path)
                bg = background_for_span(hs_, he_, tag=f"{hit_i + j}")
                segments.append(mix_with_background(conformed, bg, tag=f"{hit_i + j}")
                                if bg is not None else conformed)
            elif cached_whole_instrumental is not None:
                # Already-stemmed whole-track Instrumental available for free
                # (same cached_instrumental mute_track() itself prefers) -
                # slice straight from it instead of re-stemming this one
                # clip from scratch.
                out = seg_dir / f"cached_inst_{len(segments)}.wav"
                subprocess.run([ffmpeg, "-hide_banner", "-y", "-ss", f"{hs_:.6f}",
                               "-to", f"{he_:.6f}", "-i", str(cached_whole_instrumental),
                               *fmt_args, str(out)], check=True, capture_output=True)
                segments.append(out)
            else:
                raw = extract_original(hs_, he_)
                muted = _mute_clip(ffmpeg, ffprobe, stem_tool, raw, n_ch, sr, layout_name, cfg,
                                   tmp, stem_clip_fn, tag=f"span_{hit_i + j}")
                segments.append(conform(muted))
            inner_cursor = he_
        if inner_cursor < e:
            segments.append(extract_original(inner_cursor, e))

        hit_i += len(hs)
        cursor = e

    if cursor < total_dur:
        segments.append(extract_original(cursor, total_dur))

    list_file = seg_dir / "concat_list.txt"
    list_file.write_text("\n".join(f"file '{p.resolve().as_posix()}'" for p in segments),
                         encoding="utf-8")
    spliced = tmp / "spliced.wav"
    subprocess.run([ffmpeg, "-hide_banner", "-y", "-f", "concat", "-safe", "0",
                   "-i", str(list_file), *fmt_args, str(spliced)],
                  check=True, capture_output=True)
    return spliced


# ---------------------------------------------------------------------------
# Step 9: subtitle substitution
# ---------------------------------------------------------------------------
_SRT_TIME = re.compile(r"(\d{1,3}):([0-5]?\d):([0-5]?\d)[,.](\d{1,3})")


def _srt_sec(m) -> float:
    h, mm, ss, ms = m
    return int(h) * 3600 + int(mm) * 60 + int(ss) + int(ms.ljust(3, "0")) / 1000.0


def _match_case(original: str, replacement: str) -> str:
    if original.isupper():
        return replacement.upper()
    if original[:1].isupper():
        return " ".join(w[:1].upper() + w[1:] for w in replacement.split())
    return replacement


def censor_srt_with_replacements(text: str, spans, matchers: dict, per_hit_outcomes: list[dict],
                                 pad: float) -> tuple[str, int, int]:
    """Same cue-block/time-overlap scan clean.censor_srt() already does,
    but each regex match is resolved against per_hit_outcomes (keyed by
    time) instead of one global mask string: a "mute" outcome deletes the
    word (identical to method="mute"'s own subtitle behaviour); a
    "donor"/"synth" outcome substitutes the chosen replacement word,
    case-matched to the original matched text."""
    ivals = [(s - pad, e + pad) for s, e, _ in spans]
    outcomes_by_time = sorted(per_hit_outcomes, key=lambda o: o["time"])

    def outcome_for(cs: float, ce: float) -> dict | None:
        best, best_d = None, None
        for o in outcomes_by_time:
            ot, oe = o["time"], o["end"]
            if ot - pad <= ce and oe + pad >= cs:
                mid = (ot + oe) / 2.0
                d = abs(mid - (cs + ce) / 2.0)
                if best is None or d < best_d:
                    best, best_d = o, d
        return best

    blocks = re.split(r"\r?\n\r?\n", text.lstrip("﻿").strip())
    out, cues_hit, words = [], 0, 0
    for block in blocks:
        lines = [ln.rstrip("\r") for ln in block.split("\n")]
        ti = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        times = _SRT_TIME.findall(lines[ti]) if ti is not None else []
        if ti is None or len(times) < 2:
            out.append(block)
            continue
        cue_s, cue_e = _srt_sec(times[0]), _srt_sec(times[1])
        if not any(cue_s < ie and lo < cue_e for lo, ie in ivals):
            out.append(block)
            continue
        body = lines[ti + 1:]
        new_body = []
        for ln in body:
            def _sub(m, _ln=ln):
                nonlocal words
                o = outcome_for(cue_s, cue_e)
                words += 1
                if o is None or o["outcome"] == "mute":
                    return ""
                return _match_case(m.group(0), o["replacement_word"])

            new_ln = ln
            for rx in matchers.values():
                if rx is not None:
                    new_ln = rx.sub(_sub, new_ln)
            new_ln = re.sub(r"[ \t]{2,}", " ", new_ln).strip()
            new_body.append(new_ln)
        if new_body != body:
            cues_hit += 1
        out.append("\n".join(lines[:ti + 1] + new_body))
    return "\n\n".join(out) + "\n", cues_hit, words
