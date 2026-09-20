"""Deterministic embedded-image scan for legacy Word .doc (OLE2, Word 97-2003).

Word 97 stores inline pictures inside the compound-document ``Data`` stream behind
OfficeArt wrappers; there is no rels/media manifest like OOXML (.docx) to parse, and the
text extractor (antiword) can only leave a ``[pic]`` placeholder. This module recovers
the actual pixels: it locates every raster magic (PNG/JPEG/BMP) and Windows metafile
header (EMF, WMF placeable/standard), slices the container-declared extent, and keeps
only blobs that Pillow can actually decode — a wrong silent drop would repeat the
"``[pic]``, cannot open" blindness this scan exists to fix.

Metafiles (EMF/WMF) convert to PNG where Pillow's GDI-backed plugins exist (Windows);
on other platforms they fail to decode and are counted in ``skipped`` so callers can
tell the user why an image has no asset. Output order follows first-appearance position
in the container, which matches insertion order for ordinary Word documents.
"""
from __future__ import annotations

import hashlib
import io
import struct

MAX_IMAGES = 30
MIN_IMAGE_BYTES = 512
_MAX_EXTENT = 64 * 1024 * 1024  # never trust a header that claims more than 64 MB

_RASTER = "raster"
_METAFILE = "metafile"


def _png_extent(data: bytes, pos: int) -> int | None:
    """Walk PNG chunks to IEND; return total byte length or None."""
    i = pos + 8  # signature already matched
    while i + 8 <= len(data):
        length = struct.unpack(">I", data[i:i + 4])[0]
        ctype = data[i + 4:i + 8]
        end = i + 12 + length  # len + type + data + crc
        if length > _MAX_EXTENT or end > len(data):
            return None
        if ctype == b"IEND":
            return end - pos
        i = end
    return None


def _jpeg_extent(data: bytes, pos: int) -> int | None:
    """FFD9 cannot legally appear inside an entropy stream (0xFF is byte-stuffed),
    so the first occurrence is the real EOI."""
    end = data.find(b"\xff\xd9", pos + 2)
    if end < 0 or end + 2 - pos > _MAX_EXTENT:
        return None
    return end + 2 - pos


def _bmp_extent(data: bytes, pos: int) -> int | None:
    if pos + 26 > len(data):
        return None
    size = struct.unpack("<I", data[pos + 2:pos + 6])[0]
    if any(b != 0 for b in data[pos + 6:pos + 10]):  # reserved fields must be zero
        return None
    off = struct.unpack("<I", data[pos + 10:pos + 14])[0]
    if not (MIN_IMAGE_BYTES <= size <= _MAX_EXTENT) or off < 26 or off >= size:
        return None
    if pos + size > len(data):
        return None
    return size


def _emf_extent(data: bytes, pos: int) -> int | None:
    """EMR_HEADER: RecordType==1, nBytes (whole-file size) at offset 44."""
    if pos + 48 > len(data):
        return None
    if struct.unpack("<I", data[pos:pos + 4])[0] != 1:
        return None
    total = struct.unpack("<I", data[pos + 44:pos + 48])[0]
    if not (88 <= total <= _MAX_EXTENT) or pos + total > len(data):
        return None
    return total


def _wmf_extent(data: bytes, pos: int) -> int | None:
    """WMF, either placeable (22-byte prefix + standard header) or standard
    (18-byte header whose offset 12 holds the whole-file size in bytes)."""
    placeable = data[pos:pos + 4] == b"\xd7\xcd\xc6\x9a"
    std = pos + 22 if placeable else pos
    if std + 18 > len(data):
        return None
    header_words = struct.unpack("<H", data[std + 2:std + 4])[0]
    total = struct.unpack("<I", data[std + 12:std + 16])[0]
    if header_words < 9 or header_words > 20 or not (36 <= total <= _MAX_EXTENT):
        return None
    if pos + (total + (22 if placeable else 0)) > len(data):
        return None
    return total + (22 if placeable else 0)


class _Candidate:
    __slots__ = ("pos", "size", "kind", "fmt")

    def __init__(self, pos: int, size: int, kind: str, fmt: str):
        self.pos = pos
        self.size = size
        self.kind = kind
        self.fmt = fmt


def _find_candidates(content: bytes) -> list[_Candidate]:
    out: list[_Candidate] = []
    seen: set[tuple[int, int]] = set()

    def add(magic: bytes, extent, kind: str, fmt: str):
        i = 0
        while True:
            i = content.find(magic, i)
            if i < 0:
                break
            size = extent(content, i)
            if size is not None and size >= MIN_IMAGE_BYTES and (i, size) not in seen:
                seen.add((i, size))
                out.append(_Candidate(i, size, kind, fmt))
            i += 1

    add(b"\x89PNG\r\n\x1a\n", _png_extent, _RASTER, "png")
    add(b"\xff\xd8\xff", _jpeg_extent, _RASTER, "jpeg")
    add(b"BM", _bmp_extent, _RASTER, "bmp")
    add(b"\xd7\xcd\xc6\x9a", _wmf_extent, _METAFILE, "wmf")
    add(b"\x01\x00\x00\x00", _emf_extent, _METAFILE, "emf")
    out.sort(key=lambda c: c.pos)
    return out


def _metafile_to_png(blob: bytes) -> bytes | None:
    """Render an EMF/WMF to PNG. Pillow's metafile plugins are GDI-backed and decode
    on Windows only; anywhere else this returns None (caller counts a skip)."""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(blob)) as im:
            im.load()
            out = im.convert("RGBA") if im.mode == "RGBA" else im.convert("RGB")
            buf = io.BytesIO()
            out.save(buf, format="PNG")
            return buf.getvalue()
    except Exception:
        return None


def scan_doc_images(content: bytes) -> tuple[list[dict], int]:
    """Return ``([{name, mime, data}, ...], skipped_metafiles)`` for a .doc container.

    Raster blobs are returned as original bytes (no lossy re-encode) and verified with
    Pillow when available; metafiles are converted to PNG. Extent false-positives that
    do not decode are dropped, and metafiles the platform cannot render are counted.
    """
    try:
        from PIL import Image
    except ImportError:
        Image = None

    images: list[dict] = []
    skipped = 0
    seen_sha: set[str] = set()
    for cand in _find_candidates(content):
        if len(images) >= MAX_IMAGES:
            break
        blob = content[cand.pos:cand.pos + cand.size]
        if cand.kind == _METAFILE:
            png = _metafile_to_png(blob)
            if png is None:
                skipped += 1
                continue
            blob, mime, ext = png, "image/png", "png"
        else:
            mime = {"png": "image/png", "jpeg": "image/jpeg", "bmp": "image/bmp"}[cand.fmt]
            ext = "jpg" if cand.fmt == "jpeg" else cand.fmt
            if Image is not None:
                try:
                    with Image.open(io.BytesIO(blob)) as im:
                        im.load()
                except Exception:
                    continue
        sha = hashlib.sha256(blob).hexdigest()
        if sha in seen_sha:
            continue
        seen_sha.add(sha)
        images.append({"name": f"doc_{len(images) + 1}.{ext}", "mime": mime, "data": blob})
    return images, skipped
