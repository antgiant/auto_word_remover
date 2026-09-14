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
| `out/` | cleaned files + `.bleeps.json` sidecars land here; source is never touched |

No venv of its own — `clean.ps1` / callers use `..\voice_to_text\.venv\Scripts\python.exe`
(Python 3.11, already has everything). `flag_language.py` itself is pure stdlib.

**GPU lock**: neither `clean.py` nor `_whisperx_check.py` touches the GPU lock
directly — both invoke `voice_to_text\transcribe.py` as a subprocess for any
WhisperX work, and `transcribe.py` itself holds `../gpu_lock` (if present) for
the duration of that call. mkvmerge/ffmpeg/flag_language steps here are
CPU-only and never wait on it. See `../gpu_lock/AGENTS.md`.

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
     otherwise-untouched original; the whole track is stemmed once instead
     above `stem_whole_track_words` flagged words (fixed cost regardless of
     span count). No stemmer installed -> falls back to `volume=0` silence.
     See "Always-stem muting" below.
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

Output: `out/<name> (Cleaned).mkv` (or, for `cut`, the source's own extension)
+ `out/<name> (Cleaned).bleeps.json` (name is a holdover from the POC; the
sidecar covers whichever method ran - `subtitles` field records cues/words
masked, null for `cut`).

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

Two paths, picked by `stem_whole_track_words` (default 30):

- **Per-span** (`splice_stemmed_spans`, at or below the threshold): each
  flagged span (plus ~2s of context for the separator to work with) is
  extracted, stemmed, trimmed back to the exact span, and spliced into the
  otherwise-untouched original via ffmpeg's concat demuxer. Cheap for a
  handful of spans; the separator's model-load overhead is paid once per span.
- **Whole-track** (`splice_whole_track_stem`, above the threshold): the
  entire track is stemmed once (`build_instrumental_stem` - one fixed cost
  regardless of span count), then the flagged spans are sliced out of that
  and spliced against the original the same way. Cheaper once per-span
  overhead adds up on a heavily-flagged file. `stem_whole_track_words` is a
  rough word-count proxy for cost, not a real time estimate - it ignores the
  source's own runtime, so a long movie's real breakeven point is higher than
  a short one's; tune per-library.

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
- `stem_whole_track_words` (default 30) is a rough word-count proxy for
  per-span vs. whole-track stemming cost, not measured against real runtime -
  tune per-library if the breakeven point feels wrong (see "Always-stem
  muting" above).
- `center_margin_db` (6 dB, `--method dialog` only now) was chosen from a synthetic test, not a corpus of
  real mixes - loud action/music scenes with softer dialogue may need a lower
  margin, tune per source with `--center-margin-db`.

## External tools

- `ffmpeg` / `ffprobe`: must be on PATH.
- `mkvmerge`: bundled at `bin\mkvtoolnix\`. To update: download the portable
  `.7z` from mkvtoolnix.download, `bin\7zr.exe x mkvtoolnix.7z -obin`, delete the
  old `bin\mkvtoolnix`.
