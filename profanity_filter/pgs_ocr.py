"""pgs_ocr.py - decode a PGS ("Blu-ray") bitmap subtitle stream, OCR it, and
censor flagged words DIRECTLY IN THE BITMAP - producing a new .sup that's
still an image-based PGS track, not a converted text track.

Blu-ray subtitles (codec S_HDMV/PGS) are bitmap images, not text - there is
nothing for flag_language.py or clean.py's subtitle-censoring pass to read
directly, and clean.py's choose_subs() intentionally excludes them for that
reason. This module closes that gap in two stages:

1. analyze_pgs_track() - parse the raw .sup segment stream mkvextract
   produces for a PGS track (Presentation Composition / Window Definition /
   Palette Definition / Object Definition segments - format per
   https://blog.thescorpius.com/index.php/2017/07/15/presentation-graphic-stream-sup-files-bluray-subtitle-format/),
   decode each subtitle image's RLE-compressed indexed bitmap, and OCR it
   (word-level boxes, via Tesseract) - caching the result (a plain .srt for
   flag_language's srt_backfill, and a word-box .json for stage 2) next to
   the source, since OCR-ing a full film is slow.

2. censor_pgs_track() - once clean.py knows the final flagged spans (audio
   + transcript + the stage-1 SRT backfill together), locate every flagged
   word's own bounding box (from the cached word-box json) in cues that
   overlap a span, redact just those pixels (set them to a transparent
   palette index) in the ORIGINAL bitmap, re-encode the RLE, and splice the
   new Object Definition Segment(s) back into a copy of the original .sup -
   every other segment (PCS/WDS/PDS/other objects) is copied through
   byte-for-byte unchanged. The result is muxed in by clean.py as a second,
   still-image-based "(Cleaned)" PGS track - not a text SubRip conversion.

Cue timing is derived the accurate way, not estimated: a Presentation
Composition Segment (PCS) with zero composition objects is the standard PGS
"clear the screen" marker, so a cue's end time is the PTS of the next
display set - whether that's a genuine clear or the next subtitle appearing
- rather than a fixed guess like "+4s".

Rendering trick: rather than a full YCbCr->RGB palette conversion, each
pixel's grayscale value for OCR is just 255-alpha from its palette entry -
text (opaque, high alpha) comes out dark, background (transparent, alpha 0)
comes out white, regardless of the subtitle's actual on-screen color. Gives
Tesseract exactly the high-contrast input it wants with far less code, and
sidesteps color entirely since redaction never needs to preserve it (the
masked pixels become fully transparent, not a same-color box).

OCR accuracy note: this is inherently approximate - misreads happen,
especially on stylised fonts, italics, or rapid/overlapping cues. A missed
word isn't redacted; treat this as "much better than nothing," not as
authoritative as hand-authored censoring.
"""
from __future__ import annotations

import json
import struct
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

# ---------------------------------------------------------------------------
# .sup segment parsing
# ---------------------------------------------------------------------------
SEG_PDS = 0x14  # Palette Definition Segment
SEG_ODS = 0x15  # Object Definition Segment (the actual bitmap, RLE-encoded)
SEG_PCS = 0x16  # Presentation Composition Segment (what's shown, where, when)
SEG_WDS = 0x17  # Window Definition Segment
SEG_END = 0x80  # End of Display Set (no extra data needed from this one)


@dataclass
class _Segment:
    pts: float  # seconds - the segment header's PTS is a 90kHz clock
    seg_type: int
    payload: bytes


def _read_segments(data: bytes, with_offsets: bool = False):
    """Every segment in a .sup byte string. Each is: 2 bytes 'PG' magic, 4
    bytes PTS (90kHz), 4 bytes DTS (unused here), 1 byte type, 2 bytes
    payload size, then the payload. With `with_offsets`, also yields the
    (start, end) byte range of the WHOLE raw segment (header+payload) in
    `data`, so censor_pgs_track can copy unmodified segments through as
    exact byte slices instead of re-serialising them."""
    i = 0
    n = len(data)
    while i + 13 <= n:
        if data[i:i + 2] != b"PG":
            nxt = data.find(b"PG", i + 1)  # resync past any corruption/junk
            if nxt == -1:
                break
            i = nxt
            continue
        pts90 = struct.unpack(">I", data[i + 2:i + 6])[0]
        seg_type = data[i + 10]
        size = struct.unpack(">H", data[i + 11:i + 13])[0]
        start = i + 13
        end = start + size
        if end > n:
            break
        seg = _Segment(pts90 / 90000.0, seg_type, data[start:end])
        yield (seg, i, end) if with_offsets else seg
        i = end


def _parse_pcs(payload: bytes) -> list[tuple[int, int, int, int]]:
    """[(object_id, window_id, x, y), ...] for this composition. An empty
    list is the standard "hide/clear" display set."""
    n_objs = payload[10]
    objs = []
    off = 11
    for _ in range(n_objs):
        obj_id, window_id, cropped_flag = struct.unpack(">HBB", payload[off:off + 4])
        off += 4
        x, y = struct.unpack(">HH", payload[off:off + 4])
        off += 4
        if cropped_flag & 0x80:
            off += 8  # crop x,y,w,h - not needed, we render the object's own bitmap whole
        objs.append((obj_id, window_id, x, y))
    return objs


def _parse_pds(payload: bytes) -> dict[int, int]:
    """{palette index: alpha (0-255)} - Y/Cr/Cb are decoded away entirely
    (see the module docstring: neither OCR nor redaction needs color)."""
    entries: dict[int, int] = {}
    off = 2
    while off + 5 <= len(payload):
        idx = payload[off]
        a = payload[off + 4]
        entries[idx] = a
        off += 5
    return entries


def _decode_rle(width: int, height: int, data: bytes) -> bytes:
    """PGS's run-length code -> a flat width*height buffer of palette
    indices, one byte per pixel. Per-row codes: a nonzero byte is one pixel
    of that color; a 0x00 byte starts either an end-of-line marker (next
    byte also 0x00) or a run (1- or 2-byte length, transparent or a given
    color - see the four `flag` cases below)."""
    out = bytearray(width * height)
    pos = 0
    i = 0
    n = len(data)
    row_start = 0
    while i < n and pos < len(out):
        b0 = data[i]
        i += 1
        if b0 != 0:
            out[pos] = b0
            pos += 1
            continue
        if i >= n:
            break
        b1 = data[i]
        i += 1
        if b1 == 0:
            row_start += width
            pos = row_start
            continue
        flag = b1 & 0xC0
        length = b1 & 0x3F
        if flag == 0x00:
            color, run = 0, length
        elif flag == 0x40:
            if i >= n:
                break
            run = (length << 8) | data[i]
            i += 1
            color = 0
        elif flag == 0x80:
            if i >= n:
                break
            color = data[i]
            i += 1
            run = length
        else:  # 0xC0
            if i + 1 >= n:
                break
            run = (length << 8) | data[i]
            i += 1
            color = data[i]
            i += 1
        end_pos = min(pos + run, len(out))
        if end_pos > pos:
            out[pos:end_pos] = bytes([color]) * (end_pos - pos)
        pos = end_pos
    return bytes(out)


def _encode_rle(width: int, height: int, pixels: bytes) -> bytes:
    """The inverse of _decode_rle: a flat width*height index buffer -> PGS
    RLE bytes. Not trying to be maximally compact (e.g. always using the
    escape+run form rather than the compact single-raw-byte form for an
    isolated non-zero pixel) - just correct and simple, since this only
    needs to round-trip through _decode_rle, not match the original
    encoder's exact byte choices."""
    out = bytearray()
    for row in range(height):
        base = row * width
        col = 0
        while col < width:
            color = pixels[base + col]
            run = 1
            while col + run < width and pixels[base + col + run] == color:
                run += 1
            if color == 0:
                _emit_run(out, run, None)
            else:
                _emit_run(out, run, color)
            col += run
        out += b"\x00\x00"  # end of line
    return bytes(out)


def _emit_run(out: bytearray, n: int, color: int | None) -> None:
    max_len = 0x3FFF  # always use the 2-byte length form - simplest, always valid
    while n > 0:
        chunk = min(n, max_len)
        if color is None:
            out += bytes([0x00, 0x40 | (chunk >> 8), chunk & 0xFF])
        else:
            out += bytes([0x00, 0xC0 | (chunk >> 8), chunk & 0xFF, color])
        n -= chunk


@dataclass
class _ObjAccum:
    width: int = 0
    height: int = 0
    data: bytearray = field(default_factory=bytearray)
    complete: bool = False


def _accum_ods(payload: bytes, pending: dict[int, _ObjAccum]) -> None:
    """ODS payload can be split across several segments for a large image
    (first-in-sequence carries the length/width/height header; later ones
    are pure continuation data) - accumulate by object id until the
    last-in-sequence flag."""
    obj_id = struct.unpack(">H", payload[0:2])[0]
    last_in_seq = payload[3]
    if last_in_seq & 0x40:  # first (or first+last) segment
        width, height = struct.unpack(">HH", payload[7:11])
        acc = _ObjAccum(width, height)
        acc.data.extend(payload[11:])
        pending[obj_id] = acc
    else:
        acc = pending.get(obj_id)
        if acc is None:
            return
        acc.data.extend(payload)
    if last_in_seq & 0x80:
        pending[obj_id].complete = True


@dataclass
class DisplaySet:
    pts: float
    objects: list  # [(object_id, window_id, x, y), ...] - empty means "clear"
    palette: dict  # {index: alpha}
    images: dict  # {object_id: (width, height, indexed_bytes)}


def parse_display_sets(sup_path: Path) -> list[DisplaySet]:
    """One DisplaySet per Presentation Composition Segment in the .sup, in
    order, each carrying its composition objects' positions, the palette
    (alpha only) in effect, and the RLE-decoded bitmap for any object
    completed by the ODS segment(s) that followed it (a display set's
    segments always appear PCS, [WDS], [PDS], [ODS...], END before the next
    PCS - so finalizing on "next PCS seen" is exactly right). Returned as a
    list (not a generator) - censor_pgs_track needs to index into it by
    display-set position, matching the second, offset-tracking pass over
    the same bytes."""
    data = sup_path.read_bytes()
    result: list[DisplaySet] = []
    pending_objs: dict[int, _ObjAccum] = {}
    cur_palette: dict[int, int] = {}
    cur_pcs: tuple[float, list] | None = None

    def finalize() -> DisplaySet:
        pts, objs = cur_pcs
        images = {
            oid: (acc.width, acc.height, _decode_rle(acc.width, acc.height, bytes(acc.data)))
            for oid, acc in pending_objs.items() if acc.complete and acc.width and acc.height
        }
        return DisplaySet(pts=pts, objects=objs, palette=dict(cur_palette), images=images)

    for seg in _read_segments(data):
        if seg.seg_type == SEG_PCS:
            if cur_pcs is not None:
                result.append(finalize())
            cur_pcs = (seg.pts, _parse_pcs(seg.payload))
            pending_objs = {}
        elif seg.seg_type == SEG_WDS:
            pass  # window geometry isn't needed - see module docstring
        elif seg.seg_type == SEG_PDS:
            cur_palette = _parse_pds(seg.payload)
        elif seg.seg_type == SEG_ODS:
            _accum_ods(seg.payload, pending_objs)
        elif seg.seg_type == SEG_END:
            pass

    if cur_pcs is not None:
        result.append(finalize())
    return result


# ---------------------------------------------------------------------------
# render: composite each display set's object bitmap(s) into one grayscale
# image, black text on a white background (regardless of the subtitle's
# actual on-screen color) - built straight from each palette entry's alpha.
# ---------------------------------------------------------------------------
@dataclass
class Placement:
    obj_id: int
    x: int  # absolute video-frame position (matches DisplaySet.objects' x/y)
    y: int
    w: int
    h: int


@dataclass
class Cue:
    ds_index: int  # index into the display_sets list this cue's image came from
    start: float
    end: float
    canvas: np.ndarray  # grayscale, unscaled, cropped to the union bbox of `placements`
    min_x: int  # canvas[0,0] is this absolute video-frame position
    min_y: int
    placements: list  # list[Placement], in absolute video-frame coordinates


def render_cues(display_sets: list[DisplaySet]) -> list[Cue]:
    """[Cue, ...] - one per span the subtitle was actually on screen. A
    display set with no objects (or none of its objects have completed
    image data) ends the previous cue rather than starting a new one - see
    the module docstring on why this is more accurate than guessing a fixed
    display duration."""
    cues: list[Cue] = []
    active: Cue | None = None

    for ds_index, ds in enumerate(display_sets):
        placed = []
        for obj_id, _window_id, x, y in ds.objects:
            img = ds.images.get(obj_id)
            if img is not None:
                w, h, idx_bytes = img
                placed.append((obj_id, x, y, w, h, idx_bytes))

        if not placed:
            if active is not None:
                active.end = ds.pts
                cues.append(active)
                active = None
            continue

        alpha_lut = np.zeros(256, dtype=np.int16)
        for idx, a in ds.palette.items():
            alpha_lut[idx] = a

        min_x = min(p[1] for p in placed)
        min_y = min(p[2] for p in placed)
        max_x = max(p[1] + p[3] for p in placed)
        max_y = max(p[2] + p[4] for p in placed)
        canvas = np.full((max_y - min_y, max_x - min_x), 255, dtype=np.uint8)
        placements = []

        for obj_id, x, y, w, h, idx_bytes in placed:
            idx_arr = np.frombuffer(idx_bytes, dtype=np.uint8).reshape(h, w)
            alpha = alpha_lut[idx_arr]
            gray = (255 - alpha).astype(np.uint8)
            oy, ox = y - min_y, x - min_x
            region = canvas[oy:oy + h, ox:ox + w]
            mask = alpha > 0
            region[mask] = np.minimum(region[mask], gray[mask])
            placements.append(Placement(obj_id, x, y, w, h))

        if active is not None:
            active.end = ds.pts
            cues.append(active)
        active = Cue(ds_index, ds.pts, ds.pts, canvas, min_x, min_y, placements)

    if active is not None:
        cues.append(active)
    return cues


# ---------------------------------------------------------------------------
# OCR: one Tesseract pass per cue gets BOTH the plain text (for the cached
# .srt / srt_backfill) and per-word boxes (for later redaction) - avoids
# OCR-ing every cue twice.
# ---------------------------------------------------------------------------
def ocr_cue(cue: Cue, lang: str = "eng", psm: int = 6,
            tesseract_cmd: str | None = None) -> tuple[str, list[dict]]:
    """(full_text, [{"text","left","top","width","height","line"}, ...]) -
    box coordinates are already rescaled back to `cue.canvas`'s own
    (unscaled) pixel space, i.e. directly addable to cue.min_x/min_y to get
    absolute video-frame coordinates. "line" is a 0-based index, stable
    within this cue only, grouping words that share a text line - Tesseract
    boxes on this kind of stylised/italic subtitle font were found to
    undershoot a glyph's true left edge by a lot (confirmed ~29px on a
    ~50px-tall word - not a small-margin problem), so censor_pgs_track()
    uses gaps BETWEEN same-line neighbors to size a redaction, not each
    word's own box edges - "line" is what makes that grouping possible."""
    import pytesseract
    from pytesseract import Output
    from PIL import Image

    if tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd

    img = Image.fromarray(cue.canvas, mode="L")
    scale = 1
    if img.width < 800:  # small bitmaps OCR noticeably better upscaled
        scale = 2
        img = img.resize((img.width * scale, img.height * scale), Image.LANCZOS)

    data = pytesseract.image_to_data(img, lang=lang, config=f"--psm {psm}", output_type=Output.DICT)
    words: list[dict] = []
    lines: dict[tuple, list[str]] = {}
    line_ids: dict[tuple, int] = {}
    for i, text in enumerate(data["text"]):
        text = text.strip()
        if not text:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        if key not in line_ids:
            line_ids[key] = len(line_ids)
        words.append({
            "text": text, "line": line_ids[key],
            "left": data["left"][i] / scale, "top": data["top"][i] / scale,
            "width": data["width"][i] / scale, "height": data["height"][i] / scale,
        })
        lines.setdefault(key, []).append(text)

    full_text = "\n".join(" ".join(v) for v in lines.values()).strip()
    return full_text, words


def _srt_ts(t: float) -> str:
    t = max(0.0, t)
    total_ms = int(round(t * 1000))
    h, rem = divmod(total_ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(cues: list[tuple[float, float, str]], out_path: Path) -> None:
    lines = []
    for i, (start, end, text) in enumerate(cues, 1):
        lines.append(str(i))
        lines.append(f"{_srt_ts(start)} --> {_srt_ts(end)}")
        lines.append(text)
        lines.append("")
    out_path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# stage 1: extract + OCR + cache
# ---------------------------------------------------------------------------
def analyze_pgs_track(mkvextract: str, media: Path, track_id: int, cache_base: Path,
                       lang: str = "eng", tesseract_cmd: str | None = None,
                       min_len_s: float = 0.08, force: bool = False) -> dict:
    """Extract the PGS track to `<cache_base>.sup` (kept, not scratch - stage
    2 re-reads it), OCR every cue once, and cache the result as
    `<cache_base>.srt` (plain text, for flag_language.backfill_from_srt) and
    `<cache_base>.words.json` (per-cue word boxes, for censor_pgs_track).
    Reuses all three cache files on a rerun unless `force`. Returns
    {sup_path, srt_path, json_path, display_sets, image_cues, text_cues}."""
    sup_path = cache_base.with_suffix(cache_base.suffix + ".sup")
    srt_path = cache_base.with_suffix(cache_base.suffix + ".srt")
    json_path = cache_base.with_suffix(cache_base.suffix + ".words.json")

    if not force and sup_path.is_file() and srt_path.is_file() and json_path.is_file():
        cached = json.loads(json_path.read_text(encoding="utf-8"))
        return {"sup_path": sup_path, "srt_path": srt_path, "json_path": json_path,
               "display_sets": cached["display_sets"], "image_cues": len(cached["cues"]),
               "text_cues": sum(1 for c in cached["cues"] if c["text"]), "cached": True}

    subprocess.run([mkvextract, "tracks", str(media), f"{track_id}:{sup_path}"], check=True)
    display_sets = parse_display_sets(sup_path)
    cues = render_cues(display_sets)

    srt_rows: list[tuple[float, float, str]] = []
    json_rows: list[dict] = []
    for cue in cues:
        if cue.end - cue.start < min_len_s:
            continue
        text, words = ocr_cue(cue, lang=lang, tesseract_cmd=tesseract_cmd)
        json_rows.append({"ds_index": cue.ds_index, "start": cue.start, "end": cue.end,
                          "text": text, "words": words})
        if text:
            srt_rows.append((cue.start, cue.end, text))

    write_srt(srt_rows, srt_path)
    json_path.write_text(json.dumps({"display_sets": len(display_sets), "cues": json_rows}),
                         encoding="utf-8")
    return {"sup_path": sup_path, "srt_path": srt_path, "json_path": json_path,
           "display_sets": len(display_sets), "image_cues": len(cues),
           "text_cues": len(srt_rows), "cached": False}


# ---------------------------------------------------------------------------
# stage 2: redact + splice
# ---------------------------------------------------------------------------
_MAX_ODS_PAYLOAD = 0xFFF0  # stay safely under the 2-byte segment-size field's 0xFFFF cap


def _build_segment(pts: float, seg_type: int, payload: bytes) -> bytes:
    pts90 = int(round(pts * 90000)) & 0xFFFFFFFF
    return b"PG" + struct.pack(">IIB", pts90, 0, seg_type) + struct.pack(">H", len(payload)) + payload


def _build_ods_segments(pts: float, obj_id: int, w: int, h: int, rle: bytes) -> bytes:
    """One or more ODS segments (fragmented if `rle` is too big for one) -
    the redacted-image replacement for whatever ODS segment(s) originally
    carried this object in this display set."""
    header_extra = struct.pack(">HH", w, h)  # 4 bytes, first fragment only
    data_length = len(rle) + 4  # RLE bytes + the w/h header, per the PGS spec
    first_avail = _MAX_ODS_PAYLOAD - 2 - 1 - 1 - 3 - 4  # obj_id + ver + flags + datalen + wh

    if len(rle) <= first_avail:
        payload = (struct.pack(">HBB", obj_id, 0, 0xC0) + data_length.to_bytes(3, "big")
                  + header_extra + rle)
        return _build_segment(pts, SEG_ODS, payload)

    out = bytearray()
    first_chunk, rest = rle[:first_avail], rle[first_avail:]
    payload = (struct.pack(">HBB", obj_id, 0, 0x40) + data_length.to_bytes(3, "big")
              + header_extra + first_chunk)
    out += _build_segment(pts, SEG_ODS, payload)
    cont_avail = _MAX_ODS_PAYLOAD - 2 - 1 - 1
    while rest:
        chunk, rest = rest[:cont_avail], rest[cont_avail:]
        payload = struct.pack(">HBB", obj_id, 0, 0x80 if not rest else 0x00) + chunk
        out += _build_segment(pts, SEG_ODS, payload)
    return bytes(out)


def _locate_placement(cue_ds_index: int, placements_by_ds: dict, left: float, top: float,
                       width: float, height: float, min_x: int, min_y: int):
    """Which Placement (if any) a word's canvas-local box falls inside,
    resolved by the box's center point, in absolute video-frame
    coordinates - and the word's position local to THAT object's own
    bitmap. None if no placement's rectangle contains the center (shouldn't
    normally happen; a straddling word is a rare edge case we skip rather
    than risk redacting the wrong pixels)."""
    abs_cx = min_x + left + width / 2
    abs_cy = min_y + top + height / 2
    for p in placements_by_ds.get(cue_ds_index, []):
        if p.x <= abs_cx < p.x + p.w and p.y <= abs_cy < p.y + p.h:
            return p, int(round(min_x + left - p.x)), int(round(min_y + top - p.y))
    return None, 0, 0


def _ink_columns(idx_bytes: bytes, obj_w: int, obj_h: int, palette: dict, y0: int, y1: int) -> np.ndarray:
    """bool[obj_w] - True for a column with at least one non-fully-
    transparent pixel within rows [y0, y1). Used to grow a redaction box out
    to a glyph's REAL edges from real pixel data, instead of trusting an
    OCR-reported box edge directly - see censor_pgs_track."""
    arr = np.frombuffer(idx_bytes, dtype=np.uint8).reshape(obj_h, obj_w)
    alpha_lut = np.zeros(256, dtype=np.int16)
    for idx, a in palette.items():
        alpha_lut[idx] = a
    band = arr[max(0, y0):min(obj_h, y1), :]
    if band.size == 0:
        return np.zeros(obj_w, dtype=bool)
    return (alpha_lut[band] > 0).any(axis=0)


def grow_word_box(w: dict, line_words: list, i: int, cue_min_x: float, cue_min_y: float,
                   placement_x: int, placement_y: int, obj_w: int, obj_h: int,
                   ink_columns_fn, mask_pad_px: int = 6):
    """(left, top, width, height) LOCAL (object-relative) box to redact for
    word `w` (index `i` within `line_words`, already sorted left-to-right)
    - or None if the caps collapse to nothing. Format-agnostic: shared by
    pgs_ocr.censor_pgs_track and vobsub_ocr's equivalent, since the growing
    algorithm doesn't care which bitmap format the ink came from - only
    `ink_columns_fn(y0, y1) -> bool[obj_w]` (True per column with any
    non-transparent pixel in that row band) does.

    Two things fixed together after Tesseract's own per-word box was found
    to undershoot a real glyph's true edge by ~29px on a ~50px-tall word
    (this project's original PGS test source, a stylised italic font - not
    a small-margin problem a fixed/proportional pad can paper over):
    - Growth is CAPPED by the same-line neighbors' CENTERS (not their box
      edges, and not an edge-to-edge midpoint - both were tried first and
      still cut off real ink; a line-edge word with no neighbor on that
      side falls back to a proportional pad instead).
    - Within that cap, the box grows pixel-by-pixel outward from the OCR
      box's own center through the REAL bitmap until it hits an actual
      transparent gap - real pixel data, not trusted OCR geometry."""
    ly = int(round(cue_min_y + w["top"] - placement_y))
    vpad = max(mask_pad_px, round(0.12 * w["height"]))
    fallback_pad = max(mask_pad_px, round(0.3 * w["height"]))
    if i > 0:
        prev = line_words[i - 1]
        left_bound = prev["left"] + prev["width"] / 2
    else:
        left_bound = w["left"] - fallback_pad
    if i + 1 < len(line_words):
        nxt = line_words[i + 1]
        right_bound = nxt["left"] + nxt["width"] / 2
    else:
        right_bound = w["left"] + w["width"] + fallback_pad

    left_cap = max(0, int(round(cue_min_x + left_bound - placement_x)))
    right_cap = min(obj_w - 1, int(round(cue_min_x + right_bound - placement_x)) - 1)
    y0 = max(0, ly - vpad)
    y1 = min(obj_h, ly + int(round(w["height"])) + vpad)
    if right_cap <= left_cap:
        return None

    ink = ink_columns_fn(y0, y1)
    center = max(left_cap, min(right_cap,
                 int(round(cue_min_x + w["left"] + w["width"] / 2 - placement_x))))
    if not ink[center]:
        # OCR's box missed the glyph entirely at its own center (rare, but
        # seen on other fonts) - search a small radius for the nearest ink
        # pixel instead of silently redacting nothing.
        radius = max(4, int(round(w["width"] / 2)))
        found = next((c for d in range(1, radius + 1) for c in (center - d, center + d)
                     if left_cap <= c <= right_cap and ink[c]), None)
        center = found if found is not None else center
    l = r = center
    while l - 1 >= left_cap and ink[l - 1]:
        l -= 1
    while r + 1 <= right_cap and ink[r + 1]:
        r += 1
    return (max(0, l - 1), y0, max(1, r - l + 1) + 2, y1 - y0)


def censor_pgs_track(sup_path: Path, json_path: Path, spans, matchers: dict,
                      out_sup_path: Path, subs_pad: float = 0.15,
                      mask_pad_px: int = 6) -> dict:
    """Redact every wordlist-matching word in a cue that overlaps a flagged
    span directly in the PGS bitmap, and write the result as a new .sup -
    still image-based, everything NOT redacted (positions, other objects,
    palette, timing) copied through byte-for-byte from the original.

    `spans`/`matchers` are exactly what clean.py's find_spans()/
    load_matchers() already produced for the audio pass - this re-runs the
    SAME wordlist match against each cue's OCR'd words (not a lookup into
    the transcript's own word list), mirroring how censor_srt() re-scans
    text-track cues rather than trying to align to transcript timing
    word-for-word. A word is redacted if BOTH true: its cue overlaps a
    flagged span (± subs_pad, same padding censor_srt uses) AND the word
    itself matches a wordlist pattern."""
    display_sets = parse_display_sets(sup_path)
    cached = json.loads(json_path.read_text(encoding="utf-8"))

    # placements per display set, recomputed by re-rendering (cheap, no OCR)
    # so redaction coordinates line up with the cached word boxes, which were
    # produced against these same cues in stage 1.
    cues_by_ds = {c.ds_index: c for c in render_cues(display_sets)}
    placements_by_ds = {ds_i: c.placements for ds_i, c in cues_by_ds.items()}

    ivals = [(s - subs_pad, e + subs_pad) for s, e, _ in spans]

    def overlaps(start: float, end: float) -> bool:
        return any(start < ie and lo < end for lo, ie in ivals)

    redactions: dict[int, dict[int, list]] = {}
    words_redacted = 0
    for row in cached["cues"]:
        if not row["words"] or not overlaps(row["start"], row["end"]):
            continue
        ds_index = row["ds_index"]
        cue = cues_by_ds.get(ds_index)
        if cue is None:
            continue

        # Group by line, left-to-right - grow_word_box() needs same-line
        # neighbors in this order to cap its growth (see its docstring).
        by_line: dict[int, list[dict]] = {}
        for w in row["words"]:
            by_line.setdefault(w["line"], []).append(w)
        for line_words in by_line.values():
            line_words.sort(key=lambda lw: lw["left"])

        for line_words in by_line.values():
            for i, w in enumerate(line_words):
                token = w["text"].strip(".,!?;:'\"-()[]{}*")
                if not token or not any(rx is not None and rx.search(token) for rx in matchers.values()):
                    continue
                placement, _lx, _ly = _locate_placement(
                    ds_index, placements_by_ds, w["left"], w["top"], w["width"], w["height"],
                    cue.min_x, cue.min_y)
                if placement is None:
                    continue

                obj_w, obj_h, idx_bytes = display_sets[ds_index].images[placement.obj_id]
                palette = display_sets[ds_index].palette
                box = grow_word_box(
                    w, line_words, i, cue.min_x, cue.min_y, placement.x, placement.y, obj_w, obj_h,
                    ink_columns_fn=lambda y0, y1: _ink_columns(idx_bytes, obj_w, obj_h, palette, y0, y1),
                    mask_pad_px=mask_pad_px)
                if box is None:
                    continue
                redactions.setdefault(ds_index, {}).setdefault(placement.obj_id, []).append(box)
                words_redacted += 1

    _splice_sup(sup_path, display_sets, redactions, out_sup_path)
    return {"display_sets_touched": len(redactions), "words_redacted": words_redacted}


def _splice_sup(sup_path: Path, display_sets: list[DisplaySet],
                 redactions: dict[int, dict[int, list]], out_sup_path: Path) -> None:
    """Copy `sup_path` through to `out_sup_path` byte-for-byte, except: for
    every object flagged in `redactions`, replace its ODS segment(s) with a
    freshly RLE-encoded, redacted bitmap (same object id, same dimensions,
    same PTS) - everything else (PCS/WDS/PDS/END, and every other object's
    ODS) is an exact slice of the original bytes."""
    data = sup_path.read_bytes()
    out = bytearray()
    ds_index = -1
    redact_objs: set[int] = set()

    for seg, start_off, end_off in _read_segments(data, with_offsets=True):
        if seg.seg_type == SEG_PCS:
            ds_index += 1
            redact_objs = set(redactions.get(ds_index, {}).keys())
            out += data[start_off:end_off]
            continue
        if seg.seg_type == SEG_ODS:
            obj_id = struct.unpack(">H", seg.payload[0:2])[0]
            if obj_id in redact_objs:
                if seg.payload[3] & 0x40:  # first fragment - build+emit the replacement once
                    w, h, idx_bytes = display_sets[ds_index].images[obj_id]
                    arr = bytearray(idx_bytes)
                    for (bx, by, bw, bh) in redactions[ds_index][obj_id]:
                        for row in range(max(0, by), min(h, by + bh)):
                            rs, re_ = row * w + max(0, bx), row * w + min(w, bx + bw)
                            if re_ > rs:
                                arr[rs:re_] = bytes(re_ - rs)  # -> palette index 0 (transparent)
                    out += _build_ods_segments(seg.pts, obj_id, w, h, _encode_rle(w, h, bytes(arr)))
                continue  # drop the original fragment (first or continuation) - already replaced
        out += data[start_off:end_off]

    out_sup_path.write_bytes(bytes(out))
