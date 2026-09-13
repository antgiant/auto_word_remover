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
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORDLIST_DIR = HERE / "wordlists"
CATEGORIES = ("profanity", "irreverence")
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


def scan_file(path: str | Path, matchers: dict[str, re.Pattern | None],
              ctx: int = 48) -> tuple[list[dict], str]:
    fmt, units = parse_transcript(Path(path))
    hits: list[dict] = []
    for u in units:
        hits.extend(_scan_unit(u, matchers, ctx))
    for h in hits:
        h["source"] = fmt
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
