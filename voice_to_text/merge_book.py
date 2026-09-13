#!/usr/bin/env python
"""Stitch per-chapter WhisperX .json files into one book-level word-level transcript.

Usage:
    python merge_book.py "path\\to\\audiobook_folder"  [--title "Book Title"]

Reads every <n> - Chapter <n>.json (natural-sorted), concatenates with cumulative
timestamps, and writes  <folder>\\<title>.json  and  <title>.words.json  next to it.
"""
from __future__ import annotations
import argparse, json, re
from pathlib import Path


def natkey(p: Path):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", p.stem)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--title")
    ap.add_argument("--glob", default="*.json")
    args = ap.parse_args()

    folder = Path(args.folder)
    title = args.title or folder.name
    jsons = sorted(
        (p for p in folder.glob(args.glob)
         if not p.name.endswith(".words.json") and p.stem != title),
        key=natkey,
    )
    if not jsons:
        print("no chapter .json files found in", folder)
        return 1

    offset = 0.0
    all_segments, all_words, chapters = [], [], []
    for idx, jp in enumerate(jsons, 1):
        d = json.loads(jp.read_text(encoding="utf-8"))
        meta = d.get("_meta", {})
        dur = float(meta.get("duration_sec") or 0.0)
        seg_end = 0.0
        for s in d.get("segments", []):
            s = dict(s)
            s["chapter"] = idx
            s["chapter_file"] = jp.name
            for key in ("start", "end"):
                if s.get(key) is not None:
                    s[key] = round(s[key] + offset, 3)
            new_words = []
            for w in s.get("words", []):
                w = dict(w)
                for key in ("start", "end"):
                    if w.get(key) is not None:
                        w[key] = round(w[key] + offset, 3)
                w["chapter"] = idx
                new_words.append(w)
                all_words.append({
                    "word": w.get("word", "").strip(),
                    "start": w.get("start"), "end": w.get("end"),
                    "score": w.get("score"), "chapter": idx,
                })
            s["words"] = new_words
            all_segments.append(s)
            if s.get("end"):
                seg_end = max(seg_end, s["end"])
        if not dur:
            dur = max(0.0, seg_end - offset)
        chapters.append({"chapter": idx, "file": jp.name,
                         "start": round(offset, 3), "end": round(offset + dur, 3)})
        offset += dur
        print("  +%-32s  %6.1f min  (book %6.1f min)" % (jp.name, dur / 60, offset / 60))

    book = {
        "title": title,
        "language": "en",
        "chapters": chapters,
        "duration_sec": round(offset, 3),
        "segment_count": len(all_segments),
        "word_count": len(all_words),
        "segments": all_segments,
    }
    (folder / f"{title}.json").write_text(json.dumps(book, ensure_ascii=False, indent=2), encoding="utf-8")
    (folder / f"{title}.words.json").write_text(
        json.dumps({"title": title, "language": "en", "chapters": chapters,
                    "word_count": len(all_words), "words": all_words},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print("\nwrote %s.json (%.1f MB) and %s.words.json  -  %d words, %.1f h"
          % (title, (folder / f"{title}.json").stat().st_size / 1e6, title,
             len(all_words), offset / 3600))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
