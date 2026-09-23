# AGENTS.md — Profanity_Filter

Detects profanity / "God's name in vain" in media and produces a cleaned copy
with those spans removed from the audio. Owns **all** profanity/irreverence
logic in this toolkit (the detection logic used to live in voice_to_text).

## Layout

| Path | What |
|---|---|
| `flag_language.py` | transcript scanner — finds every profanity / irreverence hit + timestamp in any common transcript format. Standalone CLI **and** importable API. |
| `wordlists/` | `profanity.txt`, `irreverence.txt` — plain-text match lists, re-read every run |
| `clean.py` | end-to-end: media in → transcript → flag → mute (or bleep) one audio track → mkvmerge remux → cleaned `.mkv` |
| `clean.ps1` | launcher (runs `clean.py` with the sibling voice_to_text project's venv, UTF-8 console) |
| `config.toml` | `clean.py` defaults |
| `bin/mkvtoolnix/` | portable MKVToolNix (mkvmerge v101) — `clean.py` finds it here automatically |
| `bin/7zr.exe` | standalone 7-Zip extractor (used to unpack the portable MKVToolNix) |
| `out/` | scratch dir only now — temp files during a run, plus `--keep-temp` debug artifacts. The cleaned result replaces the source file in place; see "Replacing the source in place" below |

No venv of its own — `clean.ps1` / callers use `..\voice_to_text\.venv\Scripts\python.exe`
(Python 3.11, already has everything). `flag_language.py` itself is pure stdlib.

**GPU lock**: two paths touch it now. `clean.py`/`_whisperx_check.py` invoke
`voice_to_text\transcribe.py` as a subprocess for any WhisperX work, and
`transcribe.py` itself holds `../gpu_lock` (if present) for that call.
Separately, `clean.py`'s own `_run_separator()` (the stemmer,
`mute_fill = "stems"` and `--method dialog`'s fallback path) holds the same
lock directly for each `audio-separator` invocation — it uses CUDA too, and
running it with no coordination was observed to crash outright under
contention on the machine this was built on, not just run slowly.
`_run_separator()` also retries a failed invocation up to `STEM_RETRY_ATTEMPTS`
times (a movie with many flagged spans calls it hundreds of times, and a rare
transient failure shouldn't sink the whole run). Both paths degrade to "no
queuing" if `../gpu_lock` isn't present. mkvmerge/ffmpeg/flag_language steps
here are CPU-only and never wait on it. See
`../gpu_lock/AGENTS.md`.

## flag_language.py

Reads any of: `.json` / `.words.json` (WhisperX — word-accurate times),
`.srt` / `.vtt` (cue), `.lrc` (line), `.tsv` (row), `.speakers.txt` (turn),
`.txt` (line numbers only).

```powershell
$py = "..\voice_to_text\.venv\Scripts\python.exe"
& $py flag_language.py "meeting.json"
& $py flag_language.py "C:\Transcripts" --recurse --report flags.txt
& $py flag_language.py "sermon.srt" --only irreverence
```

- Writes `<input-filename>.flags.json` next to each input (`--no-write` to skip).
- Folder input: one file per recording (`.json` > `.srt` > `.vtt` > `.lrc` >
  `.tsv` > `.speakers.txt` > `.txt`); `*.flags.json` always skipped.
- Exit `0` clean / `1` hits / `2` error; `--fail-on none` forces `0`.
- API used by `clean.py`: `load_matchers(only=...)`, `scan_file(path, matchers)`,
  `write_report(...)`, `count_categories(...)`, `load_word_timeline(path)`,
  `backfill_from_srt(srt_path, matchers, word_timeline)`.

### SRT backfill (catching what the transcript missed)

Whisper drops or mis-hears a surprising number of short interjections
("Shit!", "Fuck.") - the aligned transcript alone typically misses ~30-45% of
a film's actual profanity. `backfill_from_srt()` closes that gap by treating a
human-authored SubRip track as a second detection source:

1. Scan every SRT cue's text with the same word lists.
2. For each hit, align the cue's words (via `difflib`) against the transcript
   words spoken near that cue (±2.5s window, matched on punctuation-stripped
   tokens).
3. If the aligned transcript word **already matches the word lists**, the hit
   is a duplicate of one `scan_file` already found on the transcript directly
   - skip it.
4. Otherwise it's genuinely missed. Use the aligned transcript word's own
   timing if one lines up (a mis-heard substitution), or interpolate between
   the transcript words immediately before/after the gap if the word was
   dropped entirely, or fall back to the cue's own start/end as a last resort.

Hits from this path carry `"source": "srt-backfill"` and a locator ending in
`"(missed by transcript)"`. `scan_file()` itself tags every hit's `"source"`
with the format it came from (`"json"`, `"srt"`, ...).

Both the `flag_language.py` CLI (auto-detects a sibling `<name>.srt` next to a
`.json`/`.words.json` input; `--no-srt-backfill` to disable) and `clean.py`
(extracts the media's embedded SubRip track for this; `cfg.srt_backfill`) use
it. Validated on a real feature film: 25 transcript-only hits -> 46 total (21
were entirely absent from the transcript), zero duplicates after the fix below.

**Gotcha already hit and fixed:** normalize ASR word tokens the same way SRT
cue tokens are extracted (strip surrounding punctuation) before comparing them
- `"motherfuckers,"` vs `"motherfuckers"` looking different broke the
already-covered check and produced a duplicate hit at the wrong (interpolated)
time. Any future tokenization change to either side must keep them consistent.

### PGS OCR + bitmap censoring (Blu-ray bitmap subtitles, `pgs_ocr.py`)

A Blu-ray rip's subtitle tracks (`S_HDMV/PGS`, codec `hdmv_pgs_subtitle`) are
bitmap images, not text - `choose_subs()` skips them on purpose since
there's nothing to censor or backfill from directly. `pgs_ocr.py` closes
that gap in two stages, and the output stays image-based end to end: the
"(Cleaned)" subtitle track clean.py adds is the ORIGINAL bitmap with
flagged words redacted directly in the image, not a text conversion (an
earlier version of this feature converted to a censored SubRip text track
instead - abandoned the same day once the user pointed out that left every
*original* PGS track, still selectable, completely uncensored; the
image-in, image-out design below replaced it before the first real run).

**Stage 1 - `analyze_pgs_track()`** (runs during clean.py's normal subtitle-
discovery, same point the old text-track/CC608 fallbacks live): parses the
raw `.sup` segment stream mkvextract produces for a PGS track (Presentation
Composition / Window Definition / Palette Definition / Object Definition
segments), decodes each subtitle image's RLE-compressed indexed bitmap
(`_decode_rle`), and OCRs every cue ONCE with Tesseract via
`pytesseract.image_to_data` - getting both the plain cue text (written to a
cached `.srt`, used only for `srt_backfill`) AND per-word bounding boxes
(cached to a `.words.json`) in the same pass, so stage 2 never re-runs OCR.
Cached next to the source as `<name>.<lang>.pgsocr.{sup,srt,words.json}`
(reused on rerun unless `--retranscribe`/`cfg.retranscribe`) since OCR-ing a
full film is slow.

**Rendering trick**: rather than a full YCbCr->RGB palette conversion, each
pixel's grayscale value for OCR is just `255 - alpha` from its palette entry
- text (opaque, high alpha) comes out dark, background (transparent, alpha
0) comes out white, regardless of the subtitle's actual on-screen color.
Sidesteps color entirely, which is fine since redaction never needs to
preserve it either (a masked pixel becomes fully transparent, not a
same-color box).

**Cue timing** is derived exactly, not estimated: a Presentation Composition
Segment with zero composition objects is PGS's standard "clear the screen"
marker, so a cue's end is the next display set's own PTS - covers both a
genuine clear and the next subtitle's appearance - instead of a fixed guess
like "+4s" (which `_pgs_cue_starts`/`subtitle_dialogue_spans` still use,
since that code only needs rough dialogue *timing*, not the text itself).

**Stage 2 - `censor_pgs_track()`** (runs once `find_spans()` has the final
audio-removal spans, same point `censor_srt()` runs for a text track):
re-scans each cue's cached OCR'd words against the SAME wordlist matchers
used for audio (mirrors `censor_srt`'s re-scan-the-cue-text approach rather
than trying to align to transcript timing word-for-word), and for every
match in a cue overlapping a flagged span, redacts it DIRECTLY IN THE
BITMAP:

1. `_encode_rle()` is the RLE encoder side of `_decode_rle` - not
   byte-optimal, just correct (always uses the escape+run form), since it
   only has to round-trip through the decoder, not match the original
   encoder's exact choices.
2. **Finding what to redact was the hard part** - Tesseract's own per-word
   box, taken at face value, was confirmed on this project's real test
   source (Hamilton (2020), a bold italic/stylised font) to undershoot a
   glyph's TRUE left edge by ~29px on a ~50px-tall word ("whore" mis-boxed
   well into its own letters) - not a small-margin problem a fixed or
   proportional pad can paper over. Two things fixed it together:
   - Words are grouped by OCR line (`ocr_cue`'s `"line"` field) and sorted
     left-to-right, so a flagged word's redaction bounds are capped by its
     same-line NEIGHBORS' centers, not by its own (unreliable) box edges or
     an edge-to-edge midpoint (a midpoint was tried first and still cut off
     real ink - see the git history for that dead end).
   - Within that cap, `_ink_columns()` reads the REAL per-object index
     bitmap (not OCR geometry at all) and the redaction box is grown
     pixel-by-pixel outward from the OCR box's own center until it hits an
     actual all-transparent column/gap - i.e. the true glyph edge - capped
     only as a last-resort backstop against engulfing an entire connected
     neighbor when no whitespace exists in the bitmap at all.
3. `_splice_sup()` rewrites the `.sup`: every segment NOT touched (PCS/WDS/
   PDS/END, every other object's ODS) is copied through as an exact byte
   slice; a touched object's ODS segment(s) are replaced with freshly
   RLE-encoded, redacted data via `_build_ods_segments()` (handles
   fragmentation for an object too big for one segment - same PTS, same
   object id/dimensions, just different pixels).

**Wiring in `clean.py`**: `choose_pgs()` finds the PGS track,
`locate_tesseract()` finds the OCR binary, stage 1 runs during subtitle
discovery (sets `pgs_source_track`/`pgs_cache`, and `raw_srt`/`chosen_subs`
for `srt_backfill` exactly like the old design did). Once spans are known,
`main()` branches on `pgs_source_track is not None` instead of going through
`censor_srt()`: stage 2 runs, and the resulting `.sup` is passed to
`remux()` as `clean_srt` (the parameter is generic - mkvmerge autodetects
`.sup` as `S_HDMV/PGS` on import same as it does `.srt` as SubRip, so
`remux()` needed no changes to accept either).

**Known limitation**: matching is per-OCR'd-word-token, so a multi-word
phrase in `irreverence.txt` (default categories are `["profanity"]` only,
all single words, so this doesn't bite yet) would never match here, unlike
`censor_srt()` which scans a whole cue's text at once. Worth revisiting if
`irreverence` phrases are ever enabled for a PGS-only source.

**Setup**: needs Tesseract OCR installed (`winget install --id
UB-Mannheim.TesseractOCR`) and `pytesseract` + `numpy` in the shared
`voice_to_text\.venv` (`pip install pytesseract`; numpy/Pillow are already
there). `locate_tesseract()` checks fixed install locations before PATH,
since a winget install updates the registry-level user PATH that an
already-running shell/process won't see until it restarts.

**Validated** 2026-09-23 against a real Blu-ray rip (Hamilton (2020), no
text subtitle track at all - only PGS): the parser/renderer/OCR chain
correctly reproduced the film's actual opening dialogue/lyrics from raw
`.sup` bytes with exact PTS-based timing; `srt_backfill` correctly caught
"bastard"/"whore" from the real lyric ("How does a bastard, orphan, son of
a whore..."); a full clean.py run (transcript -> flag -> mute -> PGS
censor -> mkvmerge remux) on a 1-minute real clip produced a new default
PGS "(Cleaned)" track with both words visibly and fully blanked (confirmed
by pixel-diffing rendered before/after cue images, not just eyeballing a
PNG) with zero bleed into the surrounding words ("How does a [blank]
orphan", "Son of a [blank] and a Scotsman"). OCR/detection is still
inherently approximate - a missed transcription/OCR means a missed
redaction - so this is "much better than nothing," not as authoritative as
manual review. Only `S_HDMV/PGS` is handled, not `S_VOBSUB` (DVD-era bitmap
subs use a different encoding `pgs_ocr.py` doesn't parse).

### Word lists

One entry per line: literal phrase (whole-word, case-insensitive) or
`re:<regex>`; `#` starts a comment. `irreverence.txt` ships with only the
high-precision expletive patterns active ("goddamn", "oh my God",
"Jesus Christ", "for heaven's sake", minced oaths); a commented block at the
bottom, if enabled, flags every bare "God" / "Jesus" / "Lord" / "Christ" —
expect many false positives on devotional content. Tune the lists rather than
hard-coding terms in the script.

## clean.py — remove profanity from a media file

```powershell
.\clean.ps1 "C:\Media\Movie (2002).mkv"
.\clean.ps1 "Movie.mkv" --dry-run
.\clean.ps1 "Movie.mkv" --method bleep --beep-gain-db -8 --keep-temp
```

Steps:
1. **transcript** — reuse `<name>.json` beside the input, else run
   `voice_to_text\transcribe.py --formats json --no-diarize`.
2. **flag** — `flag_language.scan_file` on the json, categories from config
   (`profanity` and `irreverence` by default); if the media has an embedded SubRip track,
   `mkvextract` pulls it and `backfill_from_srt` adds anything the transcript
   missed entirely (`cfg.srt_backfill`, default on; `--no-srt-backfill`).
3. **remove** — one `ffmpeg` pass over ONE audio track (`source_track` picks
   the highest-channel-count track among whichever language the container's
   default-track flag would have selected, not just that flag blindly - see
   `choose_audio()`), method-dependent:
   - `method = "mute"` **(default)** — never dead air: every flagged span gets
     vocals stemmed out of every channel for just that clip (ambient noise/
     music keeps playing through, unbroken) and spliced back into the
     otherwise-untouched original; whichever of per-span or whole-track
     stemming a calibrated time-cost model predicts will be faster is used
     (`_predict_stem_seconds`). No stemmer installed -> falls back to
     `volume=0` silence. See "Always-stem muting" below.
   - `method = "bleep"` — `volume=0` on each padded span + a gated `aevalsrc`
     1 kHz tone at `beep_gain_db` peak dBFS (`amix ... normalize=0`).
   - `method = "cut"` — the flagged span is spliced out and the gap closed
     (`aselect='not(SPANS)',asetpts=N/SR/TB`), shortening the file. **Refuses
     any input with a real video track** (`has_video_track()` - embedded cover
     art, i.e. an attached-picture stream, does not count) since cutting audio
     out of a video desyncs it from the picture. See "cut" below for the rest.

   `mute`/`bleep` results are encoded to a standalone file - the only thing
   re-encoded - then muxed back in by mkvmerge (step 5/6). `clean_codec`
   defaults to `ac3` at `clean_bitrate` (224k for a <=2ch source) or
   `clean_bitrate_surround` (448k for >2ch); `flac` is available for a
   lossless (much bigger) track. `cut` instead re-encodes with a codec
   matching the *source's own codec* (`CUT_CODEC_MAP`) and writes directly to
   `out/<name> (Cleaned)<source's own extension>` - no mkvmerge step at all (see
   below).
4. **subs** (`mute`/`bleep` only) — the chosen text subtitle track (Matroska
   SubRip, MP4 "Timed Text"/tx3g via ffmpeg's built-in conversion, or an
   embedded CEA-608 closed-caption track as a fallback when the primary pick
   turns out to be an empty/placeholder track - mkvmerge doesn't even list
   CEA-608 tracks, so that fallback probes with ffprobe instead, see
   `find_cc608_track`) has every cue that overlaps a removed span (± `subs_pad`
   s) censored: `method = "mute"` deletes the flagged word(s) entirely
   (matching the audio, which has no audible trace left either);
   `method = "bleep"` replaces them with `subs_mask` (`***`) instead, matching
   the audible tone. Image subs (PGS/VobSub) and ASS are skipped with a note.
5. **remux** (`mute`/`bleep` only) — `mkvmerge` copies the original bit-for-bit
   and appends the clean audio **and** clean subtitle as new **default**
   tracks named `<original label> (Cleaned)` (same `clean_label()` rule for
   both); the originals' default flags are cleared; `--track-order` puts each
   clean track first among its type. Video / other tracks untouched.
6. **replace** — the build (steps 1-5) lands in a temp dir under `output_dir`
   (`out/` by default), never touching the source. Only once it succeeds:
   the pre-clean original is moved to the **Recycle Bin** (`send_to_recycle_bin`
   - `ctypes` + `shell32.SHFileOperationW`, `FOF_ALLOWUNDO` - recoverable, not
   a hard delete, no `send2trash` pip dependency needed) and the build is
   staged next to it and renamed into its place (`main()`'s `final_dest`/
   `staging` dance - see "Replacing the source in place" below).

Output: replaces the source file at its own path (same folder/stem; the
extension only changes for `mute`/`bleep`/`dialog` on a non-`.mkv` source,
since mkvmerge always writes `.mkv` - `cut` always keeps the source's own
extension, so its destination path is always identical to the source's) +
`<name>.bleeps.json` next to it (`subtitles` field records cues/words masked,
null for `cut`).

### Replacing the source in place

`main()` computes `final_dest = media.with_name(f"{media.stem}{out_ext}")`
early (right after `out_ext` is decided) and guards it the same way the old
`out_path` was guarded: `--overwrite` is only consulted when `final_dest !=
media` (extension changed) and something already sits there from an earlier
run - when `final_dest == media` (the common case: source and result are both
`.mkv`), there's nothing to guard, since replacing the source *is* the point.

The actual build target inside the run is `build_path = tmp / f"build{out_ext}"`
(inside the per-run `TemporaryDirectory`, `dir=out_dir`) — every method
(`cut`'s `shutil.copy2`, `dialog`'s and `mute`/`bleep`'s `remux()` calls)
writes there, never to `media` or `final_dest` directly, since mkvmerge/ffmpeg
are still reading `media` at that point. Only after that build finishes does
`main()` touch the source, and in a specific order chosen for safety:

1. `shutil.move(build_path, staging)` where `staging = final_dest.with_name(
   final_dest.name + ".pf-staging")` — this is the one step that can be a
   slow **cross-drive** copy (e.g. `output_dir` on `D:` for a source on `J:`,
   the `_batch_pe3.py` case) and it happens while the original is still
   completely untouched.
2. `send_to_recycle_bin(media)` — only now does the original move, and only
   to the Recycle Bin, never a permanent delete.
3. `staging.replace(final_dest)` — a same-drive rename, about as close to
   atomic as this gets, now that the slow/fallible part is already done.

If step 1 fails, the source is never touched. If step 3 somehow fails after
step 2 succeeded, the built file is recoverable at `staging` and the original
is recoverable from the Recycle Bin - the failure mode is "user has to
reconcile two files by hand," never silent data loss.

The `.bleeps.json` report path is derived from `final_dest`, not `media` -
`final_dest.parent / f"{final_dest.stem}.bleeps.json"` - and is written mid-run
(as a crash-safety net, before the source is touched) as well as again at the
very end. `output_dir`/`--output-dir` (default `out/`) is scratch space only
now: the `TemporaryDirectory` location and where `--keep-temp` drops debug
copies of the clean track / filter graph / clean subtitle. It is **not** where
the cleaned result ends up - that's always `final_dest`.

### Re-running on an already-cleaned file (`mute`/`bleep` only)

Running `clean.py` a second time on a file it already cleaned doesn't just
add another `(Cleaned)` track alongside the old one, and doesn't blindly
redo the (often expensive - stemming, PGS OCR) work either. `main()`
fetches `tracks_all` (mkvmerge `-J`) and splits off `stale_ids` - every
audio/subtitle track whose `track_name` ends with `cfg.track_name_suffix`/
`cfg.dialog_track_suffix` (`is_own_output_track()`) - i.e. a track THIS TOOL
added on a prior run. Everything that picks a track to work from
(`choose_audio`/`choose_subs`/`choose_pgs`/`dialog_remove_track`'s dialogue-
timing derivation) only ever sees the **stale-filtered** `tracks` list, so a
rerun always re-detects from the true original source, never re-cleans an
already-cleaned track or re-censors an already-censored subtitle.

A fresh detection pass then runs exactly as normal (transcript reused from
cache, wordlists re-scanned, PGS OCR cache reused) and its word set -
`sorted({h["match"].lower() for ... in spans for h in hs})` - is compared
against the PREVIOUS run's own `<final_dest.stem>.bleeps.json`
(`_load_prev_flagged_words()` - already exactly what that report's `spans`
field records, no new state file needed):

- **Same set** (and no `--force`): skip the rebuild entirely - no ffmpeg/
  mkvmerge/stemming/PGS-censoring work, source untouched. This is the
  common case for a rerun with no real reason to redo anything (e.g. a
  scheduled recheck, or rerunning after an unrelated crash).
- **Different set**, or `--force` given: rebuild as normal. `remux()`'s
  `exclude_audio_ids`/`exclude_subs_ids` (built from `tracks_all`, since the
  stale ids no longer exist in the filtered `tracks`) drop the stale
  track(s) from `media`'s import via mkvmerge's `--audio-tracks
  '!id,id'`/`--subtitle-tracks '!id,id'` negation syntax - the fresh track
  REPLACES the stale one in the same build, rather than a second rebuild
  needing to clean up after the first.
- **Nothing flagged on this pass** (spans empty) but a `(Cleaned)` track
  already exists: the existing track is left as-is - there's nothing to
  build a replacement from, and this tool doesn't "un-clean" a file.

Scoped to `mute`/`bleep` only, matching what the user actually asked for
("only if that would result in a different set of words"): `dialog` doesn't
use wordlists at all (it strips ALL dialogue, so there's no word set to
compare - it always reruns), and `cut` fully replaces the file with a
shorter one with no separate alt track to detect/compare against in the
first place.

**Known gap, not fixed here**: `ensure_transcript()` reuses `<name>.json` if
present (the common case, and the only case this matters for) but otherwise
hands the WHOLE media file to `voice_to_text\transcribe.py`, which picks its
own default audio track - after a first clean.py run, the container's
default track IS the `(Cleaned)` one. A rerun with no transcript cache
(deleted, or `--retranscribe`) would transcribe the wrong (already-muted)
track. Not hit in practice since the cache normally exists by the time a
rerun happens; would need `ensure_transcript` to extract the chosen original
track itself rather than handing the whole container to `transcribe.py` to
close properly.

### "cut" (audio-only inputs only, e.g. audiobooks)

Cutting removes time, so it fundamentally can't work the way `mute`/`bleep` do:

- **On an mp3 source, cutting is genuinely lossless.** `mp3_splice_cut()`
  never decodes or re-encodes a single sample of audio that survives: MP3
  frames are independently decodable, so each *kept* region between flagged
  spans is pulled out with a plain `-c:a copy` (output-side `-ss`/`-to`,
  accurate even on this project's VBR Audible rips, unlike input-side
  seeking which trusts a possibly-stale Xing TOC), then all the kept regions
  are joined back with ffmpeg's concat demuxer, still `-c copy`. Only the
  flagged spans themselves disappear; everything else is bit-identical to
  the source. `cut_track()` (the original decode+re-encode implementation)
  is now the fallback for every *other* codec - AAC/Opus/etc. don't splice
  cleanly this way (inter-frame prediction/priming makes a naive concat
  click or glitch at the seams), so they still go through a full
  filter_complex `aselect+asetpts` pass.
  Known imperfection: the source's Xing/LAME VBR header (a fake first frame
  holding the *original* total frame/byte count, used for fast duration
  display and gapless-playback trim padding) is dropped rather than
  recomputed - doing that losslessly isn't possible, and regenerating it
  requires re-encoding, defeating the point. ffprobe's own duration on the
  spliced file is exactly right (it falls back to scanning frame headers);
  an old/strict player that blindly trusts a Xing TOC without sanity-checking
  it against the real frame count could show a slightly wrong duration.
  Validated on a real chapter of a commercial audiobook: 5 flagged words
  across 822s, spliced to 819.7s, re-transcribing
  the output found **zero** remaining hits, and every one of the source's 14
  ID3v2 frames - including the embedded cover art - survived (see next
  point).
- **ID3v2 tags are copied via mutagen, not ffmpeg.** `preserve_id3_tags()`
  loads the source's full `ID3` object and calls `.save(dest)` on it -
  every frame carries over byte-for-byte: title/artist/album/track/genre/
  comment, embedded cover art (`APIC`, which `ffmpeg -map_metadata` silently
  drops - it only round-trips text frames), and any nonstandard frames
  (Audible rips carry `WOAS`/`UFID`/`NARRATEDBY`, etc.). If the source has
  ID3v2 chapter frames (`CHAP`/`CTOC` - rare when a book is already split
  one file per chapter, but real for a single-file audiobook), each `CHAP`'s
  start/end is remapped onto the post-cut timeline with the same
  `remap_time()` used for ffprobe chapters below, and one entirely swallowed
  by a cut span is dropped; its (rarely-used) byte-offset fields are set to
  the ID3 "not used" sentinel (`0xFFFFFFFF`) since splicing invalidates them.
  Only applies when the source codec is mp3 - other containers use a
  different tag scheme (MP4 atoms, etc.) that this doesn't touch yet.
- **Video would desync.** `main()` calls `has_video_track(probe_streams(...))`

- **Video would desync.** `main()` calls `has_video_track(probe_streams(...))`
  before doing anything else when `method == "cut"`, and refuses (exit 1) if
  the input has a real video stream. Embedded cover art (`disposition.
  attached_pic`) is explicitly excluded from that check - m4a/m4b/mp3
  audiobooks routinely carry cover art and must NOT be refused for it
  (validated: a synthetic m4a with an attached-picture video stream correctly
  reports `has_video_track() == False`).
- **No dual-track output.** `mute`/`bleep` keep the original audio in the file
  as a non-default alt track since the runtime is unchanged. A cut file has a
  *different* runtime, so putting old and new audio in one container as
  parallel tracks is meaningless (Matroska doesn't support tracks of different
  durations coherently). `cut_track()` writes the final file directly; there's
  no `remux()` call, no mkvmerge, no alt track.
- **Codec/container matches the source.** `CUT_CODEC_MAP` picks an ffmpeg
  encoder from the *source's* `ffprobe codec_name` (mp3->libmp3lame,
  aac->aac, flac->flac lossless, etc.) and the output keeps the source's own
  extension - an `audiobook.m4b` comes back as `audiobook (Cleaned).m4b`, not
  forced into `.mkv`. An unrecognised source codec falls back to mp3.
- **Chapters are remapped onto the post-cut timeline**, not dropped.
  `remap_time(t, spans)` shifts any original timestamp `t` left by the total
  duration of every cut span before it (a `t` that falls *inside* a cut span
  collapses to that span's own remapped position, matching where
  `aselect+asetpts` actually puts the surrounding audio). `probe_chapters` +
  `build_remapped_chapters` apply this to every chapter's start/end from
  `ffprobe -show_chapters`, write an FFMETADATA1 file, and feed it back in as
  a second `-i` with `-map_chapters 1` (global metadata still comes from the
  source via `-map_metadata 0` - the two are independent). A chapter entirely
  swallowed by a cut span (remapped end <= start) is dropped rather than
  emitted as a zero/negative-length chapter; everything else keeps its title.
  No chapters in the source -> `-map_chapters -1`, nothing to do.
- Other metadata (title/author tags) IS copied (`-map_metadata 0`).

Validated end-to-end on synthetic clips built from a real movie's dialogue
(no real audiobook in this repo yet): a 90s mp3 and a 90s m4a-with-cover-art,
each containing one real flagged word - both correctly transcribed, flagged,
cut (duration shrank by exactly the flagged span's length, gap closed with no
introduced silence per an `astats`/level continuity check across the splice
point), and written back in their source format. A video `.mkv` input was
confirmed to hit the refusal immediately, before transcription. Chapter
remapping validated on a 3-chapter (0/30/60/90s) synthetic m4b-style file with
the same flagged word at 25.4-26.0s (inside chapter 1): output chapters came
back at exactly 0-29.4 / 29.4-59.4 / 59.4-89.4s, titles and title/artist tags
intact. A second test with a chapter tiny enough to sit entirely inside the
cut span confirmed it's dropped cleanly, with the chapters on either side
meeting exactly at the cut point (no gap, no overlap).

### Always-stem muting (`method = "mute"`, the default)

`mute_track()` no longer does center-channel-only muting at all - every
flagged span is stemmed instead (see "Center-channel-only removal" below for
where that logic still lives, now `--method dialog` only). Why: the old
approach silenced every channel everywhere *except* the flagged spans with
`volume=0:enable='not(SPANS)'` and compared each channel's RMS to decide
center-only vs whole-track muting, but that whole-file `enable=` expression
was found to **not reach true silence** when evaluated from the start of a
long file - confirmed reproducible even with a single span and with lossless
FLAC (rules out both the ~100-term AVExpr limit below and codec artifacts as
the cause). Stemming sidesteps the bug entirely and reads as the better
result anyway: ambient noise/music keeps playing through a flagged word
instead of a silent gap, or, on a track with no clean center channel, the
whole mix dropping out.

Two paths, picked by `_predict_stem_seconds()` - a calibrated time-cost model,
not a word-count guess:

- **Per-span** (`splice_stemmed_spans`): each flagged span (plus
  `SPLICE_CONTEXT_S` of context for the separator to work with) is extracted,
  stemmed, trimmed back to the exact span, and spliced into the
  otherwise-untouched original via ffmpeg's concat demuxer. Cheap for a
  handful of spans; the separator's model-load overhead is paid once per
  span per channel.
- **Whole-track** (`splice_whole_track_stem`): the entire track is stemmed
  once (`build_instrumental_stem` - one fixed cost regardless of span count),
  then the flagged spans are sliced out of that and spliced against the
  original the same way. Cheaper once per-span overhead adds up on a
  heavily-flagged file.

`_predict_stem_seconds()` models each separator invocation as
`STEM_STARTUP_S + STEM_RATE * duration` (fixed per-call startup - ONNX model
load + CUDA context init + the surrounding ffmpeg extract/fold calls - plus
throughput once past startup) and sums that per-channel over every span
(per-span path) vs. once over the whole file (whole-track path), picking
whichever total is lower. `STEM_STARTUP_S`/`STEM_RATE` were empirically
calibrated against real per-span and whole-track runs - re-measure both if
the GPU or stemmer model changes. This replaced an earlier
`stem_whole_track_words` word-count threshold, which was a rough proxy that
ignored the source's own runtime.

Every splice, in both paths, is built from short, independently-seeked
segments rather than one filter pass across the whole file - the identical
filter graph on a pre-seeked short clip *is* exact, which is exactly why the
old whole-file approach was dropped in favor of this.

No stemmer installed (`STEM_VENV` not found) -> falls back to the old plain
`volume=0:enable='SPANS'` silence on the whole track.

### Center-channel-only removal (`--method dialog` only)

Movie 5.1/7.1 mixes almost always carry dialogue on the center channel alone,
with music/effects spread across the others. `--method dialog` (which strips
*all* dialogue from the track, not just flagged words) exploits that: when
dialogue really is isolated to the center channel, muting only that channel
removes every spoken word with the background continuing completely
unbroken - a much better result than stemming dialogue out of every channel,
which is the fallback whenever the center channel isn't clean. (`method =
"mute"` used to make this same per-flagged-word decision - see "Always-stem
muting" above for why it doesn't anymore; everything below is
`dialog_remove_track()` only.)

1. Skip entirely if the chosen track has <=2 channels (no center channel is
   possible) - stem dialogue out of the whole (mono/stereo) track.
2. Otherwise `ffprobe` the track's `channel_layout` (e.g. `5.1(side)`, `7.1`)
   and look it up in `CHANNEL_LAYOUTS` (built from `ffmpeg -layouts`, the
   authoritative channel-order source). If the layout isn't recognised or has
   no `FC` (center) position - e.g. plain `stereo`, `quad`, `6.0(front)` -
   stem dialogue out of every channel; don't guess an index from the channel
   count alone.
3. **Detect** (`detect_center_dominance`): silence every channel everywhere
   *except* a set of test spans, then run `astats` and compare each channel's
   RMS *during just those spans*. Because every channel gets the identical
   silence mask elsewhere, this comparison is unaffected by the rest of the
   file - it purely measures who's loudest while dialogue is actually
   happening. If the center channel is at least `center_margin_db` (default
   6 dB) louder than every other channel, dialogue is confirmed center-only.
   Test spans come from the source's own subtitle track when one exists (see
   the subtitle-gating note below), falling back to the whole file only when
   there's no usable subtitle track.
4. **Remove**: if confirmed, `channelsplit` the track into its named
   channels, mute *only* the center pad for the entire runtime, pass every
   other pad through with `anull`, then `join` them back with an explicit
   `map=i.0-<ChannelName>` (so the container keeps its real layout tag) -
   channel count trivially preserved. If not confirmed, the stemmer extracts
   the non-dialogue content from every channel instead
   (`build_instrumental_stem`) and that becomes the whole output track.

Validated with synthetic 5.1 WAVs (`ffmpeg -f lavfi ... amerge ... aformat=
channel_layouts=5.1`) plus real multichannel sources (see below). Confirmed
on synthetic data: (a) center-dominant case -> non-center channels measured
**bit-identical** (via `astats`) inside vs. outside the muted span, center
channel drops ~64 dB (silence) only inside it; (b) even-energy case ->
correctly falls back to stemming every channel instead of guessing.

#### `detect_center_dominance` span-count ceiling (fixed) + `--method dialog`'s default test window

`detect_center_dominance`'s `enable='not(SPANS)'` expression goes through
ffmpeg's AVExpr boolean parser, which **hard-fails past ~100 chained
`between(...)+between(...)+...` terms** - bisected empirically against a real
build: 99 terms parse fine, 100 fails every time ("Error when evaluating the
expression"), so it's a hard-coded limit in ffmpeg itself, not a length/perf
thing. This was originally found via the old `mute_track()`'s per-flagged-word
spans, which rarely hit it (that whole code path is gone now - see
"Always-stem muting" above); `dialog_remove_track`'s spans (one per subtitle
cue, for a whole episode) hit it routinely. Fixed by batching (`_ASTATS_BATCH_LIMIT = 80`) and combining the per-batch dB
readings correctly (`_combine_batch_rms`) - dB doesn't average linearly, and
each batch is itself diluted by measuring across the WHOLE file with only its
own spans un-silenced, so naive averaging would double-count that dilution.
`detect_center_dominance` now takes `ffprobe` too (needs the file's total
duration for the correction) - update both call sites if you touch this.

`--method dialog` (`dialog_remove_track`) has no natural "flagged spans" to
gate the dominance check on (it's removing ALL dialogue, not specific words),
so it originally tested across the WHOLE file. **That's a real trap**: a
nature-documentary mix can spend most of its runtime on narration-free music/
effects, which dilutes an over-the-whole-file RMS comparison enough to hide a
center channel that's actually clearly dominant specifically while the
narrator is talking. Confirmed on a real nature-documentary episode
(embedded AC3 5.1(side)): whole-file reading put the center channel
as the QUIETEST of the six (~-86 dB vs ~-30 to -43 dB elsewhere); re-tested
using only the moments its own embedded PGS subtitles say something's being
said, center came out LOUDEST (~-27/-25 dB vs ~-30/-32 dB on front L/R) - the
opposite conclusion. Fixed by making that the default: `dialog_remove_track`
now pulls dialogue-cue spans from the source's own subtitle track
(`subtitle_dialogue_spans` - a text track gives exact cue end times via
`flag_language.parse_srt`; an image-based one like PGS/VobSub only carries a
presentation start per cue, so the end is estimated: whichever comes first of
+4s or the next cue's start) and gates the center-channel test on THOSE,
falling back to the old whole-file span only when the source has no usable
subtitle track at all.

**Real-world margin data point** (same episode, properly subtitle-gated -
143 merged spans, 2353s of confirmed dialogue time): center beat the loudest
other channel by **~4.7-5.2 dB** (DTS 5.1 / AC3 5.1 respectively) - a real,
consistent signal, but UNDER the 6 dB default `center_margin_db`, so out of
the box this source still falls through to the (much slower) stemmer path
despite the center channel genuinely carrying the dialogue. Spot-checks on
two other episodes of the same series (short fixed windows, not properly
subtitle-gated) also showed center consistently loudest by a few dB. Given
the existing note above that 6 dB was never more than a synthetic-test
starting point: a nature-documentary narration mix may want a value around
3-5 dB via `--center-margin-db` rather than the default - hasn't been raised
to the user for a permanent default change, so `config.toml`'s `6.0` is
untouched; this is a per-source tuning note, not a validated new default.
Superseded for actually deciding fast-vs-slow-path on a real batch by the
WhisperX validator below, which doesn't need a margin at all.

#### WhisperX validation: ground truth instead of a dB proxy (`_whisperx_check.py`)

A dB-margin test is a proxy for "is there residual narration" - it can't
actually tell whether what leaks through is intelligible. `_whisperx_check.py`
(standalone, not yet merged into `clean.py`) asks the real question directly
for `--method dialog`'s center-channel-only case: pick the longest
subtitle-confirmed dialogue spans, run WhisperX (via voice_to_text) on both
the real audio and a center-channel-muted version of the same spans (the
exact `build_mute_filter` graph `dialog_remove_track` would use), and
compare. A clean mute leaves only short generic hallucinated phrases
("Thank you.", "Oh, God.") with near-zero word overlap against the real
transcript (which reads as actual coherent, on-topic narration - "Columbus
crabs are thriving...", not word salad); real bleed-through shows up as an
actual matching sentence fragment. `whisperx_validate_center_mute()` requires
EVERY tested window (default 3, the longest merged spans) to score under
`overlap_threshold` (0.2) to pass.

This matters because the dB-margin dilution bug above was investigated USING
this validator, and the fix changed the real-world verdict: one episode's
sidecar (see the gotcha below) measured as center being the QUIETEST channel
by ~58 dB - WhisperX confirmed nothing intelligible came through either way
on that bad source, so it wasn't informative there. But the *embedded* track,
tested with the batching fix, showed a real ~5 dB margin - still under the
6 dB default - and WhisperX gave a clean, unambiguous "PASSED, no residual
narration" on all three tested windows. That result generalised: every
remaining episode in the same season/series (10 files across two quality
tiers) passed WhisperX validation too, at the exact ~4-5 dB margins that a
straight `center_margin_db=6` default would have rejected outright and sent
through 45-75 minutes of unnecessary per-channel stemming instead of the
~10-20 minute center-mute pass. **Practical effect on a batch**: a driver
script can run this check per file and force `--center-margin-db -99` (skip
the dB gate entirely) when it passes, falling back to the normal margin test
(and from there, to stemming) when it doesn't - see `_whisperx_check.py`'s
own docstring for the shape of that. One episode had a window with overlap
0.171 - close to the 0.2 cutoff, and the leaked words read as a real
narration fragment, not hallucination - still passed since every window has
to fail to reject the whole file, but it's the closest call seen; worth a
listen-through if this method gets relied on somewhere prose accuracy
matters more than a documentary M&E track.

Not yet wired into `clean.py` itself as a first-class option (no
`--validate-with-whisperx` flag) - it depends on voice_to_text/WhisperX,
which `mute_track`/`dialog_remove_track` don't otherwise require, and the
per-file cost (subtitle extraction + several short WhisperX transcriptions)
is real, if much cheaper than stemming. Promote it if this keeps proving out
on other sources - the pattern (pick real dialogue spans, transcribe muted +
unmuted, compare) generalises past center-channel muting to validating any
dialogue-removal method, including the stemmer's own output.

**Sidecar-file gotcha, logged as a warning for future batch work**: don't
assume a `.ac3`/`.wav` sitting next to a media file is a clean, untouched
extraction of one of its embedded tracks. One episode's sidecar turned out to
already have its center channel zeroed (every other channel matched the
embedded track to within ~0.03 dB; center was ~-86 dB in the sidecar vs
~-30 dB in the embedded track) - almost certainly a leftover/abandoned manual
attempt, not something this project produced. Two other episodes' sidecars
were unmodified plain extracts. Verify with a quick `astats` comparison
against the embedded track before trusting a sidecar as input to anything.

### Known rough edges (POC)

- Word-level timestamps drive the spans; `pad_start`/`pad_end` (default 0.10s)
  cover alignment slop. Tune per source.
- Only one audio track is cleaned (`source_track`, default = the highest-
  channel-count track among the container-default's language - see
  `choose_audio()`). Multi-track / commentary handling is not built.
- If lip-sync of the clean track drifts, measure the offset and set
  `sync_ms` (passed to `mkvmerge --sync`).
- Subtitle censoring is cue-level: a cue holding both a removed word and an
  un-removed profanity gets both censored (no per-word sub timing to split
  on). Different subtitle wording than the ASR (e.g. "friggin'") is not
  caught.
- The per-span-vs-whole-track stemming choice (`_predict_stem_seconds`,
  "Always-stem muting" above) is calibrated against a specific GPU and
  stemmer model - re-measure `STEM_STARTUP_S`/`STEM_RATE` if either changes,
  it's not something exposed as a per-run/per-library setting anymore.
- `center_margin_db` (6 dB, `--method dialog` only now) was chosen from a synthetic test, not a corpus of
  real mixes - loud action/music scenes with softer dialogue may need a lower
  margin, tune per source with `--center-margin-db`.
- **Quote comma-separated values passed to `clean.ps1`** (`--categories
  profanity,irreverence`, etc.) - `clean.ps1` captures forwarded args via
  `[Parameter(ValueFromRemainingArguments=$true)][string[]]$Passthru`, and
  PowerShell parses a bare unquoted comma as its array-constructor operator
  at the call site, before the script ever runs. The resulting array then
  collapses back into a single `[string]` slot by joining with `$OFS`
  (a space), so `--categories profanity,irreverence` silently arrives at
  `clean.py` as `--categories "profanity irreverence"` - a category name
  that matches neither `profanity` nor `irreverence`, so `load_matchers`
  builds an **empty** matcher dict and the run reports "0 hit(s)" with no
  error, no warning. (Confirmed root cause of a real false-negative run on
  a feature film that clearly had flaggable language - `--dry-run` even
  reported the wrong "0 hit(s), nothing flagged" as if the source were
  clean.) Quoting the value (`--categories "profanity,irreverence"`) avoids
  it entirely - a quoted string is never parsed as an array literal.
  Native-exe invocations (calling `python.exe` directly instead of through
  a `.ps1`) are NOT affected - only PowerShell-script/function argument
  binding triggers this.
- **If you ever relocate `.venv-stem`**: every pip console-script `.exe` in
  `Scripts\` (`audio-separator.exe` included) embeds an absolute path to that
  venv's own `python.exe`, so moving the folder breaks them all instantly and
  silently (exit code 1, no error text). See `../voice_to_text/AGENTS.md`'s
  matching note for the fix (`--force-reinstall --no-deps` per affected
  package, pinned to the currently-installed version so it hits the local
  cache).

## External tools

- `ffmpeg` / `ffprobe`: must be on PATH.
- `mkvmerge`: bundled at `bin\mkvtoolnix\`. To update: download the portable
  `.7z` from mkvtoolnix.download, `bin\7zr.exe x mkvtoolnix.7z -obin`, delete the
  old `bin\mkvtoolnix`.
