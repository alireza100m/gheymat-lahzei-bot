"""
ساخت تصویرهای کانال: کارت قیمت و نمودار هفتگی (با Pillow).

رنگ‌ها از لوگوی کانال (سرمه‌ای و طلایی) گرفته شده‌اند.
متن فارسی با موتور raqm چیده می‌شود؛ اگر روی سیستم در دسترس نباشد،
can_render() مقدار False برمی‌گرداند و ربات به‌جای تصویر، متن می‌فرستد.
"""
from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, features

ASSETS = Path(__file__).with_name("assets")
NAVY_TOP = (22, 36, 74)
NAVY_BOTTOM = (10, 17, 40)
PANEL = (255, 255, 255, 14)
GOLD = (242, 182, 50)
GOLD_LIGHT = (255, 226, 154)
WHITE = (255, 255, 255)
MUTED = (165, 176, 204)
LINE = (255, 255, 255, 28)
GREEN = (34, 197, 94)
RED = (239, 68, 68)
W = 1080


def can_render() -> bool:
    return features.check("raqm") and (ASSETS / "Vazirmatn-Bold.ttf").exists()


def _font(size: int, weight: str = "Bold") -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(ASSETS / f"Vazirmatn-{weight}.ttf"), size,
                              layout_engine=ImageFont.Layout.RAQM)


def _bg(h: int) -> Image.Image:
    img = Image.new("RGB", (W, h))
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / max(1, h - 1)
        d.line([(0, y), (W, y)], fill=tuple(round(a + (b - a) * t)
                                            for a, b in zip(NAVY_TOP, NAVY_BOTTOM)))
    return img


def _rtl(d: ImageDraw.ImageDraw, xy, text, font, fill, anchor="ra"):
    d.text(xy, text, font=font, fill=fill, anchor=anchor, direction="rtl",
           language="fa")


def _header(img: Image.Image, title: str, subtitle: str) -> int:
    d = ImageDraw.Draw(img)
    logo_p = ASSETS / "logo.png"
    if logo_p.exists():
        logo = Image.open(logo_p).convert("RGBA").resize((120, 120), Image.LANCZOS)
        mask = Image.new("L", logo.size, 0)
        ImageDraw.Draw(mask).ellipse((0, 0, 119, 119), fill=255)
        img.paste(logo.convert("RGB"), (64, 56), mask)
    _rtl(d, (W - 64, 58), title, _font(58, "Black"), GOLD)
    _rtl(d, (W - 64, 138), subtitle, _font(32, "Regular"), MUTED)
    d.line([(64, 210), (W - 64, 210)], fill=GOLD, width=3)
    return 240


def _footer(img: Image.Image, handle: str) -> None:
    d = ImageDraw.Draw(img, "RGBA")
    h = img.height
    d.line([(64, h - 96), (W - 64, h - 96)], fill=LINE, width=2)
    d.text((W // 2, h - 52), handle, font=_font(34), fill=GOLD_LIGHT, anchor="mm")


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def price_card(sections: list[tuple[str, list[tuple[str, str]]]],
               title: str, subtitle: str, handle: str) -> bytes:
    """sections: [(سرتیتر گروه، [(عنوان قلم، قیمت قالب‌بندی‌شده)])]"""
    row_h, head_h, gap = 64, 76, 26
    body = sum(head_h + len(rows) * row_h + gap for _, rows in sections if rows)
    h = 240 + body + 130
    img = _bg(h)
    y = _header(img, title, subtitle)
    d = ImageDraw.Draw(img, "RGBA")
    f_head, f_row, f_val = _font(38, "Black"), _font(36, "Regular"), _font(38, "Bold")
    for head, rows in sections:
        if not rows:
            continue
        _rtl(d, (W - 64, y + 14), head, f_head, GOLD)
        y += head_h
        top = y
        d.rounded_rectangle([(52, top - 6), (W - 52, top + len(rows) * row_h)],
                            radius=22, fill=PANEL)
        for i, (name, value) in enumerate(rows):
            cy = y + row_h // 2
            _rtl(d, (W - 84, cy), name, f_row, WHITE, anchor="rm")
            has_letters = any("\u0621" <= ch <= "\u06cc" for ch in value)
            d.text((84, cy), value, font=f_val, fill=GOLD_LIGHT, anchor="lm",
                   direction="rtl" if has_letters else "ltr", language="fa")
            if i < len(rows) - 1:
                d.line([(84, y + row_h), (W - 84, y + row_h)], fill=LINE, width=1)
            y += row_h
        y += gap
    _footer(img, handle)
    return _png(img)


def weekly_chart(series: list[tuple[str, str, list[tuple[float, float]]]],
                 title: str, subtitle: str, handle: str,
                 fmt=lambda v: f"{v:,.0f}", day_label=lambda ts: "") -> bytes:
    """series: [(عنوان، واحد، [(timestamp, value)])] — هر سری یک پنل جدا."""
    panel_h = 520
    h = 240 + len(series) * (panel_h + 40) + 120
    img = _bg(h)
    y = _header(img, title, subtitle)
    d = ImageDraw.Draw(img, "RGBA")
    for name, unit, pts in series:
        pts = sorted(pts)
        d.rounded_rectangle([(52, y), (W - 52, y + panel_h)], radius=26, fill=PANEL)
        if len(pts) < 2:
            _rtl(d, (W // 2, y + panel_h // 2), f"{name}: داده‌ی کافی نیست",
                 _font(34, "Regular"), MUTED, anchor="mm")
            y += panel_h + 40
            continue
        first, last = pts[0][1], pts[-1][1]
        pct = (last - first) / first * 100 if first else 0
        color = GREEN if pct >= 0 else RED
        _rtl(d, (W - 84, y + 26), name, _font(40, "Black"), WHITE)
        _rtl(d, (W - 84, y + 82), f"{fmt(last)} {unit}", _font(34, "Bold"), GOLD_LIGHT)
        sign = "+" if pct > 0 else ("−" if pct < 0 else "")
        d.text((84, y + 30), f"{sign}{fmt_pct(abs(pct))}٪ در هفته", font=_font(34, "Bold"),
               fill=color, anchor="la", direction="rtl", language="fa")
        # ناحیه‌ی رسم
        x0, x1 = 100, W - 100
        py0, py1 = y + 160, y + panel_h - 70
        t0, t1 = pts[0][0], pts[-1][0]
        lo = min(v for _, v in pts)
        hi = max(v for _, v in pts)
        pad = (hi - lo) * 0.12 or hi * 0.01 or 1
        lo, hi = lo - pad, hi + pad

        def P(ts, v):
            return (x0 + (ts - t0) / ((t1 - t0) or 1) * (x1 - x0),
                    py1 - (v - lo) / (hi - lo) * (py1 - py0))

        for k in range(4):
            gy = py0 + k * (py1 - py0) / 3
            d.line([(x0, gy), (x1, gy)], fill=LINE, width=1)
        xy = [P(t, v) for t, v in pts]
        d.polygon(xy + [(xy[-1][0], py1), (xy[0][0], py1)], fill=GOLD + (40,))
        d.line(xy, fill=GOLD, width=6, joint="curve")
        d.ellipse([xy[-1][0] - 10, xy[-1][1] - 10, xy[-1][0] + 10, xy[-1][1] + 10],
                  fill=GOLD_LIGHT)
        # برچسب روزها
        seen = set()
        for ts, _ in pts:
            lab = day_label(ts)
            if lab and lab not in seen:
                seen.add(lab)
                x, _ = P(ts, lo)
                if x0 <= x <= x1 - 20:
                    _rtl(d, (x, py1 + 16), lab, _font(24, "Regular"), MUTED, anchor="ma")
        # بیشترین و کمترین
        ih = max(range(len(pts)), key=lambda i: pts[i][1])
        il = min(range(len(pts)), key=lambda i: pts[i][1])
        for i, txt, dy in ((ih, "سقف", -46), (il, "کف", 14)):
            x, yy = xy[i]
            x = min(max(x, x0 + 90), x1 - 90)
            d.text((x, yy + dy), f"{txt} {fmt(pts[i][1])}", font=_font(24, "Bold"),
                   fill=WHITE, anchor="ma", direction="rtl", language="fa")
        y += panel_h + 40
    _footer(img, handle)
    return _png(img)


def fmt_pct(x: float) -> str:
    return f"{x:.1f}".translate(str.maketrans("0123456789.", "۰۱۲۳۴۵۶۷۸۹٫"))
