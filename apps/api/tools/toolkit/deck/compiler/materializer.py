"""Deterministic materializer: QUANTITATIVE_CODE chart specs → PNG bytes (Pillow).

Zero LLM, zero network, zero randomness: the same generation spec always
produces the same bytes (fixed 2x supersampling, integer-rounded geometry,
quantized text hints). The Typst path keeps drawing its charts natively as
vectors; these PNGs serve PPTX embedding (M2) and any place a raster of the
code-drawn figure is wanted.

The same :func:`chart_frame` validation the layout engine uses gates the draw —
an unusable spec raises :class:`~..errors.DeckLayoutError` loudly (the caller
falls back to a non-chart template upstream, never silently here).
"""
from __future__ import annotations

import math
from io import BytesIO

from ..errors import DeckLayoutError
from .theme import ThemeTokens

_SCALE = 2          # supersample factor, then downscale — byte-stable AA smoothing
_W, _H = 640, 360   # logical canvas (16:9), fixed regardless of data


def material_asset_name(deck_id: str, slide_index: int, visual_spec_id: str) -> str:
    """``{deck_id}_slide_{index}_{visual_spec_id}.png`` (§9.8)."""
    return f"{deck_id}_slide_{slide_index}_{visual_spec_id}.png"


def _nice_ticks(vmin: float, vmax: float, n: int = 4):
    """Round-number grid steps covering [vmin, vmax] — the axis is code-decided."""
    span = (vmax - vmin) or 1.0
    raw = span / n
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1.0
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    lo = math.floor(vmin / step) * step
    hi = math.ceil(vmax / step) * step
    if hi == lo:
        hi = lo + step
    return lo, hi, step


def _hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def _text_h(draw, s: str, font) -> int:
    b = draw.textbbox((0, 0), s, font=font)
    return b[3] - b[1]


def _fit_label(draw, names: list[str], font, width: int):
    """Truncate the LONGEST label with an ellipsis until every label fits ``width``.

    Deterministic and bounded (the axis slot width is data-independent), unlike
    the Typst tier fit which is deliberately trim-free.
    """
    def w(s):
        return draw.textbbox((0, 0), s, font=font)[2]
    cuts: dict[int, int] = {}
    for _ in range(64):
        over = [i for i, s in enumerate(names) if w(s[:cuts.get(i, len(s))] or "…") > width]
        if not over:
            break
        i = max(over, key=lambda k: len(names[k][:cuts.get(k, len(names[k]))]))
        cur = names[i][:cuts.get(i, len(names[i]))]
        cuts[i] = max(1, len(cur) - 1)
    return [(s[:cuts.get(i, len(s))] + "…") if cuts.get(i, len(s)) < len(s) else s
            for i, s in enumerate(names)]


def render_chart_png(labels: list[str], values: list[float], *, kind: str,
                     name: str, theme: ThemeTokens) -> bytes:
    """Bar or line PNG for one chart series. Deterministic bytes for same inputs."""
    from PIL import Image, ImageDraw, ImageFont

    n = len(labels)
    if kind not in ("bar", "line"):
        raise DeckLayoutError(f"materializer: unsupported chart kind {kind!r}")
    lo, hi, step = _nice_ticks(min(values), max(values))
    span = hi - lo

    font_t = ImageFont.load_default(13)        # labels / ticks
    font_s = ImageFont.load_default(11)        # unit suffix, value tags
    probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    axis_w = max(_text_h(probe, f"{t:g}", font_s) for t in
                 [lo, hi, (lo + hi) / 2]) + 8
    left, top, bottom = 12 + axis_w, 16, 28
    pw, ph = _W - left - 12, _H - top - bottom

    img = Image.new("RGB", (_W * _SCALE, _H * _SCALE), _hex(theme.surface))
    d = ImageDraw.Draw(img)

    def fx(i: int) -> float:
        return left + pw * (i + 0.5) / n

    def fy(v: float) -> float:
        return top + ph * (1.0 - (v - lo) / span)

    k = 0
    while True:
        yv = lo + k * step
        if yv > hi + step * 1e-9:
            break
        yy = round(fy(yv)) * _SCALE
        tag = f"{round(yv, 10):g}"
        d.line([(left * _SCALE, yy), ((left + pw) * _SCALE, yy)],
               fill=_hex(theme.divider), width=max(1, _SCALE))
        d.text(((left - 6) * _SCALE, yy - _text_h(probe, tag, font_s) // 2 * _SCALE),
               tag, font=font_s, fill=_hex(theme.muted), anchor="ra")
        k += 1

    accent, primary = _hex(theme.accent), _hex(theme.primary)
    if kind == "bar":
        bw = pw * 0.62 / n
        for i, v in enumerate(values):
            x0 = (fx(i) - bw / 2) * _SCALE
            y0, y1 = fy(max(v, 0.0)) * _SCALE, fy(min(v, 0.0)) * _SCALE
            d.rounded_rectangle([x0, y0, x0 + bw * _SCALE, max(y1, y0 + 1)],
                                radius=3 * _SCALE, fill=accent)
    else:
        pts = [(round(fx(i) * _SCALE), round(fy(v) * _SCALE)) for i, v in enumerate(values)]
        if len(pts) > 1:
            d.line(pts, fill=primary, width=3 * _SCALE, joint="curve")
        for px, py in pts:
            r = 4 * _SCALE
            d.ellipse([px - r, py - r, px + r, py + r], fill=primary)

    d.line([(left * _SCALE, (top + ph) * _SCALE), ((left + pw) * _SCALE, (top + ph) * _SCALE)],
           fill=_hex(theme.border), width=2 * _SCALE)

    slot = pw / n
    short = _fit_label(probe, [str(s) for s in labels], font_t, int(slot))
    for i, s in enumerate(short):
        d.text((round(fx(i) * _SCALE), (top + ph + 6) * _SCALE), s,
               font=font_t, fill=_hex(theme.ink), anchor="ma")

    title = name if len(name) <= 42 else name[:41] + "…"
    tw = probe.textlength(title, font=font_t)
    d.text((((_W - tw) / 2) * _SCALE, 2 * _SCALE), title,
           font=font_t, fill=_hex(theme.primary), anchor="la")

    img = img.resize((_W, _H), Image.LANCZOS)
    buf = BytesIO()
    img.save(buf, "png", optimize=True)         # no timestamps: stable bytes
    return buf.getvalue()
