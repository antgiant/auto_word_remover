"""vobsub_ocr.py - decode a VobSub (DVD bitmap) subtitle track, OCR it, and
censor flagged words DIRECTLY IN THE BITMAP - the VobSub analogue of
pgs_ocr.py, for when there's no text/PGS subtitle to work from but the
source has a DVD-era VobSub track.

VobSub predates PGS and is a considerably fussier legacy format: each
subtitle ("SPU", Sub-Picture Unit) is wrapped in classic MPEG-2 Program
Stream framing (a 14-byte pack header + a private-stream-1 PES header,
repeating every 2048 bytes for an SPU that spans more than one "pack"), and
its bitmap is RLE-encoded as two independently-encoded INTERLACED fields
(even/odd scanlines) using a 4-bit (nibble), variable-length run code -
quite different from PGS's clean byte-aligned RLE. All of this - the pack
framing byte layout, the SPU control-sequence command set, the nibble RLE
codes, and the (undocumented anywhere obvious) reversed palette-slot
mapping below - was verified against REAL extracted VobSub bytes from this
project's own library (Crocodile Dundee (1986), which conveniently also has
a real text subtitle track for the same dialogue) before writing the
decoder: the exact byte offsets of the pack/PES framing were found by
scanning for repeated `00 00 01 BA` pack-start markers, and the full
decode chain was confirmed correct by rendering a real subtitle and reading
back "Sue, don't misunderstand me, please." - character for character
against that movie's own known SRT line at the same timestamp.

**Reversed palette-slot mapping (the one genuinely surprising find)**: the
DVD SPU spec's SET_CONTR command gives 4 nibbles, one alpha level per pixel
value 0-3 - but empirically, pixel value V's alpha is at nibble position
`3 - V`, not position V. Confirmed by rendering with both mappings on a
real subtitle: the "as-documented" (position == pixel value) mapping
produced a solid black rectangle (implying zero transparent pixels
anywhere, impossible for real subtitle text); reversing it produced
correct, readable text. Applied consistently to SET_COLOR too, though only
alpha (for OCR/redaction) actually matters here - color is decoded but
unused, exactly like pgs_ocr.py's palette handling.

This module reuses pgs_ocr.py's format-agnostic pieces directly rather than
duplicating them: `Cue`/`Placement` (a VobSub cue always has exactly one
placement - VobSub has no PGS-style multi-window compositing), `ocr_cue`
(OCR itself doesn't care which format the pixels came from), `_ink_columns`
and `grow_word_box` (the redaction-box-growing algorithm), and `write_srt`/
`_srt_ts`.

Cue timing comes from the SPU's OWN control sequences, not the .idx file
alone: the .idx `timestamp:` gives the display START, and the SPU's own
STP_DSP (stop display) command - found in a later control sequence in the
same packet, at some further `delay` (1/100s units) - gives how long after
that it stays up. A cue with no STP_DSP in its own SPU (rare) falls back to
a fixed duration.

OCR accuracy note (same caveat as pgs_ocr.py): approximate, not
authoritative - a missed transcription/OCR is a missed redaction.
"""
from __future__ import annotations

import json
import re
import struct
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from pgs_ocr import Cue, Placement, ocr_cue, _ink_columns, grow_word_box, write_srt

# ---------------------------------------------------------------------------
# .idx parsing
# ---------------------------------------------------------------------------
_IDX_TS_RE = re.compile(r"timestamp:\s*(\d+):(\d+):(\d+):(\d+),\s*filepos:\s*([0-9a-fA-F]+)")


def parse_idx(idx_path: Path) -> list[tuple[float, int]]:
    """[(start_seconds, filepos), ...] in file order - one per SPU entry.
    Multiple `id:` (language) blocks aren't handled specially; callers pick
    a single-language track via mkvextract before this ever runs, so a
    real .idx here only ever has one block."""
    entries = []
    for line in idx_path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _IDX_TS_RE.search(line)
        if m:
            h, mn, s, ms = (int(x) for x in m.group(1, 2, 3, 4))
            entries.append((h * 3600 + mn * 60 + s + ms / 1000.0, int(m.group(5), 16)))
    return entries


# ---------------------------------------------------------------------------
# .sub pack/PES unwrapping (MPEG-2 Program Stream framing) - see module
# docstring for how this was verified against real bytes.
# ---------------------------------------------------------------------------
_PACK_SIZE = 2048


def read_spu_at(data: bytes, filepos: int) -> tuple[bytes, int]:
    """(raw_spu_bytes, substream_id) for the SPU starting at `filepos` - a
    pack boundary. Follows continuation packs (each own pack+PES header,
    every 2048 bytes) until the SPU's own declared SIZE (its first 2 bytes)
    is fully collected."""
    cursor = filepos
    buf = bytearray()
    target = None
    substream = None
    while True:
        if data[cursor:cursor + 4] != b"\x00\x00\x01\xba":
            raise ValueError(f"expected pack header at {cursor}, found {data[cursor:cursor+4].hex()}")
        cursor += 14
        if data[cursor:cursor + 4] != b"\x00\x00\x01\xbd":
            raise ValueError(f"expected private-stream-1 PES at {cursor}")
        cursor += 4
        pes_len = struct.unpack(">H", data[cursor:cursor + 2])[0]
        cursor += 2
        cursor += 1  # flag byte (scrambling/priority/alignment/copyright/original) - unused
        ptsdts = data[cursor]; cursor += 1
        hdr_len = data[cursor]; cursor += 1
        cursor += hdr_len  # skip PTS/DTS if present (first pack only, normally)
        if substream is None:
            substream = data[cursor]
        cursor += 1  # substream id - repeated at the start of every pack's payload
        payload_len = pes_len - 3 - hdr_len - 1
        buf.extend(data[cursor:cursor + payload_len])
        cursor += payload_len
        if target is None:
            target = struct.unpack(">H", bytes(buf[0:2]))[0]
        if len(buf) >= target:
            break
        cursor = ((cursor + _PACK_SIZE - 1) // _PACK_SIZE) * _PACK_SIZE
    return bytes(buf[:target]), substream


# ---------------------------------------------------------------------------
# SPU control sequences
# ---------------------------------------------------------------------------
@dataclass
class _CtrlSeq:
    offset: int  # absolute byte offset within the SPU packet
    delay: int  # 1/100s, relative to this SPU's own display start
    next_offset: int
    commands: list  # [(op_name, *params), ...]
    end_offset: int  # byte offset right after this sequence's data


def _parse_ctrl_seq(spu: bytes, offset: int) -> _CtrlSeq:
    delay, next_off = struct.unpack(">HH", spu[offset:offset + 4])
    i = offset + 4
    cmds = []
    while True:
        op = spu[i]; i += 1
        if op == 0xFF:
            cmds.append(("STOP",)); break
        elif op == 0x00:
            cmds.append(("FSTA_DSP",))
        elif op == 0x01:
            cmds.append(("STA_DSP",))
        elif op == 0x02:
            cmds.append(("STP_DSP",))
        elif op == 0x03:
            b0, b1 = spu[i], spu[i + 1]; i += 2
            cmds.append(("SET_COLOR", b0 >> 4, b0 & 0xF, b1 >> 4, b1 & 0xF))
        elif op == 0x04:
            b0, b1 = spu[i], spu[i + 1]; i += 2
            cmds.append(("SET_CONTR", b0 >> 4, b0 & 0xF, b1 >> 4, b1 & 0xF))
        elif op == 0x05:
            b = spu[i:i + 6]; i += 6
            x1 = (b[0] << 4) | (b[1] >> 4)
            x2 = ((b[1] & 0xF) << 8) | b[2]
            y1 = (b[3] << 4) | (b[4] >> 4)
            y2 = ((b[4] & 0xF) << 8) | b[5]
            cmds.append(("SET_DAREA", x1, x2, y1, y2))
        elif op == 0x06:
            top, bot = struct.unpack(">HH", spu[i:i + 4]); i += 4
            cmds.append(("SET_DSPXA", top, bot))
        else:  # unrecognised opcode - stop rather than misparse the rest
            cmds.append(("UNKNOWN", op)); break
    return _CtrlSeq(offset, delay, next_off, cmds, i)


def _walk_ctrl_seqs(spu: bytes, dcsqt: int) -> list[_CtrlSeq]:
    seqs = []
    seen = set()
    off = dcsqt
    while off not in seen:
        seen.add(off)
        seq = _parse_ctrl_seq(spu, off)
        seqs.append(seq)
        if seq.next_offset == off:
            break
        off = seq.next_offset
    return seqs


@dataclass
class SpuInfo:
    size: int
    dcsqt: int
    darea: tuple  # (x1, x2, y1, y2)
    dspxa: tuple  # (top_offset, bottom_offset)
    contr: tuple  # 4 alpha nibbles, ALREADY un-reversed (index by pixel value 0-3)
    start_delay: float  # seconds, relative to this SPU's own PTS/idx timestamp
    end_delay: float | None  # seconds after start_delay when STP_DSP fires, or None
    ctrl_seqs: list  # for rebuilding on redaction


def parse_spu_info(spu: bytes) -> SpuInfo:
    size, dcsqt = struct.unpack(">HH", spu[0:4])
    seqs = _walk_ctrl_seqs(spu, dcsqt)
    darea = dspxa = None
    contr_raw = (15, 15, 15, 0)  # sane fallback: all opaque except slot 3
    start_delay = 0.0
    end_delay = None
    for seq in seqs:
        for cmd in seq.commands:
            if cmd[0] == "SET_DAREA" and darea is None:
                darea = cmd[1:]
            elif cmd[0] == "SET_DSPXA" and dspxa is None:
                dspxa = cmd[1:]
            elif cmd[0] == "SET_CONTR":
                contr_raw = cmd[1:]
            elif cmd[0] == "STA_DSP":
                start_delay = seq.delay / 100.0
            elif cmd[0] == "STP_DSP":
                end_delay = seq.delay / 100.0
    # see module docstring: empirically, alpha for pixel value V sits at
    # nibble position (3 - V), not position V.
    contr = tuple(contr_raw[3 - v] for v in range(4))
    return SpuInfo(size, dcsqt, darea, dspxa, contr, start_delay, end_delay, seqs)


# ---------------------------------------------------------------------------
# nibble RLE decode/encode
# ---------------------------------------------------------------------------
def _get_nibble(buf: bytes, nib_off: int) -> int:
    b = buf[nib_off >> 1]
    return (b >> 4) if (nib_off & 1) == 0 else (b & 0xF)


def _decode_field(spu: bytes, byte_start: int, byte_end: int, width: int, height: int) -> np.ndarray:
    img = np.zeros((height, width), dtype=np.uint8)
    nib = byte_start * 2
    for row in range(height):
        x = 0
        while x < width:
            n1 = _get_nibble(spu, nib); nib += 1
            code = n1
            if code < 0x4:
                code = (code << 4) | _get_nibble(spu, nib); nib += 1
                if code < 0x10:
                    code = (code << 4) | _get_nibble(spu, nib); nib += 1
                    if code < 0x40:
                        code = (code << 4) | _get_nibble(spu, nib); nib += 1
            length, color = code >> 2, code & 3
            if length == 0:
                length = width - x
            length = min(length, width - x)
            img[row, x:x + length] = color
            x += length
        if nib & 1:
            nib += 1  # each row's RLE stream byte-aligns before the next row
    return img


def decode_bitmap(spu: bytes, info: SpuInfo) -> np.ndarray:
    x1, x2, y1, y2 = info.darea
    w, h = x2 - x1 + 1, y2 - y1 + 1
    top_off, bot_off = info.dspxa
    h_top, h_bot = (h + 1) // 2, h // 2
    top = _decode_field(spu, top_off, bot_off, w, h_top)
    bot = _decode_field(spu, bot_off, info.size, w, h_bot)
    full = np.zeros((h, w), dtype=np.uint8)
    full[0::2] = top
    full[1::2] = bot
    return full


def _emit_run_nibbles(nibbles: list, length: int, color: int) -> None:
    """Always uses the unambiguous 4-nibble (16-bit) form - not maximally
    compact, but never risks emitting something the 1/2/3-nibble escalation
    rules in _decode_field could misread (see the module docstring: getting
    this format's bit-packing exactly right matters more than being small).
    A run over 255 long is just chunked into several codes."""
    while length > 0:
        chunk = min(length, 255)
        val = (chunk << 2) | color
        nibbles.append(0)
        nibbles.append((val >> 8) & 0xF)
        nibbles.append((val >> 4) & 0xF)
        nibbles.append(val & 0xF)
        length -= chunk


def _encode_field(pixels: np.ndarray) -> bytes:
    h, w = pixels.shape
    nibbles: list = []
    for row in range(h):
        x = 0
        while x < w:
            color = int(pixels[row, x])
            run = 1
            while x + run < w and pixels[row, x + run] == color:
                run += 1
            _emit_run_nibbles(nibbles, run, color)
            x += run
        if len(nibbles) % 2:
            nibbles.append(0)  # byte-align before the next row
    if len(nibbles) % 2:
        nibbles.append(0)
    out = bytearray(len(nibbles) // 2)
    for i in range(0, len(nibbles), 2):
        out[i // 2] = (nibbles[i] << 4) | nibbles[i + 1]
    return bytes(out)


def encode_bitmap(pixels: np.ndarray) -> tuple[bytes, bytes]:
    """(top_field_bytes, bottom_field_bytes) - the inverse of decode_bitmap,
    splitting back into interlaced fields the same way it joined them."""
    h = pixels.shape[0]
    top = pixels[0::2]
    bot = pixels[1::2]
    return _encode_field(top), _encode_field(bot)


# ---------------------------------------------------------------------------
# render: reuse pgs_ocr's Cue/Placement - a VobSub cue always has exactly
# one placement (no PGS-style multi-window compositing), so min_x/min_y ==
# the placement's own x/y and the canvas is just this one bitmap.
# ---------------------------------------------------------------------------
@dataclass
class VobSubEntry:
    index: int
    start: float
    end: float
    spu: bytes
    info: SpuInfo
    pixels: np.ndarray  # decoded once, reused for both OCR render and redaction


def parse_vobsub(idx_path: Path, sub_path: Path, default_duration: float = 4.0) -> list[VobSubEntry]:
    idx_entries = parse_idx(idx_path)
    data = sub_path.read_bytes()
    out = []
    for i, (start, filepos) in enumerate(idx_entries):
        spu, _substream = read_spu_at(data, filepos)
        info = parse_spu_info(spu)
        if info.darea is None or info.dspxa is None:
            continue  # no displayable bitmap in this SPU - skip
        pixels = decode_bitmap(spu, info)
        end = start + info.start_delay + (info.end_delay if info.end_delay is not None else default_duration)
        out.append(VobSubEntry(i, start + info.start_delay, end, spu, info, pixels))
    return out


def render_cues(entries: list[VobSubEntry]) -> list[Cue]:
    cues = []
    for e in entries:
        x1, x2, y1, y2 = e.info.darea
        alpha_lut = np.zeros(256, dtype=np.int16)
        for v in range(4):
            alpha_lut[v] = e.info.contr[v] * 17  # 0-15 -> 0-255
        gray = (255 - alpha_lut[e.pixels]).astype(np.uint8)
        placement = Placement(obj_id=0, x=x1, y=y1, w=e.pixels.shape[1], h=e.pixels.shape[0])
        cues.append(Cue(e.index, e.start, e.end, gray, x1, y1, [placement]))
    return cues


# ---------------------------------------------------------------------------
# stage 1: extract + OCR + cache (mirrors pgs_ocr.analyze_pgs_track)
# ---------------------------------------------------------------------------
def analyze_vobsub_track(mkvextract: str, media: Path, track_id: int, cache_base: Path,
                          lang: str = "eng", tesseract_cmd: str | None = None,
                          min_len_s: float = 0.08, force: bool = False) -> dict:
    idx_path = cache_base.with_suffix(cache_base.suffix + ".idx")
    sub_path = cache_base.with_suffix(cache_base.suffix + ".sub")
    srt_path = cache_base.with_suffix(cache_base.suffix + ".srt")
    json_path = cache_base.with_suffix(cache_base.suffix + ".words.json")

    if not force and all(p.is_file() for p in (idx_path, sub_path, srt_path, json_path)):
        cached = json.loads(json_path.read_text(encoding="utf-8"))
        return {"idx_path": idx_path, "sub_path": sub_path, "srt_path": srt_path, "json_path": json_path,
               "entries": cached["entries"], "image_cues": len(cached["cues"]),
               "text_cues": sum(1 for c in cached["cues"] if c["text"]), "cached": True}

    # mkvextract requires the .idx extension specifically for S_VOBSUB - it
    # writes the matching .sub itself (same stem, .sub extension).
    subprocess.run([mkvextract, "tracks", str(media), f"{track_id}:{idx_path}"], check=True)
    entries = parse_vobsub(idx_path, sub_path)
    cues = render_cues(entries)

    srt_rows: list[tuple[float, float, str]] = []
    json_rows: list[dict] = []
    for cue in cues:
        if cue.end - cue.start < min_len_s:
            continue
        text, words = ocr_cue(cue, lang=lang, tesseract_cmd=tesseract_cmd)
        json_rows.append({"index": cue.ds_index, "start": cue.start, "end": cue.end,
                          "text": text, "words": words})
        if text:
            srt_rows.append((cue.start, cue.end, text))

    write_srt(srt_rows, srt_path)
    json_path.write_text(json.dumps({"entries": len(entries), "cues": json_rows}), encoding="utf-8")
    return {"idx_path": idx_path, "sub_path": sub_path, "srt_path": srt_path, "json_path": json_path,
           "entries": len(entries), "image_cues": len(cues), "text_cues": len(srt_rows), "cached": False}


# ---------------------------------------------------------------------------
# stage 2: redact + splice (mirrors pgs_ocr.censor_pgs_track / _splice_sup)
# ---------------------------------------------------------------------------
def censor_vobsub_track(idx_path: Path, sub_path: Path, json_path: Path, spans, matchers: dict,
                         out_idx_path: Path, subs_pad: float = 0.15, mask_pad_px: int = 6) -> dict:
    """Redact every wordlist-matching word in a cue overlapping a flagged
    span directly in the VobSub bitmap - same re-scan-the-cue's-own-OCR'd-
    words approach as censor_srt()/censor_pgs_track(), not a lookup into
    the transcript's own timing. Writes a new .idx (byte-identical to the
    original - only filepos values ever need to change, and this rewrites
    every entry fresh) + matching .sub."""
    entries = parse_vobsub(idx_path, sub_path)
    entries_by_index = {e.index: e for e in entries}
    cues_by_index = {c.ds_index: c for c in render_cues(entries)}
    cached = json.loads(json_path.read_text(encoding="utf-8"))

    ivals = [(s - subs_pad, e + subs_pad) for s, e, _ in spans]

    def overlaps(start: float, end: float) -> bool:
        return any(start < ie and lo < end for lo, ie in ivals)

    modified: dict[int, np.ndarray] = {}  # entry index -> redacted pixel array (copy)
    words_redacted = 0
    for row in cached["cues"]:
        if not row["words"] or not overlaps(row["start"], row["end"]):
            continue
        idx = row["index"]
        entry = entries_by_index.get(idx)
        cue = cues_by_index.get(idx)
        if entry is None or cue is None:
            continue

        by_line: dict[int, list[dict]] = {}
        for w in row["words"]:
            by_line.setdefault(w["line"], []).append(w)
        for line_words in by_line.values():
            line_words.sort(key=lambda lw: lw["left"])

        pixels = modified.get(idx)
        transparent_value = min(range(4), key=lambda v: entry.info.contr[v])  # alpha==0 slot, normally
        for line_words in by_line.values():
            for i, w in enumerate(line_words):
                token = w["text"].strip(".,!?;:'\"-()[]{}*")
                if not token or not any(rx is not None and rx.search(token) for rx in matchers.values()):
                    continue
                placement = cue.placements[0]
                obj_h, obj_w = entry.pixels.shape

                def ink_columns_fn(y0, y1, _entry=entry):
                    src = modified.get(idx, _entry.pixels)
                    alpha_lut = np.zeros(256, dtype=np.int16)
                    for v in range(4):
                        alpha_lut[v] = _entry.info.contr[v]
                    band = src[max(0, y0):min(obj_h, y1), :]
                    return (alpha_lut[band] > 0).any(axis=0)

                box = grow_word_box(w, line_words, i, cue.min_x, cue.min_y, placement.x, placement.y,
                                    obj_w, obj_h, ink_columns_fn, mask_pad_px=mask_pad_px)
                if box is None:
                    continue
                if pixels is None:
                    pixels = entry.pixels.copy()
                bx, by, bw, bh = box
                pixels[max(0, by):min(obj_h, by + bh), max(0, bx):min(obj_w, bx + bw)] = transparent_value
                words_redacted += 1
        if pixels is not None:
            modified[idx] = pixels

    _write_vobsub(idx_path, sub_path, entries, modified, out_idx_path)
    return {"entries_touched": len(modified), "words_redacted": words_redacted}


def _rebuild_spu(entry: "VobSubEntry", new_pixels: np.ndarray) -> bytes:
    """A full SPU packet for `entry` with `new_pixels` instead of its
    original bitmap - same control-sequence commands (position/palette/
    timing all unchanged), just new RLE data and the header/SET_DSPXA
    offsets patched to match its new length."""
    top_bytes, bot_bytes = encode_bitmap(new_pixels)
    new_top_off = 4
    new_bot_off = new_top_off + len(top_bytes)
    new_dcsqt = new_bot_off + len(bot_bytes)

    old_dcsqt = entry.info.dcsqt
    delta = new_dcsqt - old_dcsqt
    ctrl_bytes = bytearray(entry.spu[old_dcsqt:entry.info.size])
    # patch each sequence's `next` pointer (shifts by `delta`, same as the
    # whole table) and any SET_DSPXA params (point at the brand new bitmap,
    # not shifted - recomputed outright) - everything else byte-identical.
    for seq in entry.info.ctrl_seqs:
        local = seq.offset - old_dcsqt
        struct.pack_into(">H", ctrl_bytes, local + 2, seq.next_offset + delta)
        off = local + 4
        for cmd in seq.commands:
            if cmd[0] == "SET_DSPXA":
                struct.pack_into(">HH", ctrl_bytes, off + 1, new_top_off, new_bot_off)
                off += 5
            elif cmd[0] == "SET_COLOR" or cmd[0] == "SET_CONTR":
                off += 3
            elif cmd[0] == "SET_DAREA":
                off += 7
            else:
                off += 1

    new_size = new_dcsqt + len(ctrl_bytes)
    header = struct.pack(">HH", new_size, new_dcsqt)
    return header + top_bytes + bot_bytes + bytes(ctrl_bytes)


def _wrap_spu_in_packs(spu: bytes, orig_first_pack_pts: bytes, substream: int) -> bytes:
    """Re-wrap a (possibly resized) SPU packet in fresh pack/PES framing,
    reusing the ORIGINAL PTS bytes for the first pack (timing is unchanged -
    only pixel content is - so the original PTS is still exactly correct)."""
    # SCR/mux_rate bytes are a real captured MPEG2 pack header (the values
    # don't matter for re-import - only .idx timestamps do - but mkvmerge
    # sanity-checks the marker bits to tell MPEG1 from MPEG2 pack headers
    # and warns on anything that matches neither, hence reusing real bytes
    # here rather than zeroing them).
    PACK_HDR = bytes.fromhex("000001ba" "4402c5bfb4010189" "c3f8")
    out = bytearray()
    remaining = spu
    first = True
    while True:
        avail = _PACK_SIZE - 14 - 4 - 2 - (3 + 5 if first else 3) - 1
        chunk = remaining[:avail]
        remaining = remaining[avail:]
        if first:
            pes_payload_len = 3 + 5 + 1 + len(chunk)
            pes = bytes([0x81, 0x80, 0x05]) + orig_first_pack_pts + bytes([substream]) + chunk
        else:
            pes_payload_len = 3 + 1 + len(chunk)
            pes = bytes([0x81, 0x00, 0x00]) + bytes([substream]) + chunk
        pack = PACK_HDR + b"\x00\x00\x01\xbd" + struct.pack(">H", pes_payload_len) + pes
        pack = pack + b"\x00" * (_PACK_SIZE - len(pack))  # pad to the fixed pack size
        out.extend(pack)
        first = False
        if not remaining:
            break
    return bytes(out)


def _write_vobsub(idx_path: Path, sub_path: Path, entries: list, modified: dict,
                   out_idx_path: Path) -> None:
    """Write a full new .idx + matching .sub: every entry NOT in `modified`
    is copied through byte-identical to the source; every entry IN
    `modified` gets a freshly-rebuilt SPU. Rewriting every filepos (rather
    than trying to preserve the original pack layout so untouched entries
    could be a raw byte-range copy) is simpler and just as safe here, since
    an .idx's filepos values are meaningless outside their own .sub anyway."""
    src = sub_path.read_bytes()
    idx_entries = parse_idx(idx_path)
    idx_header = []
    for line in idx_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if _IDX_TS_RE.search(line):
            break
        idx_header.append(line)

    out_sub_path = out_idx_path.with_suffix(".sub")
    sub_out = bytearray()
    idx_lines = list(idx_header)
    for i, (start, filepos) in enumerate(idx_entries):
        entry = next((e for e in entries if e.index == i), None)
        if entry is not None and i in modified:
            spu, substream = read_spu_at(src, filepos)
            first_pack_pts = src[filepos + 23:filepos + 28]  # see read_spu_at's byte accounting
            new_spu = _rebuild_spu(entry, modified[i])
            block = _wrap_spu_in_packs(new_spu, first_pack_pts, substream)
        else:
            # byte-identical passthrough - entries are laid out contiguously
            # by filepos, so this entry's exact original byte range is
            # [filepos, next entry's filepos) (or EOF for the last one).
            next_pos = idx_entries[i + 1][1] if i + 1 < len(idx_entries) else len(src)
            block = src[filepos:next_pos]
        new_filepos = len(sub_out)
        sub_out.extend(block)
        h = int(start // 3600); m = int(start % 3600 // 60); s = int(start % 60)
        ms = int(round((start - int(start)) * 1000))
        idx_lines.append(f"timestamp: {h:02d}:{m:02d}:{s:02d}:{ms:03d}, filepos: {new_filepos:09x}")

    out_idx_path.write_text("\n".join(idx_lines) + "\n", encoding="utf-8")
    out_sub_path.write_bytes(bytes(sub_out))
