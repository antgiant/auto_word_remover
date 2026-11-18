#!/usr/bin/env python
"""
flag_language.py - scan transcripts for profanity and irreverent use of God's name.

Reads any of the timestamped transcript formats this project emits (plus a few
common external ones) and reports every hit with its timestamp:

    json          WhisperX full result   (segments[].words[] -> word-accurate time)
    words.json    flat word list          (word-accurate time)
    srt / vtt     subtitle / caption cues
    lrc           timestamped lyric lines
    tsv           start<TAB>end<TAB>text   (WhisperX writes milliseconds)
    speakers.txt  "[HH:MM:SS] SPEAKER: text"
    txt           plain lines (no timestamps -> line numbers)

Word lists live in wordlists/ next to this file and are plain text you can edit:
    wordlists/profanity.txt     coarse language
    wordlists/irreverence.txt   "God's name in vain"
Each non-blank line that does not start with '#' is either a literal word/phrase
(matched case-insensitively, whole-word) or 're:<regex>' for a raw pattern.

Usage:
    python flag_language.py "meeting.json"
    python flag_language.py "C:\\Transcripts" --recurse
    python flag_language.py "a.srt" "b.lrc" --context 60 --report flags.txt
    python flag_language.py "call.json" --quiet --fail-on none

When scanning a `.json` / `.words.json` and a sibling `<same-name>.srt` exists,
it is automatically cross-checked for words the aligned transcript missed
entirely (dropped or mis-heard) - see `backfill_from_srt()`. Disable with
--no-srt-backfill.

For each input a "<transcript-filename>.flags.json" report is written next to it
(e.g. meeting.srt -> meeting.srt.flags.json); disable with --no-write.
Exit codes: 0 = clean, 1 = hits found (unless --fail-on none),
2 = usage / parse error.
"""
from __future__ import annotations

import argparse
import bisect
import difflib
import json
import re
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORDLIST_DIR = HERE / "wordlists"
CATEGORIES = ("profanity", "irreverence", "profanity_strict", "irreverence_strict")
TRANSCRIPT_EXTS = {".json", ".srt", ".vtt", ".lrc", ".tsv", ".txt"}

# preference order when a folder holds several formats for the same recording
_FORMAT_PREF = (".json", ".srt", ".vtt", ".lrc", ".tsv", ".speakers.txt",
                ".words.json", ".txt")


# ---------------------------------------------------------------------------
# word lists  ->  one compiled regex per category
# ---------------------------------------------------------------------------
def _compile(parts: list[str], label: str) -> re.Pattern | None:
    if not parts:
        return None
    try:
        return re.compile("|".join(parts), re.IGNORECASE)
    except re.error:
        good: list[str] = []
        for p in parts:
            try:
                re.compile(p)
                good.append(p)
            except re.error as exc:
                print(f"[warn] {label}: skipping bad pattern {p!r} ({exc})", file=sys.stderr)
        return re.compile("|".join(good), re.IGNORECASE) if good else None


def compile_wordlist(path: Path) -> re.Pattern | None:
    if not path.is_file():
        print(f"[warn] word list not found: {path}", file=sys.stderr)
        return None
    parts: list[str] = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if s[:3].lower() == "re:":
            parts.append(f"(?:{s[3:].strip()})")
        else:
            body = r"\s+".join(re.escape(tok) for tok in s.split())
            parts.append(rf"(?<!\w){body}(?!\w)")
    return _compile(parts, path.name)


def load_matchers(wordlist_dir: str | Path = WORDLIST_DIR,
                  only: set[str] | None = None) -> dict[str, re.Pattern | None]:
    wl = Path(wordlist_dir)
    return {
        cat: compile_wordlist(wl / f"{cat}.txt")
        for cat in CATEGORIES
        if not only or cat in only
    }


# ---------------------------------------------------------------------------
# transcript units
# ---------------------------------------------------------------------------
class Unit:
    __slots__ = ("text", "start", "end", "locator", "speaker", "word_spans")

    def __init__(self, text, start, end, locator, speaker=None, word_spans=None):
        # word_spans is a list of (char_start, char_end, word_dict); when it is
        # present `text` is already the exact concatenation those offsets index.
        self.text = text if word_spans is not None else " ".join((text or "").split())
        self.start = start
        self.end = end
        self.locator = locator
        self.speaker = speaker
        self.word_spans = word_spans


def _word_unit(words: list[dict], locator: str, speaker: str | None = None) -> Unit | None:
    buf: list[str] = []
    spans: list[tuple[int, int, dict]] = []
    pos = 0
    for w in words:
        tok = (w.get("word") or "").strip()
        if not tok:
            continue
        if buf:
            buf.append(" ")
            pos += 1
        buf.append(tok)
        spans.append((pos, pos + len(tok), w))
        pos += len(tok)
    if not spans:
        return None
    start = next((w["start"] for _, _, w in spans if w.get("start") is not None), None)
    end = next((w["end"] for _, _, w in reversed(spans) if w.get("end") is not None), None)
    return Unit("".join(buf), start, end, locator, speaker, word_spans=spans)


def _refine_time(unit: Unit, cs: int, ce: int) -> tuple[float | None, float | None]:
    """Narrow a match's timestamp to the word(s) it actually covers."""
    if not unit.word_spans:
        return unit.start, unit.end
    start = end = None
    for ws, we, w in unit.word_spans:
        if we <= cs:
            continue
        if ws >= ce:
            break
        if start is None and w.get("start") is not None:
            start = w["start"]
        if w.get("end") is not None:
            end = w["end"]
    return (start if start is not None else unit.start,
            end if end is not None else unit.end)


def _context(text: str, ms: int, me: int, width: int) -> str:
    left, right = text[:ms], text[me:]
    pre = ("..." if len(left) > width else "") + left[-width:].lstrip()
    post = right[:width].rstrip() + ("..." if len(right) > width else "")
    return f"{pre} >>{text[ms:me]}<< {post}".strip()


# ---------------------------------------------------------------------------
# parsers  ->  list[Unit]
# ---------------------------------------------------------------------------
_TIME_RE = re.compile(r"(\d{1,3}):([0-5]?\d):([0-5]?\d)[.,](\d{1,3})")
_HMS_ONLY = re.compile(r"^\s*\[(\d{2}):(\d{2}):(\d{2})\]\s*")
_SPEAKER_TXT = re.compile(r"^\[(\d{2}):(\d{2}):(\d{2})\]\s*(\S.*?):\s*(.*)$")
_TXT_SPK_PREFIX = re.compile(r"^\[?(SPEAKER_\d+|SPEAKER)\]?:\s*(.*)$", re.IGNORECASE)
_LRC_TAG = re.compile(r"\[(\d{1,3}):([0-5]?\d)(?:[.:](\d{1,3}))?\]")


def _hms_to_sec(h: str, m: str, s: str, frac: str = "0") -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(frac.ljust(3, "0")) / 1000.0


def parse_json(path: Path) -> list[Unit]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(data, dict) and data.get("segments") is not None:
        units: list[Unit] = []
        for i, seg in enumerate(data["segments"]):
            loc = f"segment {i}"
            words = [w for w in (seg.get("words") or []) if (w.get("word") or "").strip()]
            if words:
                u = _word_unit(words, loc, seg.get("speaker"))
                if u:
                    units.append(u)
                    continue
            txt = (seg.get("text") or "").strip()
            if txt:
                units.append(Unit(txt, seg.get("start"), seg.get("end"), loc, seg.get("speaker")))
        return units
    if isinstance(data, dict) and data.get("words") is not None:
        return _units_from_flat_words(data["words"])
    raise ValueError("unrecognised JSON transcript (no 'segments' or 'words')")


def parse_words_json(path: Path) -> list[Unit]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    return _units_from_flat_words(data.get("words") or [])


def _units_from_flat_words(words: list[dict]) -> list[Unit]:
    """Group a flat word stream into lines, breaking on pauses > 2s."""
    units: list[Unit] = []
    buf: list[dict] = []
    first_idx = 0
    last_end: float | None = None
    for i, w in enumerate(words):
        st = w.get("start")
        if buf and last_end is not None and st is not None and st - last_end > 2.0:
            u = _word_unit(buf, f"words {first_idx}-{i - 1}")
            if u:
                units.append(u)
            buf, first_idx = [], i
        buf.append(w)
        last_end = w.get("end") or w.get("start") or last_end
    if buf:
        u = _word_unit(buf, f"words {first_idx}-{len(words) - 1}")
        if u:
            units.append(u)
    return units


def parse_srt(path: Path, kind: str = "srt") -> list[Unit]:
    raw = path.read_text(encoding="utf-8-sig")
    units: list[Unit] = []
    for block in re.split(r"\n\s*\n", raw.strip()):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        ts_idx = next((j for j, ln in enumerate(lines) if "-->" in ln), None)
        if ts_idx is None:
            continue
        times = _TIME_RE.findall(lines[ts_idx])
        start = _hms_to_sec(*times[0]) if times else None
        end = _hms_to_sec(*times[1]) if len(times) > 1 else None
        text = re.sub(r"<[^>]+>", "", " ".join(lines[ts_idx + 1:]))
        if text.strip():
            units.append(Unit(text, start, end, f"{kind} cue @ {to_hms(start)}"))
    return units


def parse_lrc(path: Path) -> list[Unit]:
    entries: list[tuple[float, str]] = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        tags = list(_LRC_TAG.finditer(raw))
        if not tags:
            continue
        text = raw[tags[-1].end():].strip()
        for tg in tags:
            mm, ss, frac = tg.group(1), tg.group(2), tg.group(3) or "0"
            entries.append((int(mm) * 60 + int(ss) + int(frac.ljust(3, "0")) / 1000.0, text))
    entries.sort(key=lambda e: e[0])
    units: list[Unit] = []
    for i, (start, text) in enumerate(entries):
        end = entries[i + 1][0] if i + 1 < len(entries) else None
        if text:
            units.append(Unit(text, start, end, f"lrc line @ {to_hms(start)}"))
    return units


def parse_tsv(path: Path) -> list[Unit]:
    units: list[Unit] = []
    for n, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        cols = raw.split("\t")
        if len(cols) < 3:
            continue
        a, b, text = cols[0].strip(), cols[1].strip(), "\t".join(cols[2:]).strip()
        try:
            start = float(a) if "." in a else int(a) / 1000.0
            end = float(b) if "." in b else int(b) / 1000.0
        except ValueError:
            continue  # header row
        if text:
            units.append(Unit(text, start, end, f"tsv row {n}"))
    return units


def parse_speakers_txt(path: Path) -> list[Unit]:
    units: list[Unit] = []
    cur: Unit | None = None
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        m = _SPEAKER_TXT.match(raw)
        if m:
            if cur:
                units.append(cur)
            h, mm, s = m.group(1), m.group(2), m.group(3)
            cur = Unit(m.group(5), _hms_to_sec(h, mm, s), None,
                       f"turn @ {h}:{mm}:{s}", m.group(4))
        elif cur and raw.strip():
            cur = Unit(cur.text + " " + raw.strip(), cur.start, cur.end, cur.locator, cur.speaker)
    if cur:
        units.append(cur)
    return units


def parse_txt(path: Path) -> list[Unit]:
    units: list[Unit] = []
    for n, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        s = raw.strip()
        if not s:
            continue
        start = None
        pfx = _HMS_ONLY.match(s)
        if pfx:
            start = _hms_to_sec(pfx.group(1), pfx.group(2), pfx.group(3))
            s = s[pfx.end():]
        spk = _TXT_SPK_PREFIX.match(s)
        speaker, text = (spk.group(1), spk.group(2)) if spk else (None, s)
        if text.strip():
            units.append(Unit(text, start, None, f"line {n}", speaker))
    return units


_DISPATCH = {
    ".json": ("json", parse_json),
    ".srt": ("srt", parse_srt),
    ".vtt": ("vtt", lambda p: parse_srt(p, "vtt")),
    ".lrc": ("lrc", parse_lrc),
    ".tsv": ("tsv", parse_tsv),
    ".txt": ("txt", parse_txt),
}


def parse_transcript(path: Path) -> tuple[str, list[Unit]]:
    name = path.name.lower()
    if name.endswith(".words.json"):
        return "words.json", parse_words_json(path)
    if name.endswith(".speakers.txt"):
        return "speakers.txt", parse_speakers_txt(path)
    fmt, fn = _DISPATCH.get(path.suffix.lower(), ("text?", parse_txt))
    return fmt, fn(path)


# ---------------------------------------------------------------------------
# scanning
# ---------------------------------------------------------------------------
def _scan_unit(unit: Unit, matchers: dict[str, re.Pattern | None], ctx: int) -> list[dict]:
    found: dict[tuple[int, int], dict] = {}
    for cat, rx in matchers.items():
        if rx is None:
            continue
        for m in rx.finditer(unit.text):
            rec = found.setdefault((m.start(), m.end()),
                                   {"match": m.group(0), "cats": set()})
            rec["cats"].add(cat)
    hits: list[dict] = []
    for (cs, ce), rec in sorted(found.items()):
        st, en = _refine_time(unit, cs, ce)
        hits.append({
            "time": round(st, 3) if st is not None else None,
            "time_hms": to_hms(st),
            "end": round(en, 3) if en is not None else None,
            "categories": sorted(rec["cats"]),
            "match": rec["match"],
            "speaker": unit.speaker,
            "locator": unit.locator,
            "context": _context(unit.text, cs, ce, ctx),
        })
    return hits


_HALLUCINATION_MIN_GAP_S = 15.0  # minimum time between two occurrences of the same line before
#   treating the later one as a suspected repetition-loop hallucination rather than real
#   dialogue - a quick stutter or call-and-response repeat happens within a few seconds; every
#   confirmed hallucination found so far (Elemental (2023), 2026-11-17 - WhisperX repeating an
#   earlier real line of dialogue verbatim 7 times during a musical/emotional climax scene with
#   no actual speech) recurred 28-270s after the real line. Comfortably below that smallest real
#   gap while still well clear of any quick genuine repeat.
_HALLUCINATION_MIN_CHARS = 20   # ignore short/common lines ("No!", "I love you.") that
#   legitimately recur throughout any film on their own - only a longer, more specific line
#   repeating verbatim is a meaningful signal.
_HALLUCINATION_LOGPROB_GAP = 0.35   # how much worse than the file's own median avg_logprob a
#   repeated segment must score before it's treated as a hallucination rather than real
#   repeated dialogue - repetition ALONE is not a safe signal on its own: validated live
#   2026-11-17 on Elemental (2023), where plain repeated-text detection (no confidence check)
#   flagged 25 segments, but 17 of those were GENUINE repeats with ordinary confidence
#   (median -0.165) - a song's chorus reprised later in the film, a story narrated twice, a
#   generic question ("What are you doing here?") asked in two different scenes. Only the 8
#   segments that were BOTH a repeat AND scored markedly worse (-0.52 to -1.20, i.e. at least
#   this far below the file's median) were confirmed actual hallucinations by listening to
#   context. 0.35 was picked to sit cleanly between the worst genuine repeat found (-0.29) and
#   the best confirmed hallucination (-0.52) on that file - see AGENTS.md for the full
#   before/after segment dump this was tuned against.
_HALLUCINATION_EPISODE_MAX_GAP_S = 120.0  # max elapsed time between two ADJACENT segments (by
#   list position) for the low-confidence-run extension below to treat them as part of the
#   same hallucination episode - keeps a real scene change/pause from bridging two unrelated
#   low-confidence moments into one. The confirmed 12-segment episode (Elemental (2023)
#   {edition-Wooley Cut}, 2026-11-17) had gaps up to 87s BETWEEN its own scattered hallucinated
#   lines (each one a short fabricated utterance dropped into an otherwise silent/musical
#   stretch, not literally back-to-back) - an earlier 20s cap missed most of that same episode
#   entirely. 120s gives real margin above the worst gap actually seen while still well under
#   a genuine scene-to-scene jump.


def _normalize_for_repeat_check(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", "", text.lower())).strip()


def detect_repeated_segments(path: str | Path) -> dict[str, dict]:
    """Flags WhisperX repetition-loop hallucinations: segments whose text is a near-verbatim
    repeat of an EARLIER segment's text, well separated in time, AND scored notably less
    confidently than the file's own typical segment - a known Whisper-family failure mode
    where, during a stretch of audio with no real matching speech (silence, music, sound
    effects), the decoder gets stuck replaying a line from its own prior context instead of
    transcribing what's actually there. condition_on_previous_text=False and a
    repetition_penalty are already set in voice_to_text/transcribe.py specifically to reduce
    this, but neither eliminates it outright - confirmed live 2026-11-17 on Elemental (2023): a
    real, contextually-coherent line ("Holy shit, stop being such a dick.") legitimately spoken
    once during an argument scene at 4284s was then repeated near-verbatim 7 more times between
    5761s-6052s during the film's musical/emotional climax, muting ambient score/dialogue that
    was never profanity at all. 8 of that file's 29 total wordlist hits (28%) were this
    artifact.

    Repeated text ALONE is not a safe signal - see _HALLUCINATION_LOGPROB_GAP's docstring for
    why a confidence check is REQUIRED, not just recorded for visibility. Confirmed the forced-
    aligner's per-word "score" is NOT a usable substitute for that confidence check (statistically
    indistinguishable between hallucinated and real words - see transcribe.py's own 2026-10-01
    finding) - this uses the ASR decoder's own avg_logprob instead, captured separately for
    exactly this reason (see transcribe.py's CONFIDENCE_KEYS).

    Only meaningful for a `.json` transcript with WhisperX-style `segments` carrying
    avg_logprob (this is the only format with both full segment text AND that field) - returns
    {} for anything else, including a `.words.json`/flat-word file (no segment-level text to
    compare) or a file with no avg_logprob recorded at all (an older transcript, or one from a
    pipeline run that dropped it - see transcribe.py's own pre_align_meta fallback warning).

    Returns {locator: {reason, matched_locator, gap_s, avg_logprob, median_avg_logprob}} for
    every segment identified as a hallucination - the locator format ("segment N") matches what
    parse_json()/scan_file() already uses, so a caller can filter scan_file()'s own hits by
    `hit["locator"] in detect_repeated_segments(path)` with no further plumbing needed. The
    FIRST occurrence of a repeated line is never flagged - only the later one(s), since that's
    the real line and the later ones are the hallucinated copies."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    segs = data.get("segments") if isinstance(data, dict) else None
    if not segs:
        return {}

    logprobs = [s.get("avg_logprob") for s in segs if s.get("avg_logprob") is not None]
    if not logprobs:
        return {}  # no confidence data at all - repeated text alone isn't trustworthy enough
    median_lp = statistics.median(logprobs)
    threshold_lp = median_lp - _HALLUCINATION_LOGPROB_GAP

    normalized = [_normalize_for_repeat_check(s.get("text") or "") for s in segs]
    first_seen: dict[str, int] = {}
    suspects: dict[str, dict] = {}
    for i, norm in enumerate(normalized):
        if len(norm) < _HALLUCINATION_MIN_CHARS:
            continue
        start_i = segs[i].get("start")
        if start_i is None:
            continue
        j = first_seen.get(norm)
        if j is None:
            first_seen[norm] = i
            continue
        start_j = segs[j].get("start")
        if start_j is None or start_i - start_j < _HALLUCINATION_MIN_GAP_S:
            continue  # too close together to be a repetition-loop rather than real repeated dialogue
        lp_i = segs[i].get("avg_logprob")
        if lp_i is None or lp_i > threshold_lp:
            continue  # repeated, but ordinary confidence - a genuine repeat (reprised song
            #   lyrics, a story told twice, a generic line reused in a different scene), not a
            #   hallucination - see _HALLUCINATION_LOGPROB_GAP
        suspects[f"segment {i}"] = {
            "reason": "repeated-text",
            "matched_locator": f"segment {j}",
            "gap_s": round(start_i - start_j, 1),
            "avg_logprob": segs[i].get("avg_logprob"),
            "median_avg_logprob": median_lp,
        }

    # A hallucination EPISODE often isn't just one repeated line - once the decoder is stuck,
    # it can keep producing a whole cascade of VARIED fabricated lines, not just verbatim
    # repeats of one phrase. Confirmed live 2026-11-17 on Elemental (2023) {edition-Wooley
    # Cut}: a 12-segment run (5685s-6073s+) was uniformly far below the confidence threshold,
    # but exact-repeat matching above only caught 2 of those 12 - the other 10 ("I love you,
    # you stupid bastard.", "I'm not a bitch, stop being such a dick.", "Get your ass over
    # here.", ...) were each the FIRST time their specific wording appeared in this file,
    # despite scoring just as badly as their already-anchored neighbors. Extend every anchor
    # found above through any IMMEDIATELY ADJACENT segment (by list position, not elapsed
    # time) that's ALSO below the confidence threshold, capped at
    # _HALLUCINATION_EPISODE_MAX_GAP_S between consecutive segments so a real scene change or
    # pause can't bridge two unrelated low-confidence moments into one episode - confirmed this
    # correctly stops at the boundary of a legitimate reprised song in the same file (whose
    # segments scored -0.37 to -0.42, worse than the file's typical dialogue but still above
    # this threshold).
    #
    # NOTE: a segment that a LATER one's repeated-text match points back to as the "original"
    # (every suspect's own matched_locator) is NOT assumed real and is NOT exempted from this
    # extension - that assumption was tried and directly disproved 2026-11-18: "Holy shit, stop
    # being such a dick." was treated as confirmed-real in the main (non-Wooley-Cut) file
    # (segment 991, matched_locator for 1226/1229/1230) specifically BECAUSE it repeated later,
    # on the theory that repetition is evidence something was real enough for the decoder to
    # latch onto - manual listening confirmed this exact line is ALSO fake there. Proof it's a
    # pure decoder artifact, not scene-dependent: segments 0 ("What the hell is wrong with
    # you?") and 402/393 ("What the hell is that?") score within 0.002 of each other between
    # the main file and Wooley Cut (-0.840/-0.840 and -1.066/-1.068) at nearly the same relative
    # position in both cuts - the model hallucinates this same cluster of lines the same way
    # regardless of which edit it's fed, with no real occurrence in either one. So a
    # matched_locator segment is evaluated purely on its own confidence/adjacency like anything
    # else below - if something qualifies it for this extension, it gets flagged too.
    frontier = set(suspects)
    while frontier:
        new_frontier = set()
        for loc in frontier:
            i = int(loc.split()[1])
            t_i = segs[i].get("start")
            for ni in (i - 1, i + 1):
                if not (0 <= ni < len(segs)):
                    continue
                nloc = f"segment {ni}"
                if nloc in suspects:
                    continue
                lp_n = segs[ni].get("avg_logprob")
                t_n = segs[ni].get("start")
                if lp_n is None or lp_n > threshold_lp or t_i is None or t_n is None \
                        or abs(t_n - t_i) > _HALLUCINATION_EPISODE_MAX_GAP_S:
                    continue
                suspects[nloc] = {
                    "reason": "contiguous-low-confidence-run",
                    "matched_locator": None,
                    "gap_s": None,
                    "avg_logprob": lp_n,
                    "median_avg_logprob": median_lp,
                }
                new_frontier.add(nloc)
        frontier = new_frontier
    return suspects


_KNOWN_HALLUCINATION_PHRASES = (
    # Confirmed recurring VERBATIM-OR-NEAR-VERBATIM across wildly unrelated films - a dead
    # giveaway of a stable decoder bias (something in this specific WhisperX model's training
    # data it reaches for during ambiguous/low-signal audio), not real dialogue from any one
    # film. Matched as case-insensitive SUBSTRINGS of the normalized segment text, not exact
    # lines, since the model varies the exact wording around a stable "favorite" core phrase.
    #
    # Deliberately narrower than first attempted: "what the hell is wrong with you" and "what
    # the hell is that" were ALSO tried and had to be dropped - a full-library sweep
    # (2026-11-18) found them matching ordinary, plausibly-real dialogue in films like The
    # Truth About Charlie (2002), City of Ember (2008), and Babe: Pig in the City (1998) - both
    # are just common enough reactive lines that real characters legitimately say them too
    # often to trust alone, even confidence-gated. The phrases kept below are different in
    # kind: compound, unusual insult pairings distinctive enough that their cross-film spread
    # is implausible as real dialogue - confirmed appearing (all confidence-gated, i.e. each
    # one ALSO scored notably below its own file's median) in The Wizard of Oz (1939), The
    # General (1926, a Buster Keaton SILENT film - no spoken dialogue track exists for it to
    # transcribe at all), Animal Crackers (1930), Horse Feathers (1932), The Jungle Book
    # (1942), Ben-Hur (1959), Charlotte's Web (1973), Chitty Chitty Bang Bang (1968), Big Hero
    # 6 (2014), multiple Christian documentaries (Revelation, God of Wonders, Noah, Samson),
    # and Elemental (2023)/Aladdin (1992) {edition-No Jafar Cut}, where this was first
    # confirmed - no genre, era, or content rating in common, which real dialogue would need.
    "stupid bastard",
    "stop being such a dick",
    "holy shit, stop being",
    "get your ass over here",
    "not a bitch",
)


def detect_known_hallucination_phrases(path: str | Path) -> dict[str, dict]:
    """Flags a segment as a suspected hallucination when its text contains one of
    _KNOWN_HALLUCINATION_PHRASES AND it scores notably below this file's own median
    avg_logprob (the same _HALLUCINATION_LOGPROB_GAP threshold detect_repeated_segments uses) -
    independent of repetition entirely, unlike every other check in this module. This is the
    ONLY check here that needs no subtitle/CC and no earlier repeat within the SAME file to
    work from, which makes it the one signal that still helps a file with no safety net at all
    (no usable CC on the file itself or any sibling edition - see Aladdin (1992) {edition-No
    Jafar Cut}, which has neither, confirmed live 2026-11-18: 8 of its hits that the
    repetition-based detector left untouched - lone, non-repeated lines like "Holy shit, stop
    being such a dick.", "...you stupid bastard?", "Get your ass over here right now." - are
    now caught by phrase match alone).

    Deliberately requires the confidence gate on top of the phrase match: a real film COULD
    legitimately contain one of these common insults in a real, well-transcribed scene, and
    the phrase match alone isn't enough to rule that out - only phrase match AND notably-worse-
    than-this-file's-own-typical confidence together should be trusted. If this list ever
    produces a confirmed false positive, that's real evidence a specific phrase here is more
    common in genuine dialogue than assumed and should be removed, not tightened further - it
    is NOT a substitute for the confidence gate.

    Returns {locator: {reason, matched_locator, gap_s, avg_logprob, median_avg_logprob}} in the
    same shape every other check in this module returns, for the same reason: so scan_file()'s
    own hits can filter by `hit["locator"] in detect_known_hallucination_phrases(path)` with no
    extra plumbing."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    segs = data.get("segments") if isinstance(data, dict) else None
    if not segs:
        return {}
    logprobs = [s.get("avg_logprob") for s in segs if s.get("avg_logprob") is not None]
    if not logprobs:
        return {}
    median_lp = statistics.median(logprobs)
    threshold_lp = median_lp - _HALLUCINATION_LOGPROB_GAP
    suspects: dict[str, dict] = {}
    for i, s in enumerate(segs):
        lp = s.get("avg_logprob")
        if lp is None or lp > threshold_lp:
            continue
        text_norm = (s.get("text") or "").lower()
        if any(phrase in text_norm for phrase in _KNOWN_HALLUCINATION_PHRASES):
            suspects[f"segment {i}"] = {
                "reason": "known-hallucination-phrase",
                "matched_locator": None,
                "gap_s": None,
                "avg_logprob": lp,
                "median_avg_logprob": median_lp,
            }
    return suspects


_HALLUCINATION_RESCUE_WINDOW_S = 8.0  # how far from a suspected-hallucination hit's own
#   timestamp to look for a confirming subtitle/CC cue - generous enough to absorb the
#   transcript's own small timing slop (and the coarser cue-level timing a sidecar/OpenSubtitles
#   source carries after resync) without being so wide it risks matching an unrelated,
#   coincidental use of the same word elsewhere in a busy scene.


def rescue_via_subtitle(hallucinated_hits: list[dict], sub_units: list[Unit]) -> list[dict]:
    """Un-flags a suspected-hallucination hit when an INDEPENDENTLY-authored subtitle/CC cue
    near its timestamp actually contains the matched word. A hallucination filter trades real
    risk in both directions - the whole point is cutting false positives (muting audio that was
    never profanity), but being too aggressive about it reintroduces false negatives (missing
    real profanity) instead, which is just as real a failure for this tool. The embedded
    subtitle/CC track (or a sidecar/OpenSubtitles one, already resynced to this recording's own
    timeline by the time a caller has one to pass in - see resync_external_srt()) was never
    generated by the same ASR decoder that's doing the hallucinating, so if IT independently
    confirms the word was said near that moment, the word really was there regardless of how
    suspicious the transcript's own repetition/confidence signals looked on their own -
    overriding the filter is the more conservative choice in that specific case.

    `sub_units` is whatever parse_srt() (or any other Unit-producing parser) already returned -
    this function doesn't parse anything itself, so a caller that already has units in hand
    (e.g. clean.py's find_spans(), which already extracts/parses a subtitle source for
    backfill_from_srt()) doesn't pay to re-read the file.

    Returns the hits that are STILL considered hallucinated after this check (i.e. the ones
    NOT found in `sub_units` are passed through unchanged; a rescued hit is removed from the
    returned list and the caller's own `hits` is expected to already have it available by
    identity if it wants to report what got rescued - this function itself only filters)."""
    still_bad = []
    for h in hallucinated_hits:
        t = h["time"]
        if t is None:
            still_bad.append(h)
            continue
        word = _norm_tok(h["match"].split()[-1])
        confirmed = any(
            u.start is not None and u.start - _HALLUCINATION_RESCUE_WINDOW_S <= t
            <= (u.end if u.end is not None else u.start) + _HALLUCINATION_RESCUE_WINDOW_S
            and any(_norm_tok(tok) == word for tok in u.text.split())
            for u in sub_units
        )
        if confirmed:
            h["suspected_hallucination"]["rescued_by_subtitle"] = True
        else:
            still_bad.append(h)
    return still_bad


_CC_SFX_CUE_RE = re.compile(r"^(\([^)]*\)|\[[^\]]*\])$")   # SDH sound-effect cues use either
#   bracket style depending on the source - "(WAVES LAPPING)" (Elemental (2023)'s embedded
#   SRT) vs. "[LIGHT SWITCH CLICKS]" (Home Alone (1990)'s) - confirmed 2026-11-18 the square-
#   bracket style is just as real an SDH track (3.5% of Home Alone's 1752 cues, well above
#   _CC_MIN_SFX_FRACTION) and was being wrongly classified as dialogue-only by only matching
#   parens, which would have silently refused a perfectly good absence-of-coverage signal.
_CC_MIN_CUES = 50   # minimum total cues before trusting a "full CC" classification below - a
#   tiny forced/partial subtitle track could spuriously look SFX-heavy by fraction alone from
#   just a handful of cues.
_CC_MIN_SFX_FRACTION = 0.02   # share of cues that are a PURE bracketed sound-effect
#   description ("(WAVES LAPPING)", "(HORN BLOWS)") before treating a subtitle source as a
#   full SDH-style closed-caption track - one that transcribes non-dialogue audio too, not
#   just speech - rather than a dialogue-only one. Measured 2026-11-18 on Elemental (2023): the
#   main cut's embedded SRT is 24.5% pure-SFX cues (482/1965) - a real SDH track; the Wooley
#   Cut edition's OpenSubtitles-sourced SRT is 0% (0/1159) - dialogue only, as expected from a
#   fan-resynced third-party source that was never trying to describe non-speech audio. 2%
#   leaves wide margin below the real case while still requiring more than an occasional
#   one-off bracketed aside ("(laughs)") in an otherwise dialogue-only track.
_HALLUCINATION_NO_CC_WINDOW_S = 12.0   # how far from a hit's own timestamp to look for ANY cue
#   (dialogue or sound-effect) before treating total silence in a full CC track as independent
#   confirmation nothing was said there at all. Wider than _HALLUCINATION_RESCUE_WINDOW_S
#   because this makes the OPPOSITE claim (absence proves nothing was said, not presence
#   confirms something was) - it should tolerate more CC timing slop before concluding
#   "nothing's there" rather than risk wrongly excluding real dialogue over a sync offset.


def find_shared_prefix_cutoff(segs_a: list[dict], segs_b: list[dict],
                              tol: float = 0.1) -> float | None:
    """Returns the timestamp through which two WhisperX segment lists - typically different
    editions/cuts of the same title - are text-AND-timing identical, or None if they diverge
    on their very first segment. Confirmed 2026-11-18: Elemental (2023) and its Wooley Cut
    edition share an exact (same text, start time within 0.05s) prefix through segment 118 /
    t=633.3s before the fan edit's first actual change (segment 119: "I'm doing a reading."
    vs. "Sorry, gotta grab some stuff.") - meaning the opening ~10.5 minutes, including the
    entire Disney+Pixar logo sequence, is bit-identical audio in both cuts, despite the fan
    edit's own subtitle track being too sparse/unreliable to help on its own.

    A confirmed-identical prefix like this means evidence gathered against ONE cut in that
    range (e.g. a full SDH CC track that only exists on the other cut - see
    exclude_via_cc_absence) is just as valid for the other cut's own hits in the same range,
    without needing full cross-edit realignment past the point where they actually diverge."""
    n = min(len(segs_a), len(segs_b))
    cutoff = None
    for i in range(n):
        a, b = segs_a[i], segs_b[i]
        if a.get("text") != b.get("text") or abs((a.get("start") or 0) - (b.get("start") or 0)) > tol:
            break
        cutoff = a.get("start")
    return cutoff


def find_local_alignment_offset(segs_a: list[dict], segs_b: list[dict], target_b_idx: int,
                                max_block_distance: int = 20, min_blocks: int = 2,
                                max_offset_stddev: float = 3.0) -> float | None:
    """Returns a reliable local time offset (segs_a's time - segs_b's time) for mapping a
    timestamp in `segs_b` (e.g. a hallucination hit in an edition's own transcript) onto the
    corresponding point in `segs_a` (e.g. a sibling edition's transcript), for use past
    find_shared_prefix_cutoff's point - i.e. once the two cuts have already diverged and a
    single global offset no longer applies, but short stretches may still be genuinely
    unedited between the two cuts, just shifted by whatever's been added/removed before them.

    Derived from TEXT-matching blocks (difflib.SequenceMatcher on segment text) near
    `target_b_idx`, deliberately EXCLUDING any block that contains target_b_idx itself. That
    exclusion is the whole point, not an optimization: confirmed 2026-11-18 on Elemental (2023)
    {edition-Wooley Cut} segment 725 ("Oh my gosh", a confirmed hallucination at t=3119.4) -
    its own text happens to exactly match a GENUINELY REAL "Oh, my gosh!" line in the main cut
    at t=3135.3, an unrelated story moment the two cuts' edits have drifted apart from. Using
    that coincidental match's own offset (~16.4s) would silently validate the wrong position.
    The blocks immediately before and after it (not containing it) agree on a quite different,
    mutually consistent ~8.3s offset - which the coincidental match's own offset disagrees with
    enough that requiring low variance across surrounding blocks protects against exactly this
    trap. A WhisperX decoder can hallucinate a phrase that also occurs for real elsewhere in
    the same film, so text equality AT the hit itself is never trustworthy alignment evidence.

    Returns None - the safe, request-nothing-happen default - unless at least `min_blocks`
    qualifying blocks exist within `max_block_distance` list positions of `target_b_idx` and
    their offsets agree within `max_offset_stddev` of each other. A caller only uses the
    returned offset to look for ADDITIONAL exclusion evidence (see exclude_via_cc_absence /
    exclude_via_cc_word_absence) - never to protect or confirm a hit - so the worst case of a
    missing or wrong offset is simply failing to catch something, not a new false exclusion."""
    a_texts = [s.get("text") for s in segs_a]
    b_texts = [s.get("text") for s in segs_b]
    sm = difflib.SequenceMatcher(None, a_texts, b_texts, autojunk=False)
    offsets = []
    for blk in sm.get_matching_blocks():
        if blk.size == 0 or (blk.b <= target_b_idx < blk.b + blk.size):
            continue
        if abs((blk.b + blk.size // 2) - target_b_idx) > max_block_distance:
            continue
        a_t = segs_a[blk.a].get("start")
        b_t = segs_b[blk.b].get("start")
        if a_t is not None and b_t is not None:
            offsets.append(a_t - b_t)
    if len(offsets) < min_blocks:
        return None
    if statistics.pstdev(offsets) > max_offset_stddev:
        return None
    return statistics.mean(offsets)


def _is_full_cc(sub_units: list[Unit]) -> bool:
    """True if `sub_units` looks like a full SDH-style closed-caption track that transcribes
    non-dialogue audio (sound effects, music cues) as well as speech, not just a dialogue-only
    subtitle - see _CC_MIN_SFX_FRACTION's docstring for how this was calibrated. Only a track
    like this makes silence itself meaningful evidence (see exclude_via_cc_absence); a
    dialogue-only source says nothing about a pure-music/silent stretch even when it has no
    cue there, since it was never trying to describe non-speech audio in the first place."""
    if len(sub_units) < _CC_MIN_CUES:
        return False
    sfx = sum(1 for u in sub_units if _CC_SFX_CUE_RE.match((u.text or "").strip()))
    return sfx / len(sub_units) >= _CC_MIN_SFX_FRACTION


def exclude_via_cc_absence(hits: list[dict], sub_units: list[Unit], reason: str = "no-cc-coverage") -> None:
    """Flags a wordlist hit as a suspected hallucination - independent of, and a different
    mechanism from, detect_repeated_segments' repetition/confidence check - when a full
    SDH-style CC track (see _is_full_cc) has NO cue of any kind, dialogue or sound-effect,
    anywhere near its timestamp. This catches exactly what repetition-based detection
    structurally cannot: a hallucination that occurs only ONCE in the file with nothing earlier
    to repeat-match against. Confirmed 2026-11-18 on Elemental (2023): "What the hell is wrong
    with you?" at 0:06 falls inside the main cut's own CC track's completely silent 0:00-1:01
    opening (the Disney + Pixar logo sequence - music only, not even a sound-effect cue) yet
    was invisible to the repeated-text detector since it's the first, not a later, occurrence
    of that exact line.

    Does nothing when `sub_units` doesn't look like a full CC - silence in a dialogue-only
    subtitle proves nothing about a pure-music stretch, since such a track was never attempting
    to describe non-speech audio at all (see _is_full_cc).

    Mutates `hits` in place (sets suspected_hallucination on the ones newly caught) and returns
    None - mirrors detect_repeated_segments/scan_file's own already-in-place hit mutation
    pattern, unlike rescue_via_subtitle's filter-and-return-a-new-list shape, since this is
    ADDING markers rather than removing hits from a list. Only evaluates hits NOT already
    flagged by the caller - this is a second, independent path onto the same
    suspected_hallucination marker, not a replacement for the repetition-based one."""
    if not _is_full_cc(sub_units):
        return
    for h in hits:
        if h.get("suspected_hallucination"):
            continue
        t = h.get("time")
        if t is None:
            continue
        nearby = any(
            u.start is not None and u.start - _HALLUCINATION_NO_CC_WINDOW_S <= t
            <= (u.end if u.end is not None else u.start) + _HALLUCINATION_NO_CC_WINDOW_S
            for u in sub_units
        )
        if not nearby:
            h["suspected_hallucination"] = {
                "reason": reason,
                "matched_locator": None,
                "gap_s": None,
                "avg_logprob": None,
                "median_avg_logprob": None,
            }


def exclude_via_cc_word_absence(hits: list[dict], sub_units: list[Unit],
                                reason: str = "no-cc-word-match") -> None:
    """A stronger sibling of exclude_via_cc_absence for the case that one can't catch: a busy,
    well-covered scene (lots of CC cues nearby - a crowd cheering, music, other dialogue) where
    none of those cues contain the matched word itself. exclude_via_cc_absence requires total
    silence, so it correctly does nothing here - there IS plenty of CC activity nearby, just not
    this word - but that's exactly the situation a full SDH track is positioned to adjudicate:
    captioners transcribe profanity/exclamations verbatim (accessibility standards require
    capturing meaningful dialogue regardless of volume, unlike an automated VAD/ASR system),
    so if the scene is confirmed covered but this specific word never appears anywhere nearby,
    that's real evidence against it, not just an absence of evidence.

    Confirmed 2026-11-18 on Elemental (2023): the main cut's hallucinated "What the hell is
    that?" at 1765s sits inside a loudly-captioned celebration scene ("We love you, Lutz!",
    "(CHEERING)", "Yay! Yeah! Way to go, Lutz!", "(WHISTLE BLOWS)") - exclude_via_cc_absence
    can't touch it (the window is full of other cues) but none of those cues contain "hell"
    anywhere nearby, and the Wooley Cut edition hallucinates the exact same line at the
    aligned position in its own cut (see find_shared_prefix_cutoff's docstring and
    find_local_alignment_offset) - two independent cuts agreeing on a fabrication is itself
    further evidence it's a shared decoder artifact, not real dialogue either cut's captioners
    ever had reason to transcribe.

    Reuses the exact same word-match logic as rescue_via_subtitle (same
    _HALLUCINATION_RESCUE_WINDOW_S), just inverted and applied to hits the caller hasn't
    already flagged - a hit this WOULD rescue if it were already flagged is, by construction,
    never caught here, so the two functions can't contradict each other on the same hit.
    Gated on _is_full_cc like exclude_via_cc_absence, for the same reason: a dialogue-only
    source saying nothing about a word doesn't mean nothing was said, just that this track
    was never trying to catch everything.

    Carries real risk a plain silence check doesn't: even a thorough SDH track can occasionally
    paraphrase a quick exclamation into a generic cue ("(EXCLAIMS)") rather than transcribing it
    verbatim, which would make this wrongly treat a real line as absent. Verify the blast radius
    broadly (not just on one already-confirmed file) before trusting this at library scale -
    see the 2026-11-18 commit message for how this was checked before shipping."""
    if not _is_full_cc(sub_units):
        return
    for h in hits:
        if h.get("suspected_hallucination"):
            continue
        t = h.get("time")
        if t is None:
            continue
        word = _norm_tok(h["match"].split()[-1])
        confirmed = any(
            u.start is not None and u.start - _HALLUCINATION_RESCUE_WINDOW_S <= t
            <= (u.end if u.end is not None else u.start) + _HALLUCINATION_RESCUE_WINDOW_S
            and any(_norm_tok(tok) == word for tok in u.text.split())
            for u in sub_units
        )
        if not confirmed:
            h["suspected_hallucination"] = {
                "reason": reason,
                "matched_locator": None,
                "gap_s": None,
                "avg_logprob": None,
                "median_avg_logprob": None,
            }


def scan_file(path: str | Path, matchers: dict[str, re.Pattern | None],
              ctx: int = 48) -> tuple[list[dict], str]:
    fmt, units = parse_transcript(Path(path))
    hits: list[dict] = []
    for u in units:
        hits.extend(_scan_unit(u, matchers, ctx))
    for h in hits:
        h["source"] = fmt
    if fmt == "json":
        suspects = detect_repeated_segments(path)
        for h in hits:
            info = suspects.get(h["locator"])
            if info is not None:
                h["suspected_hallucination"] = info
        phrase_suspects = detect_known_hallucination_phrases(path)
        for h in hits:
            if h.get("suspected_hallucination"):
                continue
            info = phrase_suspects.get(h["locator"])
            if info is not None:
                h["suspected_hallucination"] = info
    hits.sort(key=lambda h: (h["time"] is None, h["time"] or 0.0, str(h["locator"])))
    return hits, fmt


# ---------------------------------------------------------------------------
# SRT backfill: catch words the word-level transcript missed entirely, using
# an embedded/sibling subtitle track for text and the transcript's own word
# timings to locate them as precisely as possible
# ---------------------------------------------------------------------------
_TOKEN_RE = re.compile(r"[A-Za-z0-9']+")


def _norm_tok(s: str) -> str:
    """Alphanumeric+apostrophe core of a token, punctuation stripped either
    side - so an ASR word like "motherfuckers," normalizes the same way an
    SRT cue's regex-extracted token does."""
    m = _TOKEN_RE.search(s)
    return m.group(0).strip("'").lower() if m else ""


def _tokenize(text: str) -> list[tuple[str, int, int]]:
    return [(_norm_tok(m.group(0)), m.start(), m.end()) for m in _TOKEN_RE.finditer(text)]


def _char_to_token(tokens: list[tuple[str, int, int]], pos: int) -> int:
    for i, (_, cs, ce) in enumerate(tokens):
        if cs <= pos < ce or pos < cs:
            return i
    return max(0, len(tokens) - 1)


def _matches_any(text: str, matchers: dict[str, re.Pattern | None]) -> bool:
    return any(rx.search(text) for rx in matchers.values() if rx is not None)


def load_word_timeline(path: str | Path) -> list[dict]:
    """Flat, time-sorted [{"word","start","end"}, ...] from a `.json`
    (WhisperX result, `segments[].words[]`) or a `.words.json`."""
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if isinstance(data, dict) and data.get("segments") is not None:
        raw = (w for seg in data["segments"] for w in (seg.get("words") or []))
    elif isinstance(data, dict) and data.get("words") is not None:
        raw = iter(data["words"])
    else:
        raise ValueError("no 'segments' or 'words' in transcript")
    words = [{"word": (w.get("word") or "").strip(), "start": w.get("start"), "end": w.get("end")}
             for w in raw]
    words = [w for w in words if w["word"]]
    words.sort(key=lambda w: (w["start"] is None, w["start"] or 0.0))
    return words


def _locate_srt_word(ti: int, opcodes: list[tuple], local: list[dict],
                     matchers: dict[str, re.Pattern | None],
                     default_dur: float) -> tuple[float | None, float | None, bool]:
    """Map SRT token index `ti` to a transcript timestamp via the difflib
    alignment `opcodes` (srt tokens -> `local` transcript words).

    Returns (start, end, is_new). is_new is False when a transcript word
    already sits at this aligned position and itself matches the word lists -
    meaning it was already found by scanning the transcript directly, so
    this SRT occurrence is not a new (missed) hit.
    """
    for tag, i1, i2, j1, j2 in opcodes:
        if not (i1 <= ti < i2):
            continue
        if tag in ("equal", "replace") and j2 > j1:
            span = max(1, i2 - i1)
            j = j1 + min(j2 - j1 - 1, int((ti - i1) / span * (j2 - j1)))
            w = local[j]
            if _matches_any(w["word"], matchers):
                return None, None, False
            return w.get("start"), w.get("end"), True
        # "delete" (or a "replace" aligned to zero transcript words): the SRT
        # word has no transcript counterpart at all - interpolate its timing
        # from the nearest transcript words on either side of the gap.
        prev_end = local[j1 - 1]["end"] if j1 > 0 else None
        next_start = local[j2]["start"] if j2 < len(local) else None
        if prev_end is not None and next_start is not None and next_start > prev_end:
            frac = (ti - i1 + 0.5) / max(1, i2 - i1)
            mid = prev_end + frac * (next_start - prev_end)
            dur = min(default_dur, next_start - prev_end)
            return max(prev_end, mid - dur / 2), min(next_start, mid + dur / 2), True
        if prev_end is not None:
            return prev_end, prev_end + default_dur, True
        if next_start is not None:
            return max(0.0, next_start - default_dur), next_start, True
        return None, None, True
    return None, None, True


def backfill_from_srt(srt_path: str | Path, matchers: dict[str, re.Pattern | None],
                      word_timeline: list[dict], ctx: int = 48,
                      window: float = 2.5, default_dur: float = 0.35) -> list[dict]:
    """Find profanity in an SRT that the word-level transcript missed.

    For every regex hit in an SRT cue, the cue's words are aligned (via
    difflib) against the transcript words spoken near that cue (+/- `window`
    seconds). A hit whose aligned position is a transcript word that already
    matches the same word lists is skipped - already found by `scan_file` on
    the transcript directly. Otherwise the aligned transcript word's timing is
    used, or - if the transcript dropped the word entirely - a timestamp
    interpolated between its neighbours; failing that, the cue's own
    start/end. Returned hits carry `"source": "srt-backfill"`.
    """
    starts = [w["start"] if w["start"] is not None else float("inf") for w in word_timeline]
    units = parse_srt(Path(srt_path))
    extra: list[dict] = []

    for u in units:
        if u.start is None:
            continue
        cue_matches = [(m.start(), m.end(), m.group(0), cat)
                       for cat, rx in matchers.items() if rx is not None
                       for m in rx.finditer(u.text)]
        if not cue_matches:
            continue

        cue_tokens = _tokenize(u.text)
        lo = u.start - window
        hi = (u.end if u.end is not None else u.start + 4.0) + window
        local = word_timeline[bisect.bisect_left(starts, lo):bisect.bisect_right(starts, hi)]
        opcodes = difflib.SequenceMatcher(
            None, [t[0] for t in cue_tokens], [_norm_tok(w["word"]) for w in local],
            autojunk=False).get_opcodes()

        by_span: dict[tuple[int, int], dict] = {}
        for cs, ce, text, cat in cue_matches:
            ti = _char_to_token(cue_tokens, cs)
            start_t, end_t, is_new = _locate_srt_word(ti, opcodes, local, matchers, default_dur)
            if not is_new:
                continue
            if start_t is None:
                start_t = u.start
                end_t = u.end if u.end is not None else u.start + default_dur
            rec = by_span.setdefault((cs, ce), {"match": text, "cats": set(),
                                                "start": start_t, "end": end_t})
            rec["cats"].add(cat)

        for (cs, ce), rec in sorted(by_span.items()):
            extra.append({
                "time": round(rec["start"], 3),
                "time_hms": to_hms(rec["start"]),
                "end": round(rec["end"], 3) if rec["end"] is not None else None,
                "categories": sorted(rec["cats"]),
                "match": rec["match"],
                "speaker": u.speaker,
                "locator": f"{u.locator} (missed by transcript)",
                "context": _context(u.text, cs, ce, ctx),
                "source": "srt-backfill",
            })
    return extra


# ---------------------------------------------------------------------------
# External subtitle resync (e.g. OpenSubtitles): the file's own timestamps
# can't be trusted at all - a TV recording routinely carries commercial
# breaks and station edit cuts a retail/streaming release doesn't, on top of
# plain drift, and the subtitle file itself often carries injected ad/
# attribution cues. resync_units_to_transcript() throws away every timestamp
# in the source subtitle and rebuilds real ones from a whole-file TEXT
# alignment against the ASR word timeline instead - order-preserving (an
# LCS-style match via difflib, same technique backfill_from_srt above uses
# per-cue, just run once over the whole file instead of in a small time
# window), so commercial insertions/station cuts just show up as unmatched
# stretches on either side rather than breaking the alignment or requiring
# any assumption about a shared clock.
# ---------------------------------------------------------------------------
_AD_LINE_RE = re.compile(
    r"opensubtitles|addic7ed|subscene|podnapisi\.net|yifysubtitles|undertexter|"
    r"www\.[a-z0-9-]+\.(?:com|org|net|tv|info)|https?://|"
    r"support us and become|advertise your product|"
    r"sync(?:hroni[sz]ation)?(?:ed)?\s*(?:and|&)?\s*correct(?:ed|ions?)?\s*by|"
    r"subtitles?\s*(?:by|downloaded from)|ripped\s*(?:and|&)?\s*(?:corrected\s*)?by|"
    r"encoded\s*by",
    re.IGNORECASE)


def is_ad_cue(text: str) -> bool:
    """True for a subtitle cue that's injected ad/attribution spam rather
    than real dialogue - common in files pulled from public subtitle sites."""
    return bool(_AD_LINE_RE.search(text))


def strip_ad_units(units: list[Unit]) -> list[Unit]:
    return [u for u in units if not is_ad_cue(u.text)]


def resync_units_to_transcript(srt_path: str | Path, word_timeline: list[dict],
                               min_anchor_run: int = 2) -> list[tuple[float, float, str]]:
    """Realign an externally-sourced subtitle file whose own timestamps can't
    be trusted (see the section banner above) against `word_timeline` (the
    ASR's own word-accurate times - see load_word_timeline). Returns
    [(start, end, text), ...] with real, trustworthy timing, sorted and
    monotonic.

    A cue with no reliable match anywhere (ad text, or a scene this
    recording simply doesn't have) is dropped rather than guessed at, except
    a single cue sandwiched directly between two confidently-matched
    neighbours, which is interpolated between them - very likely a genuine
    line the alignment just missed (or a lone ad cue splitting real
    dialogue), unlike a longer unmatched run, which reads much more like a
    whole stretch this recording's cut doesn't have (or a commercial break)
    than a string of individually-dropped words, and is left alone.

    `min_anchor_run` is how many consecutive tokens must match before a
    block of the whole-file alignment is trusted as an anchor - 1 would let
    a single common word (e.g. "the") anchor a cue on pure coincidence; 2+
    requires an actual short phrase in common, cutting that risk sharply.
    """
    units = strip_ad_units(parse_srt(Path(srt_path)))
    if not units or not word_timeline:
        return []

    srt_tokens: list[str] = []
    cue_range: list[tuple[int, int]] = []
    for u in units:
        lo = len(srt_tokens)
        srt_tokens.extend(t for t, _cs, _ce in _tokenize(u.text) if t)
        cue_range.append((lo, len(srt_tokens)))

    ref_tokens = [_norm_tok(w["word"]) for w in word_timeline]
    # A whole-file difflib pass on movie-length sequences (tens of thousands
    # of tokens) is real work but tractable for an offline batch tool that
    # already budgets minutes for transcription/stemming - autojunk=False
    # for the same reason backfill_from_srt above uses it: junk heuristics
    # are tuned for source-code diffing, not natural-language token streams.
    sm = difflib.SequenceMatcher(None, srt_tokens, ref_tokens, autojunk=False)

    tok_map: dict[int, int] = {}
    for blk in sm.get_matching_blocks():
        if blk.size >= min_anchor_run:
            for k in range(blk.size):
                tok_map[blk.a + k] = blk.b + k

    placed: list[tuple[float, float] | None] = []
    for lo, hi in cue_range:
        idxs = [tok_map[i] for i in range(lo, hi) if i in tok_map]
        if not idxs:
            placed.append(None)
            continue
        j0, j1 = min(idxs), max(idxs)
        st = word_timeline[j0].get("start")
        en = word_timeline[j1].get("end") or word_timeline[j1].get("start")
        placed.append((st, en) if st is not None and en is not None else None)

    n = len(units)
    for i in range(n):
        if placed[i] is not None:
            continue
        p, q = i - 1, i + 1
        if p < 0 or q >= n or placed[p] is None or placed[q] is None:
            continue
        po, qo = units[p].start, units[q].start
        if po is None or qo is None or qo <= po or units[i].start is None:
            continue
        frac = min(1.0, max(0.0, (units[i].start - po) / (qo - po)))
        prev_new_end, next_new_start = placed[p][1], placed[q][0]
        if next_new_start > prev_new_end:
            st = prev_new_end + frac * (next_new_start - prev_new_end)
            placed[i] = (st, min(next_new_start, st + 2.0))

    out: list[tuple[float, float, str]] = []
    last_end = -1.0
    for u, p in zip(units, placed):
        if p is None:
            continue
        st, en = p
        if en <= st:
            en = st + 0.35
        if st < last_end:      # a bad anchor put this cue out of order - drop
            continue           # it rather than let it corrupt the track/spans
        out.append((st, en, u.text))
        last_end = en
    return out


_SRT_TS_FMT = "{:02d}:{:02d}:{:02d},{:03d}"


def _srt_ts(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    ms = round((s - int(s)) * 1000)
    s = int(s)
    if ms == 1000:
        ms = 0
        s += 1
    return _SRT_TS_FMT.format(int(h), int(m), s, ms)


def write_srt_cues(cues: list[tuple[float, float, str]], path: str | Path) -> Path:
    """Serialize [(start, end, text), ...] as a real .srt file - used for the
    resynced OpenSubtitles output (see resync_units_to_transcript)."""
    path = Path(path)
    parts = [f"{i}\n{_srt_ts(st)} --> {_srt_ts(en)}\n{text}\n"
             for i, (st, en, text) in enumerate(cues, 1)]
    path.write_text("\n".join(parts), encoding="utf-8")
    return path


def count_categories(hits: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for h in hits:
        for c in h["categories"]:
            out[c] = out.get(c, 0) + 1
    return out


def write_report(path: str | Path, hits: list[dict], fmt: str = "") -> Path:
    path = Path(path)
    payload = {
        "source": path.name,
        "format": fmt,
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "counts": {**count_categories(hits), "total": len(hits)},
        "hits": hits,
    }
    # keep the full input name so scanning e.g. both foo.srt and foo.json in
    # one folder produces foo.srt.flags.json and foo.json.flags.json, not one
    # file that clobbers the other.
    out = path.with_name(path.name + ".flags.json")
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def write_combined_report(path: Path, combined: list[dict]) -> None:
    lines = [f"# language scan  {time.strftime('%Y-%m-%d %H:%M:%S')}", ""]
    for entry in combined:
        lines.append(f"{entry['source']}  ({entry['format']})  - {len(entry['hits'])} hit(s)")
        for h in entry["hits"]:
            who = f" [{h['speaker']}]" if h["speaker"] else ""
            lines.append(f"  {h['time_hms']}  {','.join(h['categories'])}{who}  {h['match']!r}")
            lines.append(f"      {h['context']}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def to_hms(t: float | None) -> str:
    if t is None:
        return "--:--:--.---"
    t = max(0.0, float(t))
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"


def _is_transcript(f: Path) -> bool:
    n = f.name.lower()
    return f.suffix.lower() in TRANSCRIPT_EXTS and not n.endswith(".flags.json")


def _recording_key(p: Path) -> str:
    n = p.name.lower()
    for ext in (".words.json", ".speakers.txt"):
        if n.endswith(ext):
            return p.name[: -len(ext)]
    return p.stem


def _dedupe_best(files: list[Path]) -> list[Path]:
    def rank(f: Path) -> int:
        n = f.name.lower()
        if n.endswith(".words.json"):
            key = ".words.json"
        elif n.endswith(".speakers.txt"):
            key = ".speakers.txt"
        else:
            key = f.suffix.lower()
        return _FORMAT_PREF.index(key) if key in _FORMAT_PREF else len(_FORMAT_PREF)

    groups: dict[str, list[Path]] = {}
    for f in files:
        groups.setdefault(_recording_key(f), []).append(f)
    return [sorted(g, key=rank)[0] for g in groups.values()]


def gather(inputs: list[str], recurse: bool) -> list[Path]:
    explicit: list[Path] = []
    discovered: list[Path] = []
    for item in inputs:
        p = Path(item)
        if p.is_dir():
            it = p.rglob("*") if recurse else p.iterdir()
            discovered.extend(f for f in it if f.is_file() and _is_transcript(f))
        elif p.is_file():
            explicit.append(p)
        else:
            print(f"[skip] not found: {item}", file=sys.stderr)
    return sorted(dict.fromkeys(explicit + _dedupe_best(discovered)))


def _sibling_srt(path: Path) -> Path | None:
    """The `<same recording>.srt` next to a `.json` / `.words.json`, if any."""
    n = path.name.lower()
    if n.endswith(".words.json"):
        base = path.name[: -len(".words.json")]
    elif n.endswith(".json"):
        base = path.name[: -len(".json")]
    else:
        return None
    cand = path.with_name(base + ".srt")
    return cand if cand.is_file() else None


def print_file_report(path: Path, hits: list[dict], fmt: str, quiet: bool) -> None:
    if not hits:
        if not quiet:
            print(f"  OK    {path.name}  ({fmt})  - no flags")
        return
    print(f"  FLAG  {path.name}  ({fmt})  - {len(hits)} hit(s)")
    if quiet:
        return
    for h in hits:
        who = f"  {h['speaker']}" if h["speaker"] else ""
        tag = "  [srt-only]" if h.get("source") == "srt-backfill" else ""
        print(f"      {h['time_hms']}  {','.join(h['categories']):<22} {h['match']!r}{who}{tag}")
        print(f"                     {h['context']}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser(
        prog="flag_language.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+", help="transcript files or folders")
    ap.add_argument("-r", "--recurse", action="store_true", help="descend into sub-folders")
    ap.add_argument("--wordlist-dir", default=str(WORDLIST_DIR))
    ap.add_argument("--only", choices=CATEGORIES, action="append",
                    help="scan only this category (repeatable)")
    ap.add_argument("--context", type=int, default=48, help="context chars each side (default 48)")
    ap.add_argument("--report", help="also write a combined plain-text report to this path")
    ap.add_argument("--no-write", action="store_true", help="do not write per-file .flags.json")
    ap.add_argument("--quiet", action="store_true", help="summary lines only")
    ap.add_argument("--no-srt-backfill", action="store_true",
                    help="don't cross-check a sibling .srt for words the transcript missed")
    ap.add_argument("--fail-on", choices=["none", "any"], default="any",
                    help="'any' (default) exits 1 when any hit is found; 'none' always exits 0")
    ap.add_argument("--emit-json", action="store_true", help="print the combined result as JSON")
    args = ap.parse_args(argv)

    matchers = load_matchers(args.wordlist_dir, set(args.only) if args.only else None)
    active = [k for k, v in matchers.items() if v]
    if not active:
        print("[error] no usable word lists", file=sys.stderr)
        return 2

    files = gather(args.inputs, args.recurse)
    if not files:
        print("no transcript files found", file=sys.stderr)
        return 2

    print(f"scanning {len(files)} file(s)  [{', '.join(active)}]")
    combined: list[dict] = []
    errors = 0
    for f in files:
        try:
            hits, fmt = scan_file(f, matchers, args.context)
            if not args.no_srt_backfill:
                srt = _sibling_srt(f)
                if srt is not None:
                    try:
                        extra = backfill_from_srt(srt, matchers, load_word_timeline(f), args.context)
                    except Exception as exc:
                        extra = []
                        print(f"  [warn] {f.name}: srt backfill against {srt.name} failed ({exc})",
                              file=sys.stderr)
                    if extra:
                        hits = hits + extra
                        hits.sort(key=lambda h: (h["time"] is None, h["time"] or 0.0, str(h["locator"])))
                        print(f"        (+{len(extra)} found only in {srt.name})")
        except Exception as exc:
            errors += 1
            print(f"  ERR   {f.name}  - {exc}", file=sys.stderr)
            continue
        print_file_report(f, hits, fmt, args.quiet)
        if not args.no_write:
            write_report(f, hits, fmt)
        combined.append({"source": str(f), "format": fmt, "hits": hits})

    all_hits = [h for e in combined for h in e["hits"]]
    by_cat = count_categories(all_hits)
    print("-" * 60)
    tail = f"  ({', '.join(f'{k}: {v}' for k, v in sorted(by_cat.items()))})" if by_cat else ""
    print(f"{len(all_hits)} flagged term(s) across {len(files)} file(s){tail}")

    if args.report:
        write_combined_report(Path(args.report), combined)
        print(f"report -> {args.report}")
    if args.emit_json:
        print(json.dumps({
            "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "counts": {**by_cat, "total": len(all_hits)},
            "files": combined,
        }, ensure_ascii=False, indent=2))

    if errors:
        return 2
    if args.fail_on == "any" and all_hits:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
