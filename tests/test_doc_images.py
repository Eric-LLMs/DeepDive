"""Legacy .doc embedded-image recovery (core.infrastructure.doc_images).

Synthetic OLE-ish containers with real Pillow-encoded rasters at known offsets exercise
the magic-scan + extent logic; false-positive magics (truncated PNG chunks, JPEG without
EOI) must be dropped, and duplicates deduped by sha256.
"""
from __future__ import annotations

import io
import os

from core.infrastructure.doc_images import MAX_IMAGES, MIN_IMAGE_BYTES, scan_doc_images


def _png(seed: int = 1) -> bytes:
    from PIL import Image

    im = Image.frombytes("RGB", (48, 48), os.urandom(48 * 48 * 3).replace(b"\x00", bytes([seed])))
    im.save(io.BytesIO(), "PNG")  # touch encoder before seeking
    buf = io.BytesIO()
    im.save(buf, "PNG")
    blob = buf.getvalue()
    assert len(blob) >= MIN_IMAGE_BYTES
    return blob


def _jpeg() -> bytes:
    from PIL import Image

    im = Image.frombytes("RGB", (48, 48), os.urandom(48 * 48 * 3))
    buf = io.BytesIO()
    im.save(buf, "JPEG")
    return buf.getvalue()


def _container(parts: list[bytes]) -> bytes:
    junk = b"\xd0\xcf\x11\xe0" + b"\x00\x11\x22\x33" * 40
    out = bytearray(junk)
    for blob in parts:
        out += junk[:17] + blob
    return bytes(out)


def test_scan_finds_png_and_jpeg_in_position_order():
    png, jpg = _png(), _jpeg()
    data = _container([jpg, png])  # jpeg first in the file
    images, skipped = scan_doc_images(data)
    assert skipped == 0
    assert [im["name"] for im in images] == ["doc_1.jpg", "doc_2.png"]
    assert images[0]["data"] == jpg and images[0]["mime"] == "image/jpeg"
    assert images[1]["data"] == png and images[1]["mime"] == "image/png"


def test_scan_dedupes_identical_images():
    png = _png()
    images, _ = scan_doc_images(_container([png, png]))
    assert len(images) == 1


def test_scan_drops_truncated_or_false_positive_magics():
    broken_png = _png()[:20]  # signature + partial chunk, no IEND
    broken_jpg = b"\xff\xd8\xff\xe0" + b"\x11" * 600  # no FFD9 anywhere
    data = _container([broken_png, broken_jpg, _png(2)])
    images, _ = scan_doc_images(data)
    assert len(images) == 1  # only the intact third image survives
    assert images[0]["mime"] == "image/png"


def test_scan_caps_at_max_images():
    blobs = [_png(seed % 251) for seed in range(MAX_IMAGES + 5)]
    images, _ = scan_doc_images(_container(blobs))
    assert len(images) == MAX_IMAGES


def test_scan_empty_container_returns_nothing():
    images, skipped = scan_doc_images(b"\xd0\xcf\x11\xe0no images here")
    assert images == [] and skipped == 0
