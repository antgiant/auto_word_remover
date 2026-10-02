# AGENTS.md — Profanity_Filter

Detects profanity / "God's name in vain" in media and produces a cleaned copy
with those spans removed from the audio. Owns **all** profanity/irreverence
logic in this toolkit (the detection logic used to live in voice_to_text).

## Keeping the README current

When you add or materially change a user-facing feature here, update the
root [`README.md`](../README.md) (and this project's own `README.md` if
relevant) to reflect it — the root README is the marketing/feature summary
for the whole toolkit and goes stale fast otherwise.

## Layout

| Path | What |
|---|---|
| `flag_language.py` | transcript scanner — finds every profanity / irreverence hit + timestamp in any common transcript format. Standalone CLI **and** importable API. |
| `opensubtitles.py` | last-resort subtitle source — search + download from OpenSubtitles when there's no usable local subtitle at all (see "OpenSubtitles fallback" below). Standalone, pure stdlib. |
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
manual review. `S_VOBSUB` (DVD-era bitmap subs, a different encoding this
module doesn't parse) has its own equivalent module - see `vobsub_ocr.py`
below - tried as a further fallback when there's neither a text track nor
a PGS one.

### Sidecar subtitle files (`find_sidecar_subtitle()`, an external file next to the input)

Second in the subtitle-discovery chain (text track -> CC608 -> **sidecar
file** -> PGS OCR -> VobSub OCR -> OpenSubtitles), tried when there's no
usable embedded text/CC608 track. `find_sidecar_subtitle()` looks for
`<stem><ext>` or `<stem>.<lang><ext>` next to the input (`ext` one of
`.srt`/`.vtt`/`.ass`/`.ssa`), ranked by language match against
`Config.sidecar_lang` (falling back to the chosen audio track's own
language) first, an untagged file second, extension preference
(`SIDECAR_SUB_EXTS` order) as the final tiebreak.

**Treated exactly like an OpenSubtitles download, not like an embedded
track** - this was a deliberate design choice, not an oversight: a sidecar
file sitting next to a recording is no more guaranteed to be a subtitle
*for this exact cut* than one downloaded from OpenSubtitles is (a sidecar
saved from a different rip/release of the same title is a completely
plausible real-world case). So rather than trusting its timestamps the way
an embedded/CC608 track's are trusted, a sidecar is resynced against the
transcript's own word timings with the exact same machinery OpenSubtitles
uses - `stage_sidecar_srt()` normalises `.ass`/`.ssa` to plain SRT text via
ffmpeg first (`.srt`/`.vtt` parse as-is, since `flag_language.parse_srt`'s
timestamp regex already accepts both decimal separators), then both
sources share `resync_external_srt()` (`flag_language.
resync_units_to_transcript()` + `write_srt_cues()` + the same
`_MIN_USABLE_SRT_CHARS` check) and the same two-tracks-muxed-in treatment
below - `SIDECAR_TRACK_SUFFIX` (" (Sidecar)") stands in for
`OPENSUBS_TRACK_SUFFIX` and is recognised by `is_own_output_track()` the
same way.

### OpenSubtitles fallback (`opensubtitles.py`, real-world subtitle sourcing for TV recordings)

Last in the subtitle-discovery chain (text track -> CC608 -> sidecar file ->
PGS OCR -> VobSub OCR -> **OpenSubtitles**), tried only when `chosen_subs` is
still `None` after everything above - the common case for a TV recording,
which almost never carries an embedded subtitle track of any kind. Needs an
API key (see "Setup" below); degrades gracefully (a warning, pipeline
continues without it) on a missing key, failed search, exhausted quota, or
network error, same as every other optional dependency in this project.

**Why this needed a fundamentally different algorithm from `backfill_from_srt`
above**: that function trusts an SRT's own timestamps to within `window`
(default 2.5s) of the transcript - true for an embedded/OCR'd track, which
was extracted from the exact same file. An OpenSubtitles download was
authored against a *different* release entirely - for TV-recorded content
specifically, one with commercial breaks and station edit cuts the
recording has and the subtitle's source didn't (or vice versa), on top of
whatever plain frame-rate drift already exists between any two releases.
Windowed matching against that isn't "off by a bit," it's arbitrarily wrong
in different places throughout the file. **The file itself is also not
clean input**: subtitles pulled from public sites routinely carry injected
ad/attribution cues ("Support us and become VIP...", "Sync and corrected by
...", a bare URL) mixed in with the real dialogue lines.

**`flag_language.resync_units_to_transcript()`** solves this by not trusting
the subtitle's timing *at all* - real timing is rebuilt from a whole-file
TEXT alignment against the ASR transcript instead of any assumption about a
shared clock:

1. `strip_ad_units()` drops cues matching a small pattern set (known
   subtitle-site domains, "support us", "sync ... by", "subtitles by",
   etc.) before anything else runs, so junk text can't false-anchor or
   pollute the alignment.
2. Every remaining cue's tokens are concatenated into one flat stream
   (remembering each token's owning cue), and diffed - ONE
   `difflib.SequenceMatcher` pass, `autojunk=False` - against the
   transcript's own flat word-token stream. This is the same technique
   `_locate_srt_word`/`backfill_from_srt` use per-cue in a small time
   window, just run once over the WHOLE file with no time window at all.
   Because both sequences are in speaking order, the LCS-style match
   naturally respects that order even where the same phrase recurs
   elsewhere in the file - a commercial break or a station's cut scene
   just becomes an unmatched stretch on one side, not a broken alignment.
3. Matching blocks of `>= min_anchor_run` (default 2) consecutive tokens
   become trusted anchors - a run of 1 is rejected since a single common
   word ("the") anchoring on coincidence is a real risk; requiring an
   actual short shared phrase cuts that sharply.
4. Each cue's new `(start, end)` is the min/max transcript-word time among
   its own tokens that landed inside an anchor. A cue with no anchored
   token at all is dropped UNLESS it's a single cue sandwiched directly
   between two anchored ones, in which case it's interpolated between
   them (very likely a real line the alignment just missed, or a lone ad
   cue splitting real dialogue) - a longer unmatched run is left dropped,
   since that reads as a genuine structural difference (an extra/missing
   scene, a commercial break) rather than a few individually-missed words,
   and guessing across it risks inventing wrong-context subtitle text.
5. A final monotonicity pass drops any cue a bad anchor placed out of
   order rather than let it corrupt the track.

The result is a real `.srt` with trustworthy timing (`write_srt_cues()`),
cached next to the source as `<name>.<lang>.opensubtitles.srt` (the RAW,
un-resynced download is cached separately as `<name>.<lang>.
opensubtitles.raw.srt` + a `.meta.json` of what search matched - both
reused on rerun like every other cache in this project, `force=cfg.
retranscribe`). Once resynced, it's fed through the **existing**,
unmodified `backfill_from_srt()` for word detection exactly like an
embedded track - by this point its timing is trustworthy, so no special
casing was needed there at all.

**Muxing in two tracks, not one**: every embedded/OCR'd subtitle source
already has its "original" passing through the container untouched (the
embedded track itself, or the source PGS/VobSub bitmap) - only the
"(Cleaned)" derivative is new. Neither an OpenSubtitles-sourced subtitle nor
a sidecar file exists as a track in the source file at all, so BOTH an
uncensored `"<lang> (OpenSubtitles)"`/`"<lang> (Sidecar)"` (non-default) and
a censored `"<lang> (OpenSubtitles) (Cleaned)"`/`"<lang> (Sidecar)
(Cleaned)"` (default) track have to be muxed in fresh. `remux()`'s
`extra_srt` parameter handles the first for either source; the second
reuses the pipeline's normal `clean_srt`/`chosen_subs` path unchanged
(both set those exactly like a PGS/VobSub OCR result does - see
`resync_external_srt()`). `OPENSUBS_TRACK_SUFFIX`/`SIDECAR_TRACK_SUFFIX`
are recognised by `is_own_output_track()` alongside `track_name_suffix`/
`dialog_track_suffix` so a rerun replaces both stale tracks instead of
piling up duplicates - the "(Cleaned)" one already ends in
`track_name_suffix` so that half was free, but the plain uncensored one
needed this added explicitly or it would have accumulated one new copy per
rerun.

**Quota-exceeded detection** (`OpenSubtitlesQuotaExceeded`, a subclass of
`OpenSubtitlesError`): `_request()` raises this specific type instead of the
generic error when an HTTP call fails with code 406/429 or the response body
mentions "quota" - a best-effort heuristic, not yet validated against a real
quota-exceeded response. `fetch_subtitle()` catches it separately and prints
a line containing the literal marker `OPENSUBTITLES_QUOTA_EXCEEDED` (to
stderr) so an external batch driver can grep a run's captured output for it
and stop attempting further downloads for the rest of that run rather than
burning time on calls doomed to fail the same way. See the personal
`_batch_movies_full.py` driver (in the `Profanity_Filter` shell, not this
repo - personal/hardcoded, same reasoning as `_batch_pe3.py`) for the
daily-run + quota-trickle consumer of this.

**Setup**: get a free API key at
[opensubtitles.com/en/consumers](https://www.opensubtitles.com/en/consumers)
("API Consumers" under account settings), then either
- set the `OPENSUBTITLES_API_KEY` environment variable, or
- drop it as the only line in a new `opensubtitles.key` file next to
  `opensubtitles.py` (gitignored - never committed).

Optional `OPENSUBTITLES_USERNAME`/`OPENSUBTITLES_PASSWORD` env vars log in
for a higher daily download quota; without them, downloads use the
anonymous quota tied to the API key alone (`opensubtitles.login()` - never
fatal if missing/wrong, just stays anonymous).

**Search matching is a filename heuristic** (`opensubtitles.guess_query()`
- strips common quality/edit tags, picks off `SxxExx` or a `(YYYY)`, uses
what's left as the title) and picks the top result by download count - good
enough to be useful, not guaranteed right. `--opensubtitles-query` overrides
the guessed title; `--opensubtitles-id` bypasses search entirely with a
file_id you found yourself on the site. Always check the printed match
line (`OpenSubtitles: fetched '<release>' (...) -> ...`) before trusting a
batch run's output.

**Known rough edges (POC, same status as the rest of this project's
subtitle sourcing)**: the whole-file `difflib` pass is real work on a
movie-length token stream (tens of thousands of tokens each side) - slow
but tractable for a tool that already budgets minutes for transcription/
stemming, not yet benchmarked against a very long source. Title-guessing
from a DVR-style filename is unvalidated against a real batch of TV
recordings; `--opensubtitles-query`/`--opensubtitles-id` exist specifically
because it will sometimes guess wrong. Not yet run end-to-end against a
real commercial-broken TV recording - validated so far only by code review
against the documented algorithm and by exercising `resync_units_to_
transcript()`/`is_ad_cue()` on synthetic inputs.

### VobSub OCR + bitmap censoring (DVD bitmap subtitles, `vobsub_ocr.py`)

The VobSub (DVD-era, `S_VOBSUB`) analogue of `pgs_ocr.py` above, same
two-stage image-in/image-out design (OCR for backfill + word-box caching,
then redact-in-place on the real spans) and same reason for existing: no
text/PGS subtitle to backfill or censor from, but there IS a VobSub track.
Tried in `clean.py`'s subtitle-discovery chain after sidecar/PGS have both
come up empty (text track -> CC608 -> sidecar -> PGS -> **VobSub**) - PGS
wins if a source somehow has both, being the newer, higher-resolution
format.

**Reuses pgs_ocr.py's format-agnostic pieces directly** rather than
duplicating them: `Cue`/`Placement` (a VobSub cue always has exactly one
placement - no PGS-style multi-window compositing), `ocr_cue` (OCR doesn't
care which format the pixels came from), `_ink_columns` and
`grow_word_box` (the redaction-box-growing algorithm, factored out of
`pgs_ocr.censor_pgs_track` into a shared function specifically so this
module could reuse it verbatim rather than re-deriving/copying it), and
`write_srt`/`_srt_ts`.

**Why this format needed real reverse-engineering, not just a spec read**:
VobSub predates PGS and is considerably fussier - each subtitle ("SPU",
Sub-Picture Unit) is wrapped in classic MPEG-2 Program Stream framing (a
14-byte pack header + a private-stream-1 PES header, repeating every 2048
bytes for an SPU spanning more than one "pack"), and its bitmap is
RLE-encoded as two independently-encoded INTERLACED fields (even/odd
scanlines) using a 4-bit (nibble), variable-length run code - quite
different from PGS's clean byte-aligned RLE. Rather than trust memory of
the format's byte layout the way PGS's (much simpler, well-documented)
format allowed, every piece of this was verified against REAL extracted
bytes from this project's own library before writing the decoder:

- The pack/PES framing byte offsets were found by scanning a real `.sub`
  file for repeated `00 00 01 BA` pack-start markers (confirmed: exactly
  every 2048 bytes) and manually walking one real SPU's header byte by
  byte to confirm SIZE/DCSQT/substream-ID placement.
- The SPU control-sequence command set (`SET_COLOR`/`SET_CONTR`/
  `SET_DAREA`/`SET_DSPXA`/`STA_DSP`/`STP_DSP`) and the nibble RLE escalation
  rule were validated by decoding a real subtitle image and reading back
  actual text.
- **The one genuinely surprising find, undocumented anywhere obvious**:
  `SET_CONTR`'s 4 nibbles give one alpha level per pixel value 0-3, but
  empirically pixel value V's alpha sits at nibble position `3 - V`, NOT
  position V. Confirmed by rendering a real subtitle both ways: the
  "as-documented" (position == pixel value) mapping produced a solid black
  rectangle (implying zero transparent pixels anywhere in a whole line of
  text - impossible); reversing it produced correct, readable text.
- The whole chain (real subtitle -> decode -> render -> OCR) was
  cross-checked against a movie that has BOTH a VobSub track and a real
  text subtitle track for the same dialogue (Crocodile Dundee (1986)) -
  removing any need to trust memory of what a movie's lines "should" say:
  `parse_vobsub`+`ocr_cue` on the VobSub track reproduced "Sue, don't
  misunderstand me, please." (and the next several lines) character-for-
  character against that movie's own SRT at the same timestamps, including
  cue END time (from the SPU's own `STP_DSP` delay, not a guess).
- The RLE ENCODER (`encode_bitmap`/`_encode_field`) and the pack/PES
  re-wrapping (`_wrap_spu_in_packs`) were validated with a full ROUND TRIP
  through the real tools, not just this module's own decoder: a rebuilt-
  but-unchanged SPU came back pixel-identical after
  `mkvmerge`-import-then-`mkvextract`-re-extract; a real pixel redaction
  (a blanked column range) survived that same round trip intact, confirmed
  by diffing before/after arrays, not eyeballing a render. One real bug
  surfaced by this: `mkvmerge` warned "Unsupported MPEG mpeg_version" on an
  all-zeroed dummy pack header (the SCR/mux_rate bytes, which don't matter
  for re-import since `.idx` timestamps are authoritative, still needed to
  look like a real MPEG-2 header for mkvmerge's own sanity check to pass) -
  fixed by reusing a real captured pack header's bytes as the template
  instead of zeros.

**Encoding simplicity over compactness**: `_emit_run_nibbles` always uses
the unambiguous 4-nibble (16-bit) form for every run, chunking anything
over 255 pixels into multiple codes - not maximally compact, but this
guarantees the decoder's 1/2/3-nibble escalation rules (each is a real,
easy-to-get-subtly-wrong threshold) can never misread output this module
itself produced, at the cost of a slightly larger `.sub`.

**Rewriting the .idx/.sub pair**: unlike `pgs_ocr._splice_sup` (which
copies untouched PGS segments as exact byte slices within one file),
`vobsub_ocr._write_vobsub` rewrites the WHOLE `.idx`+`.sub` pair fresh -
still copying an untouched entry's SPU bytes byte-identical from the
source (entries are laid out contiguously by `filepos`, so `[filepos[i],
filepos[i+1])` is exactly that entry's original byte range), but every
`filepos` value in the new `.idx` is recomputed from scratch rather than
trying to preserve the original pack-count per entry so offsets elsewhere
stay valid. Simpler and just as safe, since an `.idx`'s filepos values are
meaningless outside its own paired `.sub` anyway.

**Setup**: same as `pgs_ocr.py` - needs Tesseract OCR + `pytesseract` in
the shared venv; nothing extra. `Config.vobsub_ocr` (default on) toggles
it; `--no-vobsub-ocr`/`--vobsub-ocr-lang` on the CLI.

### Vocals-stem re-transcription (absolute last resort, no usable subtitle at all)

If embedded/sidecar/PGS/VobSub/OpenSubtitles *all* find nothing, the
transcript is this file's ONLY detection safety net - no `srt_backfill`
cross-check is possible at all. `Config.stem_retranscribe` (default on)
pays for a second, better-odds attempt specifically in this one case:
`build_vocals_stem` runs the chosen audio track through the same
`.venv-stem` audio-separator `build_instrumental_stem`/`mute_track` use, but
asking for the **Vocals** stem, not `Instrumental` - downmixed to plain
stereo first, since transcription downmixes to mono 16kHz internally
regardless, so preserving the source's real channel layout (the whole point
of `build_instrumental_stem`'s per-channel round trip) buys nothing here.
`ensure_vocals_transcript` feeds that isolated track through
`voice_to_text/transcribe.py` again, caching the result as its own
`"<name>.vocals.json"` sibling (never overwrites the primary `"<name>.json"`)
so a rerun doesn't re-stem/re-transcribe unless `--retranscribe`.

**Never transcribes the same audio twice.** `main()`'s primary transcript
(step 1) is computed lazily via a memoized `get_transcript()` closure,
deferred until the subtitle-discovery chain either needs it for a resync
(sidecar/OpenSubtitles) or runs out of methods entirely. That means whether
the original mix ever gets transcribed AT ALL is decided *before* any STT
call, purely from what's detectable without one (a track/file/OCR result
existing, or an OpenSubtitles fetch succeeding):
- **No candidate found anywhere** (`js` was never forced) - the common
  case for a file with no subtitle source. The original mix is skipped
  entirely; the vocals-stem transcript computed here just *becomes* `js` -
  one Voice_to_Text call total for this file, not two.
- **A candidate WAS found** (sidecar file existed, or OpenSubtitles had a
  match) but its resync came up too thin - this already forced one
  transcription of the original mix to attempt that resync, so a second,
  real re-transcription pass on the isolated vocals is unavoidable here
  (there was no way to know the resync would fail before trying it).
  `scan_vocals_transcript` scans the new transcript with the same
  wordlists, and `_dedupe_vocals_hits` merges in only what it catches that
  the first pass (plus any srt-backfill already merged) missed *entirely* -
  a same-word hit within `stem_retranscribe_min_gain_s` seconds (default
  1.0) of an existing hit is treated as the same occurrence, not a new
  catch, so a timing wobble between the two independent transcriptions
  can't double up a span.

**Also reuses the SAME separator pass for muting, when possible.** On a
`<=2` channel source track, `build_vocals_stem(..., also_instrumental=True)`
asks audio-separator for BOTH stems in one invocation (no `--single_stem` -
see `_invoke_separator`'s docstring for why that's not double the GPU cost:
the model derives one stem from the other internally regardless of which
one was requested). The Instrumental half is exactly what `mute_fill =
"stems"`'s whole-track path (`build_instrumental_stem`) would otherwise
separately re-stem later - `mute_track`'s `cached_whole_instrumental`
splices straight from it instead, skipping its own per-span-vs-whole-track
cost comparison entirely (the whole-track cost is already sunk, so reusing
it always wins). Only valid for `<=2` channels: a `>2ch` track needs
`build_instrumental_stem`'s per-channel round trip to preserve its real
layout, which a stereo downmix can't stand in for - those tracks still
re-stem separately for muting, no way around it.

Why scoped this narrowly rather than shipped as a transcription-wide
default: see `voice_to_text/AGENTS.md`'s "Reducing the Whisper miss rate" -
vocal isolation measurably recovers real misses (dialogue masked by music/
effects that Whisper's VAD never even hands to the decoder) but costs
roughly as much as `clean.py`'s own whole-track stemming pass (~80-90
minutes on a real 2+ hour 5.1 film), which isn't worth paying on every file
when `srt_backfill`/PGS-OCR/VobSub-OCR already catches a meaningful share of
this same class of miss for free whenever a real subtitle exists. It's
worth it specifically here, where nothing else is left. No-ops with a
warning (never a hard failure) when no stemmer is installed - same
degrade-gracefully posture as `mute_fill = "stems"`/`--method dialog`.
`--no-stem-retranscribe` on the CLI.

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

### `--stt-only` (pre-warming a transcript without cleaning) and the alignment-reuse guard

`ensure_transcript()` reuses `<name>.json` next to the input when present -
but only after `_transcript_is_aligned()` confirms `result["_meta"]["aligned"]`
is `true`. This matters because that file doesn't have to be one `clean.py`
itself produced: anything that drops a `<name>.json` next to a media file -
most notably a sibling sweep like `_batch_in_progress_video.py` (personal,
`Profanity_Filter` shell - see below) running `clean.py --stt-only` ahead of
time - is a candidate for reuse too. `transcribe.py`'s own `_meta["aligned"]`
reflects whether forced alignment actually **succeeded**, not merely whether
it was requested (fixed alongside this - previously it just echoed
`cfg.align`, so a run where the `try`/`except` around `whisperx.align()`
silently fell back to coarser segment-level timing still claimed `aligned:
true`). A `<name>.json` that fails this check (missing `_meta`, or
`aligned: false`) is treated exactly like a missing file - re-transcribed,
not blindly trusted - since word-level hit timing isn't safe to build spans
from without real forced alignment. `ensure_vocals_transcript()` guards its
own `<name>.vocals.json` reuse the same way, for consistency (lower practical
risk there - that file's only ever written by `clean.py` itself, always via
the same always-aligned transcribe.py call).

`--stt-only` (`cfg.stt_only`) runs the full pipeline through step 3
(transcript -> flag -> backfill) - with `cfg.opensubtitles` forced off
regardless of config/CLI, since paying for a download/quota on a file that
isn't being cleaned yet defeats the purpose - then writes `<name>.flags.json`
(hits/spans/`subtitle_backfill_source`) and returns **before** step 4
(remove) ever runs: no ffmpeg, no mkvmerge, no stemming, source never
touched. Rejected outright for `--method dialog` (no wordlist scan happens
there, so there's nothing to report). The report is deliberately NOT named
`<name>.bleeps.json` - that's the "fully processed, skip forever" sentinel
`_batch_movies_full.py`'s `discover_new()`-equivalent scanning keys off of; a
`--stt-only` pass finding a file isn't a real clean and must never look like
one to that logic (a file swept here, then later moved into a real library
and picked up by the daily movies sweep, must still get a genuine clean.py
run - the sweep only needs to see that its OWN sentinel, `<name>.flags.json`,
isn't there yet).

`--transcript-formats`/`--diarize-transcript` (`cfg.transcript_formats`/
`cfg.transcript_diarize`, forwarded to `ensure_transcript()`) default to
`"json"`/`False` - unchanged from always-on behaviour, since `clean.py`'s own
detection never needs more than the bare json or speaker labels. A sweep
building a generally-useful transcript library (not just feeding `clean.py`)
passes `all`/`True` to get the full Voice_to_Text output set (srt/vtt/txt/
tsv/speakers.txt/words.json) plus diarization cached alongside.

**`_batch_in_progress_video.py`** (personal, `Profanity_Filter` shell, not
this repo): sweeps `J:\Media\In Progress Video` with
`clean.py --stt-only --transcript-formats all --diarize-transcript`. No
OpenSubtitles means no quota/retry-queue machinery, and no Extras-folder
distinction either (that only ever existed to decide whether to skip an
OpenSubtitles call). "New" is keyed off `<name>.flags.json` absence, same
idea as the movies script's `<name>.bleeps.json` key.

Follows **`_no_narration_sweep.py`'s refresh/worker run model**, not
`_batch_movies_full.py`'s single-daily-lock one - a file added right after a
once-a-day run would otherwise sit untouched for up to 24h before its
transcript even started. "Run" means two separately-lockable things: a quick
REFRESH (rglob the library, recompute the pending queue wholesale from
"no `.flags.json` sidecar yet" - cheap, no ffprobe/clean.py involved) that
always happens, and a WORKER (actually invoking `clean.py --stt-only`, one
file at a time, possibly for many minutes) that only one instance may run at
once - a second instance just refreshes the queue and exits if a worker is
already active, exactly like the no-narration sweep's own
refresh-vs-worker split. Run via the "ProfanityFilter STT Sweep - In
Progress Video" Scheduled Task - hourly (`CalendarTrigger` + `Repetition`
`PT1H`/`P1D`, same pattern as "NoNarration Hourly Sweep"'s own trigger),
`MultipleInstances = Parallel` (not `IgnoreNew` - a refresh-only firing must
never be blocked behind a worker that could still be running from hours
ago).

### Coexisting with a "(No Narration)"/"(Wordless)" alt track

A file can carry a narration-free alt track from a prior `--method dialog`
run (this project's own, or a personal batch driver's project-specific
suffix override - e.g. `no_narration_batch.py`'s `" (No Narration)"`, see
that project's memory for the batch itself). A later plain `mute`/`bleep`/
`cut` run over the same file (e.g. the daily movie sweep) must not treat
that track as the thing to detect profanity in or promote to default -
`is_no_narration_track()` recognizes it by a case-insensitive name-substring
match ("no narration" / "wordless", via `NO_NARRATION_NAME_MARKERS`) rather
than `cfg.dialog_track_suffix`, so it's recognized regardless of which
config produced it (this repo's own default config vs. a caller's
project-specific override):

- **`choose_audio(tracks, "default")`** excludes a no-narration track from
  the candidate pool entirely before picking a source to clean, even if
  IT'S the one flagged default in the container (it always is, once
  created - see `no_narration_batch.py`) - falls back to a real narrated
  track.
- **`remux()`** leaves an existing no-narration track's default flag alone
  and adds the new `(Cleaned)` track as a non-default alt right after it in
  `--track-order` (`keep_no_narration_primary` in `remux()`), rather than
  clearing every original audio track's default flag and making the new one
  the primary track the way a normal run does. Only skipped when the call
  itself is adding another no-narration-style track (`audio_suffix`/
  `cfg.track_name_suffix` itself matches `NO_NARRATION_NAME_MARKERS` - e.g.
  a `--method dialog` run), so the no-narration batch's own runs are
  unaffected.

Net effect: run the daily movie sweep (or any plain `clean.py <file>`) over
a file the no-narration batch already touched, and the No Narration track
stays the primary/default audio, the freshly bleep-censored narrated track
lands right after it as the next alt, and the raw uncensored narrated track
(if kept) sits after that - matching listening-order intent (silent-by-
default background ambience, censored-narrated as the next thing to reach
for, raw original last).

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

### Center-channel-only removal (`--method dialog`, explicit opt-in only)

`dialog_remove_track()` always stems dialogue out of every channel by
default now - same "always-stem" posture as `mute_track()` above, and
removed for a related reason: there is no automatic decision anymore at
all, not even a smarter one. Movie 5.1/7.1 mixes can carry dialogue on the
center channel alone, and when that's true, muting only that channel beats
stemming - so this DID ship for a while as automatic per-file detection
(`detect_center_dominance`: RMS-compare the center channel against every
other channel during the source's own subtitle-derived dialogue spans,
batched past ffmpeg's ~100-term AVExpr limit, gated on a `center_margin_db`
threshold). It worked well on a source where dialogue is essentially 100%
center-isolated and nothing else worth keeping lives there (a nature
documentary's narration - see the git history for the validated synthetic-
and real-source testing that shipped it, and the WhisperX cross-check
below).

It broke on a real film mix: **The Emperor's New Groove**. Dialogue there is
only SOMETIMES center-isolated - songs, panned lines, and effects sharing
the channel - so a single aggregate dominance reading across the whole file
can't represent it, and gets it wrong in both directions: a "dominant"
verdict mutes legitimate non-dialogue center content site-unseen for the
ENTIRE runtime (the old code muted the whole file once confirmed, never
scoped to just the dialogue moments), while a "not dominant" verdict sends
an otherwise-mostly-clean source through the much slower stemmer
unnecessarily. Removed rather than patched with a smarter per-span
classifier: stemming is unconditionally correct regardless of how complex
the mix is, just slower, and every failure mode of the old auto-detection
was a false positive on "this file is safe to fast-path" - the actual
per-source knowledge needed to make that call safely lives with a human,
not a dB heuristic.

Center-only muting is still available, but only when a human explicitly
asks for it per file (`--center-mute` / `cfg.center_mute`) - the same
"opt-in, never inferred" posture as `--method bleep`. When requested:

1. The chosen track needs >2 channels, or this fails loudly (`SystemExit`) -
   no silent fallback to stemming, since the whole point of asking
   explicitly is that the caller already believes this source qualifies and
   a silent fallback would hide that belief being wrong.
2. `ffprobe` the track's `channel_layout` (e.g. `5.1(side)`, `7.1`) and look
   it up in `CHANNEL_LAYOUTS` (built from `ffmpeg -layouts`, the
   authoritative channel-order source). No recognised layout, or none with
   an `FC` (center) position - e.g. plain `stereo`, `quad`, `6.0(front)` -
   fails loudly the same way; don't guess an index from the channel count
   alone.
3. `channelsplit` the track into its named channels, mute *only* the center
   pad for the entire runtime, pass every other pad through with `anull`,
   then `join` them back with an explicit `map=i.0-<ChannelName>` (so the
   container keeps its real layout tag) - channel count trivially
   preserved. No dominance test, no margin: asking explicitly means this
   step is trusted outright.

Validated (back when this ran automatically) with synthetic 5.1 WAVs
(`ffmpeg -f lavfi ... amerge ... aformat=channel_layouts=5.1`) plus real
multichannel sources: center-dominant case -> non-center channels measured
**bit-identical** (via `astats`) inside vs. outside the muted span, center
channel drops ~64 dB (silence) only inside it. `build_mute_filter` (the
actual mute step) is unchanged by any of this - still the same channel-split/
mute-center/rejoin graph, still used by both the explicit `--center-mute`
path and `_whisperx_check.py`'s own validation muting.

`subtitle_dialogue_spans`/`_pick_dialogue_subs_track` (deriving dialogue-cue
timing from a subtitle track, merging nearby cues) also survive removal of
the detection - `clean.py` itself no longer calls them for any automatic
decision, but `_whisperx_check.py` still uses them to pick real dialogue
windows to validate an explicit `--center-mute` request against.

#### WhisperX validation: ground truth instead of a dB proxy (`_whisperx_check.py`)

A dB-margin test is a proxy for "is there residual narration" - it can't
actually tell whether what leaks through is intelligible, and the whole
class of problem the aggregate test had (one number can't represent a mixed
file) is exactly what made it unsafe as an automatic decision in the first
place. `_whisperx_check.py` (standalone, not merged into `clean.py`) asks
the real question directly: pick the longest subtitle-confirmed dialogue
spans, run WhisperX (via voice_to_text) on both the real audio and a
center-channel-muted version of the same spans (the exact `build_mute_filter`
graph the explicit `--center-mute` path would use), and compare. A clean
mute leaves only short generic hallucinated phrases ("Thank you.", "Oh,
God.") with near-zero word overlap against the real transcript (which reads
as actual coherent, on-topic narration - "Columbus crabs are thriving...",
not word salad); real bleed-through shows up as an actual matching sentence
fragment. `whisperx_validate_center_mute()` requires EVERY tested window
(default 3, the longest merged spans) to score under `overlap_threshold`
(0.2) to pass.

Real-world numbers from when this validator was used to sanity-check the
old automatic dB-margin test on a real episodic source: same-source margins
of ~4.7-5.2 dB (DTS 5.1 / AC3 5.1) that the 6 dB default would have rejected
outright still passed WhisperX validation cleanly across every tested
window on all 10 files in the batch - the dB proxy was being needlessly
conservative on a source that really was clean. That result is now the
justification for making `--center-mute` opt-in-with-validation rather than
opt-in-blind: `_batch_pe3.py` is the current example of the intended
workflow - WhisperX-validate each file, pass `--center-mute` only when it
passes, let `clean.py` stem everything else. One tested window had overlap
0.171, close to the 0.2 cutoff, with leaked words reading as a real
narration fragment rather than hallucination - still passed since every
window has to fail to reject the whole file, but it's the closest call
seen; worth a listen-through if prose accuracy matters more than a
documentary M&E track for a given source.

Costs a few short WhisperX transcriptions per file (real, but much cheaper
than a stemming pass) and needs voice_to_text/WhisperX, which
`dialog_remove_track` doesn't otherwise require - that's why it stays a
separate script rather than a `clean.py` flag. The pattern (pick real
dialogue spans, transcribe muted + unmuted, compare) generalises past
center-channel muting to validating any dialogue-removal method, including
the stemmer's own output, if that's ever worth doing.

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
- `--center-mute` (`--method dialog` only) has no automatic detection or
  validation of its own - the caller is trusted to already know the source's
  dialogue is genuinely center-isolated. `_whisperx_check.py` is the way to
  actually check that belief instead of assuming it (see "Center-channel-
  only removal" above).
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
