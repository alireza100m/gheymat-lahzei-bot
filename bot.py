#!/usr/bin/env python3
"""
ربات انتشار قیمت در کانال‌های ایتا، بله و تلگرام

قیمت طلا، سکه، ارز، رمزارز، نفت و انس‌ها را می‌گیرد و:
  • هر ۱۰ دقیقه یک پیام پین‌شده را ویرایش می‌کند (تلگرام و بله)،
  • هر ساعت یک پست جدید می‌گذارد (شب‌ها کمتر)، چند بار در روز به‌صورت تصویر،
  • ساعت ۲۰ خلاصه‌ی روز و جمعه‌ها نمودار هفتگی طلا و دلار را منتشر می‌کند،
  • اگر دلار یا طلا ناگهان بیش از ۱٪ جابه‌جا شود، هشدار می‌فرستد.

اجرا:
    python bot.py              # اجرای دائمی
    python bot.py --loop 330   # اجرای ۳۳۰ دقیقه‌ای (گیت‌هاب اکشنز)
    python bot.py --once       # یک دور و خروج (برای cron)
    python bot.py --dry-run    # فقط نمایش متن پست، بدون ارسال
    python bot.py --preview card   # ساخت نمونه‌ی تصویر بدون ارسال
    python bot.py --check      # نمایش همه‌ی داده‌های خام منابع (عیب‌یابی)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

try:
    from zoneinfo import ZoneInfo
    TEHRAN = ZoneInfo("Asia/Tehran")
except Exception:  # روی ویندوز بدون پکیج tzdata
    from datetime import timedelta, timezone
    TEHRAN = timezone(timedelta(hours=3, minutes=30))

log = logging.getLogger("price-bot")
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "Mozilla/5.0 (price-bot)"})
TIMEOUT = 15


# ───────────────────────── تنظیمات ─────────────────────────

def load_env(path: str = ".env") -> None:
    """خواندن فایل .env ساده (KEY=VALUE) بدون نیاز به پکیج اضافه."""
    p = Path(__file__).with_name(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


# ───────────────────────── ابزار عدد و تاریخ ─────────────────────────

FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def fa(s) -> str:
    return str(s).translate(FA_DIGITS)


def to_float(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(",", "").replace("٬", "").strip()
    s = s.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))
    try:
        return float(s)
    except ValueError:
        return None


def fmt_num(x: float, decimals: int = 0) -> str:
    if decimals == 0:
        return fa(f"{round(x):,}")
    return fa(f"{x:,.{decimals}f}")


def gregorian_to_jalali(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy + 1 if gm > 2 else gy
    days = (355666 + 365 * gy + (gy2 + 3) // 4 - (gy2 + 99) // 100
            + (gy2 + 399) // 400 + gd + g_d_m[gm - 1])
    jy = -1595 + 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm, jd = 1 + days // 31, 1 + days % 31
    else:
        jm, jd = 7 + (days - 186) // 30, 1 + (days - 186) % 30
    return jy, jm, jd


JALALI_MONTHS = ["فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
                 "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند"]
WEEKDAYS = ["دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه"]


def persian_now() -> str:
    now = datetime.now(TEHRAN)
    jy, jm, jd = gregorian_to_jalali(now.year, now.month, now.day)
    return (f"{WEEKDAYS[now.weekday()]} {fa(jd)} {JALALI_MONTHS[jm - 1]} {fa(jy)}"
            f" | ساعت {fa(now.strftime('%H:%M'))}")


# ───────────────────────── منابع داده ─────────────────────────
# هر منبع یک دیکشنری {کلید_داخلی: (قیمت, درصد_تغییر_یا_None)} برمی‌گرداند.
# اگر منبعی قطع باشد، خطا لاگ می‌شود و آن قلم از منبع بعدی گرفته می‌شود.

def fetch_tgju() -> dict:
    """tgju.org — دلار آزاد، طلا، سکه، انس‌ها، نفت و ارز دیجیتال (همه از یک درخواست)."""
    r = HTTP.get("https://call1.tgju.org/ajax.json", timeout=TIMEOUT)
    r.raise_for_status()
    cur = r.json().get("current", {})
    out = {}
    for key, item in cur.items():
        if not isinstance(item, dict):
            continue
        price = to_float(item.get("p"))
        if price is None:
            continue
        pct = to_float(item.get("dp"))
        if pct is not None and item.get("dt") == "low":
            pct = -abs(pct)
        out[f"tgju:{key}"] = (price, pct)
    return out


def fetch_nobitex() -> dict:
    """نوبیتکس — تتر به ریال، بیت‌کوین و اتریوم به دلار (تتر)."""
    r = HTTP.get(
        "https://api.nobitex.ir/market/stats",
        params={"srcCurrency": "usdt,btc,eth", "dstCurrency": "rls,usdt"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    stats = r.json().get("stats", {})
    out = {}
    for pair, s in stats.items():
        price = to_float(s.get("latest"))
        if price:
            out[f"nobitex:{pair}"] = (price, to_float(s.get("dayChange")))
    return out


def fetch_gold_api() -> dict:
    """gold-api.com — انس طلا و نقره (رایگان، بدون کلید؛ از سرور خارج از ایران)."""
    out = {}
    for sym in ("XAU", "XAG"):
        r = HTTP.get(f"https://api.gold-api.com/price/{sym}", timeout=TIMEOUT)
        r.raise_for_status()
        price = to_float(r.json().get("price"))
        if price:
            out[f"goldapi:{sym}"] = (price, None)
    return out


def fetch_coingecko() -> dict:
    """CoinGecko — بیت‌کوین و اتریوم به دلار (پشتیبان)."""
    r = HTTP.get(
        "https://api.coingecko.com/api/v3/simple/price",
        params={"ids": "bitcoin,ethereum", "vs_currencies": "usd",
                "include_24hr_change": "true"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    d = r.json()
    return {f"coingecko:{k}": (to_float(v["usd"]), to_float(v.get("usd_24h_change")))
            for k, v in d.items() if "usd" in v}


SOURCES = {
    "tgju": fetch_tgju,
    "nobitex": fetch_nobitex,
    "goldapi": fetch_gold_api,
    "coingecko": fetch_coingecko,
}

# ───────────────────────── اقلام پست ─────────────────────────
# هر قلم: عنوان، گروه، تعداد اعشار، ضریب (ریال→تومان = 0.1)،
# و فهرست منابع به ترتیب اولویت. اولین منبعی که جواب بدهد استفاده می‌شود.
# برای حذف یک قلم کافی است کلیدش را از ITEMS بردارید.

R2T = 0.1  # ریال به تومان

# گروه‌های پست به ترتیب نمایش: (کلید، سرتیتر)
GROUPS = [
    ("gold",   "🟡 طلا و سکه - تومان"),
    ("bubble", "🫧 حباب سکه و طلا"),
    ("fx",     "💵 ارز - تومان"),
    ("crypto", "🪙 رمزارز - دلار"),
    ("world",  "🌍 بازار جهانی - دلار"),
]

CATALOG = {
    "gold18":    dict(title="طلای ۱۸ عیار (گرم)", group="gold", dec=0, mul=R2T,
                      src=["tgju:geram18"]),
    "coin":      dict(title="سکه امامی", group="gold", dec=0, mul=R2T,
                      src=["tgju:sekee"]),
    "bahar":     dict(title="سکه بهار آزادی", group="gold", dec=0, mul=R2T,
                      src=["tgju:sekeb"]),
    "half":      dict(title="نیم سکه", group="gold", dec=0, mul=R2T,
                      src=["tgju:nim"]),
    "quarter":   dict(title="ربع سکه", group="gold", dec=0, mul=R2T,
                      src=["tgju:rob"]),
    "gerami":    dict(title="سکه گرمی", group="gold", dec=0, mul=R2T,
                      src=["tgju:gerami"]),
    "silver":    dict(title="نقره ۹۹۹ (گرم)", group="gold", dec=0, mul=R2T,
                      src=["tgju:silver_999"]),
    "usd":       dict(title="دلار آزاد", group="fx", dec=0, mul=R2T,
                      src=["tgju:price_dollar_rl"]),
    "eur":       dict(title="یورو", group="fx", dec=0, mul=R2T,
                      src=["tgju:price_eur"]),
    "aed":       dict(title="درهم امارات", group="fx", dec=0, mul=R2T,
                      src=["tgju:price_aed"]),
    "try":       dict(title="لیر ترکیه", group="fx", dec=0, mul=R2T,
                      src=["tgju:price_try"]),
    "usdt":      dict(title="تتر", group="fx", dec=0, mul=R2T,
                      src=["nobitex:usdt-rls", "tgju:crypto-tether-irr"]),
    "btc":       dict(title="بیت‌کوین", group="crypto", dec=0, mul=1,
                      src=["nobitex:btc-usdt", "coingecko:bitcoin", "tgju:crypto-bitcoin"]),
    "eth":       dict(title="اتریوم", group="crypto", dec=0, mul=1,
                      src=["nobitex:eth-usdt", "coingecko:ethereum", "tgju:crypto-ethereum"]),
    "gold_oz":   dict(title="انس طلا", group="world", dec=2, mul=1,
                      src=["tgju:ons", "goldapi:XAU"]),
    "silver_oz": dict(title="انس نقره", group="world", dec=2, mul=1,
                      src=["tgju:silver", "goldapi:XAG"]),
    "oil":       dict(title="نفت برنت", group="world", dec=2, mul=1,
                      src=["tgju:oil_brent", "tgju:oil"]),
}

DEFAULT_ITEMS = ("gold18,coin,bahar,half,quarter,silver,usd,eur,aed,try,usdt,"
                 "btc,eth,gold_oz,silver_oz,oil")

# حباب = قیمت بازار منهای ارزش ذاتی طلای آن (انس جهانی × دلار آزاد).
# مقدار: گرم طلای خالص در هر واحد.
OUNCE_G = 31.1035
BUBBLE = [
    ("coin",    "سکه امامی", 8.133 * 0.9),
    ("quarter", "ربع سکه",   2.033 * 0.9),
    ("gold18",  "طلای ۱۸ عیار", 0.75),
]


def selected_items() -> list[str]:
    items = [i.strip() for i in env("ITEMS", DEFAULT_ITEMS).split(",") if i.strip()]
    bad = [i for i in items if i not in CATALOG]
    if bad:
        log.error("اقلام ناشناخته در ITEMS: %s (مجاز: %s)", bad, ",".join(CATALOG))
    return [i for i in items if i in CATALOG]


def collect_prices(items: list[str]) -> dict:
    """فقط منابعی را صدا می‌زند که برای اقلام انتخاب‌شده لازم است."""
    disabled = {x.strip() for x in env("DISABLED_SOURCES").split(",") if x.strip()}
    needed = []
    for it in items:
        for s in CATALOG[it]["src"]:
            name = s.split(":")[0]
            if name not in needed and name not in disabled:
                needed.append(name)
    data: dict = {}
    for name in needed:
        try:
            data.update(SOURCES[name]())
        except Exception as e:
            log.warning("منبع %s در دسترس نیست: %s", name, e)
    return data


def change_badge(pct: float | None) -> str:
    if pct is None or abs(pct) < 0.005:
        return ""
    arrow = "🔺" if pct > 0 else "🔻"
    return f"   {arrow}{fa(f'{abs(pct):.2f}')}٪"


def short_toman(t: float) -> str:
    """۸,۸۱۰,۰۰۰ → ۸.۸ میلیون"""
    a = abs(t)
    if a >= 1_000_000:
        txt = f"{fa(f'{a / 1_000_000:.1f}'.rstrip('0').rstrip('.'))} میلیون"
    elif a >= 1_000:
        txt = f"{fa(round(a / 1_000))} هزار"
    else:
        txt = fa(round(a))
    return txt


def pick(data: dict, key: str):
    c = CATALOG[key]
    return next((data[s] for s in c["src"] if s in data and data[s][0]), None)


def bubble_lines(data: dict, items: list[str]) -> list[tuple[str, str]]:
    oz, usd = pick(data, "gold_oz"), pick(data, "usd")
    if not (oz and usd) or env("SHOW_BUBBLE", "1") == "0":
        return []
    gram_toman = oz[0] / OUNCE_G * usd[0] * R2T  # ارزش هر گرم طلای خالص
    out = []
    for key, title, pure_g in BUBBLE:
        v = pick(data, key)
        if key not in items or not v:
            continue
        market = v[0] * R2T
        bubble = market - pure_g * gram_toman
        pct = bubble / (market - bubble) * 100
        sign = "منفی " if bubble < 0 else ""
        out.append((title, f"{sign}{short_toman(bubble)} ({fa(f'{abs(pct):.1f}')}٪)"))
    return out


def values_of(items: list[str], data: dict) -> dict:
    """قیمت نهایی هر قلم به واحد نمایش (تومان یا دلار)."""
    out = {}
    for it in items:
        v = pick(data, it)
        if v is None:
            log.warning("قیمت «%s» پیدا نشد", CATALOG[it]["title"])
        else:
            out[it] = v[0] * CATALOG[it]["mul"]
    return out


def fmt_item(key: str, value: float) -> str:
    return fmt_num(value, CATALOG[key]["dec"])


# ───────────────────────── ساخت متن پست‌ها ─────────────────────────
# B0/B1 نشانگر متن درشت و FOOT جای پانویس است؛ هنگام ارسال برای هر
# پیام‌رسان به قالب خودش تبدیل می‌شوند.
B0, B1, FOOT = "\x01", "\x02", "\x03"
SEP = "➖➖➖➖➖➖➖➖"


def render(text: str, style: str) -> str:
    if style == "html":
        text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return text.replace(B0, "<b>").replace(B1, "</b>")
    if style == "md1":
        text = text.replace("_", "\\_").replace("*", "\\*")
        return text.replace(B0, "*").replace(B1, "*")
    return text.replace(B0, "").replace(B1, "")


def price_sections(items: list[str], data: dict) -> list[tuple[str, list[tuple[str, str]]]]:
    rows: dict = {g: [] for g, _ in GROUPS}
    for it in items:
        v = pick(data, it)
        if v is None:
            continue
        c = CATALOG[it]
        rows[c["group"]].append((c["title"], fmt_num(v[0] * c["mul"], c["dec"]), v[1]))
    rows["bubble"] = [(t, s, None) for t, s in bubble_lines(data, items)]
    return [(title, rows[g]) for g, title in GROUPS if rows[g]]


def build_post(items: list[str], data: dict, header: str, note: str = "") -> str | None:
    sections = price_sections(items, data)
    if not any(r for g, r in sections if g != GROUPS[1][1]):
        return None
    lines = [B0 + header + B1, f"🗓 {persian_now()}"]
    if note:
        lines.append(note)
    for title, rows in sections:
        lines += ["", B0 + title + B1]
        lines += [f"▫️ {t}: {p}{change_badge(pct)}" for t, p, pct in rows]
    lines += ["", FOOT]
    return "\n".join(lines)


# ───────────────────────── وضعیت ماندگار ─────────────────────────
# در گیت‌هاب اکشنز هر اجرا روی یک ماشین تازه است؛ وضعیت (شناسه‌ی پیام پین‌شده،
# نمونه‌های قیمت برای خلاصه‌ی روزانه و نمودار هفتگی، زمان آخرین پست‌ها) در
# فایل state.json نگه داشته می‌شود و اگر STATE_BRANCH تنظیم باشد، روی یک
# شاخه‌ی جدا در همین مخزن ذخیره می‌شود.

STATE_PATH = Path(__file__).with_name("state.json")
DAY = 86400


def _git(*args: str, inp: str | None = None) -> str:
    return subprocess.run(["git", *args], input=inp, capture_output=True, text=True,
                          check=True, cwd=STATE_PATH.parent).stdout.strip()


def load_state() -> dict:
    branch = env("STATE_BRANCH")
    if branch:
        try:
            _git("fetch", "-q", "--depth=1", "origin", branch)
            STATE_PATH.write_text(_git("show", "FETCH_HEAD:state.json"), encoding="utf-8")
        except subprocess.CalledProcessError as e:
            log.info("وضعیت قبلی روی شاخه‌ی %s پیدا نشد (%s)", branch, e.stderr.strip()[:200])
    st = {}
    if STATE_PATH.exists():
        try:
            st = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except ValueError:
            log.warning("state.json خراب بود؛ از نو شروع می‌شود")
    for k, v in (("live", {}), ("samples", []), ("hourly", {}), ("alerts", {}), ("marks", {})):
        st.setdefault(k, v)
    return st


def save_state(st: dict) -> None:
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, separators=(",", ":")),
                          encoding="utf-8")
    branch = env("STATE_BRANCH")
    if not branch:
        return
    try:
        blob = _git("hash-object", "-w", str(STATE_PATH))
        tree = _git("mktree", inp=f"100644 blob {blob}\tstate.json\n")
        commit = _git("commit-tree", tree, "-m", "bot state")
        _git("push", "-q", "-f", "origin", f"{commit}:refs/heads/{branch}")
    except subprocess.CalledProcessError as e:
        log.warning("ذخیره‌ی وضعیت روی گیت ناموفق: %s", e.stderr.strip()[:300])


def record(st: dict, now: float, vals: dict) -> None:
    st["samples"].append([round(now), {k: round(v, 4) for k, v in vals.items()}])
    st["samples"] = [s for s in st["samples"] if s[0] > now - 1.2 * DAY]
    for k in env("CHART_ITEMS", "gold18,usd").split(","):
        k = k.strip()
        if k not in vals:
            continue
        ser = st["hourly"].setdefault(k, [])
        if not ser or now - ser[-1][0] >= 50 * 60:
            ser.append([round(now), round(vals[k], 4)])
        st["hourly"][k] = [p for p in ser if p[0] > now - 8 * DAY]


# ───────────────────────── پیام‌رسان‌ها ─────────────────────────

class ApiError(RuntimeError):
    pass


class Target:
    """یک کانال مقصد. kind یکی از telegram، bale یا eitaa است."""

    def __init__(self, kind: str, name: str, token: str, chat: str, footer: str):
        self.kind, self.name, self.token, self.footer = kind, name, token, footer
        if kind == "telegram" and not chat.lstrip("-").isdigit() and not chat.startswith("@"):
            chat = "@" + chat  # آیدی بدون @ هم قبول شود
        self.chat = chat

    @property
    def can_edit(self) -> bool:
        return self.kind in ("telegram", "bale")

    # متن نهایی برای این پیام‌رسان
    def fmt(self, text: str) -> str:
        text = text.replace(FOOT, self.footer).rstrip()
        return render(text, {"telegram": "html", "bale": "md1"}.get(self.kind, "plain"))

    def _call(self, method: str, data: dict, files: dict | None = None):
        if self.kind == "eitaa":
            url = f"https://eitaayar.ir/api/{self.token}/{method}"
        else:
            base = {"telegram": "https://api.telegram.org/bot",
                    "bale": "https://tapi.bale.ai/bot"}[self.kind]
            url = f"{base}{self.token}/{method}"
        if files or self.kind == "eitaa":
            r = HTTP.post(url, data={k: str(v) for k, v in data.items()}, files=files,
                          timeout=60)
        else:
            r = HTTP.post(url, json=data, timeout=TIMEOUT)
        try:
            j = r.json()
        except ValueError:
            raise ApiError(f"{self.name}: HTTP {r.status_code} {r.text[:200]}")
        if not j.get("ok"):
            raise ApiError(f"{self.name}: {j.get('description') or j}")
        return j.get("result")

    def _common(self, silent: bool, pin: bool) -> dict:
        d: dict = {"chat_id": self.chat}
        if silent:
            d["disable_notification"] = 1 if self.kind == "eitaa" else True
        if pin and self.kind == "eitaa":
            d["pin"] = 1
        return d

    def send_text(self, text: str, silent=False, pin=False):
        d = self._common(silent, pin)
        d["text"] = self.fmt(text)
        if self.kind == "telegram":
            d.update(parse_mode="HTML", disable_web_page_preview=True)
        elif self.kind == "bale":
            d["parse_mode"] = "Markdown"
        res = self._call("sendMessage", d)
        mid = res.get("message_id") if isinstance(res, dict) else None
        if pin and mid and self.kind != "eitaa":
            self.pin(mid)
        return mid or True

    def send_photo(self, png: bytes, caption: str, silent=False, pin=False):
        d = self._common(silent, pin)
        d["caption"] = self.fmt(caption)
        if self.kind == "telegram":
            d["parse_mode"] = "HTML"
        elif self.kind == "bale":
            d["parse_mode"] = "Markdown"
        field, method = ("file", "sendFile") if self.kind == "eitaa" else ("photo", "sendPhoto")
        res = self._call(method, d, files={field: ("prices.png", png, "image/png")})
        mid = res.get("message_id") if isinstance(res, dict) else None
        if pin and mid and self.kind != "eitaa":
            self.pin(mid)
        return mid or True

    def edit_text(self, mid: int, text: str) -> None:
        d = {"chat_id": self.chat, "message_id": mid, "text": self.fmt(text)}
        if self.kind == "telegram":
            d.update(parse_mode="HTML", disable_web_page_preview=True)
        else:
            d["parse_mode"] = "Markdown"
        self._call("editMessageText", d)

    def pin(self, mid: int) -> None:
        try:
            self._call("pinChatMessage", {"chat_id": self.chat, "message_id": mid,
                                          "disable_notification": True})
        except Exception as e:
            log.warning("پین کردن در %s ناموفق (ربات باید دسترسی پین/ویرایش داشته باشد): %s",
                        self.name, e)


def targets() -> list[Target]:
    footer = env("FOOTER", "🆔 @gheymat_lahzei")
    out = []
    for kind, name, pre, foot in (
            ("bale", "بله", "BALE", footer),
            ("eitaa", "ایتا", "EITAA", footer),
            ("telegram", "تلگرام", "TELEGRAM", env("TELEGRAM_FOOTER", "🆔 @gheymat_lahzei_ir"))):
        token, chat = env(f"{pre}_TOKEN"), env(f"{pre}_CHAT_ID")
        if token and chat:
            out.append(Target(kind, name, token, chat, foot))
    return out


def retry(fn, what: str, tries: int = 3):
    for attempt in range(tries):
        try:
            return fn()
        except ApiError as e:
            log.warning("%s ناموفق: %s", what, e)
            return None  # خطای خود API با تکرار درست نمی‌شود
        except Exception as e:
            log.warning("%s ناموفق (تلاش %d): %s", what, attempt + 1, e)
            time.sleep(5 * (attempt + 1))
    return None


# ───────────────────────── پیام پین‌شده‌ی زنده ─────────────────────────
# در تلگرام و بله یک پیام پین می‌شود و هر بار فقط ویرایش می‌شود (پست جدیدی
# نمی‌سازد). ایتایار متد ویرایش ندارد؛ آنجا پست ساعتی پین می‌شود.

def update_live(t: Target, text: str, st: dict) -> bool:
    """True یعنی پیام زنده در این کانال کار می‌کند."""
    rec = st["live"].get(t.kind) or {}
    if rec.get("disabled"):
        return False
    now = time.time()
    if rec.get("mid") and rec.get("chat") == t.chat:
        try:
            t.edit_text(rec["mid"], text)
            rec["ok"] = True
            return True
        except ApiError as e:
            if "not modified" in str(e).lower():
                return True
            log.warning("ویرایش پیام پین‌شده در %s ناموفق: %s", t.name, e)
            if now - rec.get("created", 0) < 3 * 3600:
                return True  # تازه ساخته شده؛ برای جلوگیری از پست‌های تکراری صبر می‌کنیم
            if not rec.get("ok") and rec.get("tries", 0) >= 2:
                rec["disabled"] = str(e)[:200]
                log.error("ویرایش در %s پشتیبانی نمی‌شود؛ پست ساعتی پین می‌شود.", t.name)
                return False
        except Exception as e:
            log.warning("ویرایش پیام پین‌شده در %s: %s", t.name, e)
            return True
    mid = retry(lambda: t.send_text(text, silent=True), f"ساخت پیام زنده در {t.name}")
    if not mid or mid is True:
        return False
    t.pin(mid)
    st["live"][t.kind] = {"mid": mid, "chat": t.chat, "created": round(now),
                          "tries": rec.get("tries", 0) + 1, "ok": rec.get("ok", False)}
    return True


# ───────────────────────── برنامه‌ی پست‌ها ─────────────────────────

def hour_set(spec: str) -> set[int]:
    """«0-7» یا «9,13,17» → مجموعه‌ی ساعت‌ها"""
    out: set[int] = set()
    for part in filter(None, (p.strip() for p in spec.split(","))):
        if "-" in part:
            a_, b_ = (int(x) for x in part.split("-"))
            out |= set(range(a_, b_)) if a_ < b_ else set(range(a_, 24)) | set(range(0, b_))
        else:
            out.add(int(part))
    return out


def plan_post(now: datetime, st: dict) -> str | None:
    """نوع پست جدید این ساعت: text، card، summary، chart یا None."""
    hour_key = now.strftime("%Y-%m-%d %H")
    if st["marks"].get("post") == hour_key:
        return None
    h, today = now.hour, now.strftime("%Y-%m-%d")
    if h in hour_set(env("QUIET_HOURS")):
        return None
    if h in hour_set(env("NIGHT_HOURS", "0-7")) and h % int(env("NIGHT_EVERY", "3")):
        return None
    if (now.weekday() == 4 and h == int(env("CHART_HOUR", "12"))
            and st["marks"].get("chart") != today):
        return "chart"
    if h == int(env("SUMMARY_HOUR", "20")) and st["marks"].get("summary") != today:
        return "summary"
    if h in hour_set(env("IMAGE_HOURS", "10,14,18,22")):
        return "card"
    return "text"


def is_night(now: datetime) -> bool:
    return now.hour in hour_set(env("NIGHT_HOURS", "0-7"))


# ───────────────────────── هشدار نوسان ─────────────────────────

def check_alerts(st: dict, now: float, vals: dict) -> list[str]:
    pct_lim = float(env("ALERT_PCT", "1"))
    window = int(env("ALERT_WINDOW_MIN", "60")) * 60
    out = []
    for k in (x.strip() for x in env("ALERT_ITEMS", "usd,gold18").split(",")):
        if k not in vals:
            continue
        old = [s for s in st["samples"] if now - window - 300 <= s[0] <= now - 5 * 60 and k in s[1]]
        if not old:
            continue
        ref_ts, ref = old[0][0], old[0][1][k]
        cur = vals[k]
        pct = (cur - ref) / ref * 100
        if abs(pct) < pct_lim or abs(pct) > 15:  # بیش از ۱۵٪ یعنی داده‌ی خراب
            continue
        last = st["alerts"].get(k, {})
        if now - last.get("ts", 0) < 90 * 60 and (pct > 0) == (last.get("pct", 0) > 0):
            continue
        st["alerts"][k] = {"ts": round(now), "pct": round(pct, 3)}
        mins = max(10, round((now - ref_ts) / 600) * 10)
        title = CATALOG[k]["title"].replace(" (گرم)", "")
        up = pct > 0
        out.append("\n".join([
            B0 + f"🚨 نوسان شدید {title}" + B1,
            "",
            f"{'📈' if up else '📉'} {title} در {fa(mins)} دقیقه‌ی گذشته "
            f"{fa(f'{abs(pct):.1f}')}٪ {'گران' if up else 'ارزان'} شد.",
            f"▫️ از {fmt_item(k, ref)} به {B0}{fmt_item(k, cur)}{B1} تومان",
            "",
            f"🗓 {persian_now()}",
            "",
            FOOT,
        ]))
    return out


# ───────────────────────── خلاصه‌ی روزانه ─────────────────────────

def build_summary(st: dict, now: datetime, items: list[str], vals: dict) -> str | None:
    start = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    today = [s for s in st["samples"] if s[0] >= start]
    if len(today) < 3:
        return None
    moves = []
    for k in items:
        if k not in vals:
            continue
        seq = [s[1][k] for s in today if k in s[1]]
        if not seq or not seq[0]:
            continue
        moves.append((k, (vals[k] - seq[0]) / seq[0] * 100, seq[0], min(seq + [vals[k]]),
                      max(seq + [vals[k]])))
    if not moves:
        return None
    n = int(env("SUMMARY_TOP", "3"))
    ups = sorted((m for m in moves if m[1] >= 0.05), key=lambda m: -m[1])[:n]
    downs = sorted((m for m in moves if m[1] <= -0.05), key=lambda m: m[1])[:n]
    unit = lambda k: "تومان" if CATALOG[k]["mul"] == R2T else "دلار"

    def line(m):
        k, pct, first = m[0], m[1], m[2]
        sign = "+" if pct > 0 else "−"
        return (f"▫️ {CATALOG[k]['title']}: {sign}{fa(f'{abs(pct):.2f}')}٪"
                f"  (از {fmt_item(k, first)} به {fmt_item(k, vals[k])})")

    lines = [B0 + "📋 خلاصه‌ی بازار امروز" + B1, f"🗓 {persian_now()}", ""]
    lines += [B0 + "🟢 بیشترین رشد" + B1] + ([line(m) for m in ups] or ["▫️ موردی نبود"])
    lines += ["", B0 + "🔴 بیشترین افت" + B1] + ([line(m) for m in downs] or ["▫️ موردی نبود"])
    rng = [m for m in moves if m[0] in ("usd", "gold18", "coin")]
    if rng:
        lines += ["", B0 + "↕️ کف و سقف امروز" + B1]
        lines += [f"▫️ {CATALOG[k]['title']}: {fmt_item(k, lo)} تا {fmt_item(k, hi)} {unit(k)}"
                  for k, _, _, lo, hi in rng]
    lines += ["", "(تغییر نسبت به اولین قیمت امروز)", "", FOOT]
    return "\n".join(lines)


# ───────────────────────── تصویرها ─────────────────────────

SHORT_DAYS = ["دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه"]


def make_card(items: list[str], data: dict, footer: str) -> bytes | None:
    import cards
    if not cards.can_render():
        log.warning("موتور چیدمان فارسی (raqm) در دسترس نیست؛ به‌جای تصویر متن فرستاده می‌شود.")
        return None
    secs = [(t, [(a, b) for a, b, _ in rows]) for t, rows in price_sections(items, data)]
    handle = footer.replace("🆔", "").strip()
    return cards.price_card(secs, env("HEADER", "📊 قیمت لحظه‌ای بازار").lstrip("📊 "),
                            persian_now(), handle)


def make_chart(st: dict, footer: str) -> bytes | None:
    import cards
    if not cards.can_render():
        return None
    series = []
    for k in (x.strip() for x in env("CHART_ITEMS", "gold18,usd").split(",")):
        pts = [(p[0], p[1]) for p in st["hourly"].get(k, []) if p[0] > time.time() - 7 * DAY]
        if k in CATALOG:
            unit = "تومان" if CATALOG[k]["mul"] == R2T else "دلار"
            series.append((CATALOG[k]["title"], unit, pts))
    spans = [s[2][-1][0] - s[2][0][0] for s in series if len(s[2]) > 1]
    if not spans or max(spans) < 3 * DAY:
        log.info("داده‌ی نمودار هفتگی هنوز کمتر از ۳ روز است")
        return None
    return cards.weekly_chart(
        series, "نمودار هفتگی", persian_now(), footer.replace("🆔", "").strip(),
        fmt=lambda v: fmt_num(v),
        day_label=lambda ts: SHORT_DAYS[datetime.fromtimestamp(ts, TEHRAN).weekday()])


# ───────────────────────── یک دور اجرا ─────────────────────────

def tick(st: dict, dry: bool = False, force: str | None = None) -> None:
    items = selected_items()
    data = collect_prices(items)
    vals = values_of(items, data)
    now_ts = time.time()
    now = datetime.now(TEHRAN)
    if not vals:
        log.error("هیچ قیمتی دریافت نشد؛ پستی ارسال نشد.")
        return
    header = env("HEADER", "📊 قیمت لحظه‌ای بازار")
    tg = [] if dry else targets()
    alerts = check_alerts(st, now_ts, vals)
    record(st, now_ts, vals)

    def out_text(text, silent=False, pin=False, only=None):
        if dry:
            print(render(text.replace(FOOT, env("FOOTER", "🆔 @gheymat_lahzei")), "plain"))
            print("─" * 30)
            return
        for t in tg:
            if only is None or t in only:
                if retry(lambda: t.send_text(text, silent=silent, pin=pin), f"ارسال به {t.name}"):
                    log.info("ارسال به %s انجام شد", t.name)

    def out_photo(png_for, caption, silent=False, pin=False):
        for t in tg:
            png = png_for(t.footer)
            if png is None:
                return False
            retry(lambda: t.send_photo(png, caption, silent=silent, pin=pin),
                  f"ارسال تصویر به {t.name}")
            log.info("تصویر به %s ارسال شد", t.name)
        if dry:
            png = png_for(env("FOOTER", "🆔 @gheymat_lahzei"))
            if png is None:
                return False
            p = STATE_PATH.with_name(f"preview-{force or 'img'}.png")
            p.write_bytes(png)
            print("تصویر ذخیره شد:", p)
        return True

    # ۱) هشدار نوسان
    for a in alerts:
        if not dry or force is None:
            out_text(a, silent=is_night(now))

    # ۲) پیام پین‌شده‌ی زنده (هر دور)
    note = f"🔄 به‌روزرسانی خودکار هر {fa(env('INTERVAL_MINUTES', '10'))} دقیقه"
    live_text = build_post(items, data, "📌 " + header.lstrip("📊 "), note)
    live_ok = {}
    if env("LIVE_MESSAGE", "1") != "0" and live_text:
        if dry and force in (None, "live"):
            out_text(live_text)
        for t in tg:
            if t.can_edit:
                live_ok[t.kind] = update_live(t, live_text, st)

    # ۳) پست جدید ساعتی
    kind = force if force and force != "live" else (None if dry else plan_post(now, st))
    if not kind:
        return
    silent = is_night(now)
    # در جایی که پیام زنده کار نمی‌کند (ایتا)، پست ساعتی پین می‌شود
    pin_here = [t for t in tg if not live_ok.get(t.kind)
                and (t.kind != "eitaa" or env("EITAA_PIN", "1") != "0")]
    text = build_post(items, data, header)
    done = False
    if kind == "chart":
        cap = "\n".join([B0 + "📈 نمودار هفتگی طلا و دلار" + B1, f"🗓 {persian_now()}", "", FOOT])
        done = out_photo(lambda f: make_chart(st, f), cap, silent)
        if done or dry:
            st["marks"]["chart"] = now.strftime("%Y-%m-%d")
    elif kind == "summary":
        s = build_summary(st, now, items, vals)
        if s:
            out_text(s, silent)
            done = True
        st["marks"]["summary"] = now.strftime("%Y-%m-%d")
    elif kind == "card":
        cap = "\n".join([B0 + header + B1, f"🗓 {persian_now()}", "", FOOT])
        done = out_photo(lambda f: make_card(items, data, f), cap, silent)
    if not done and text:
        if dry:
            out_text(text)
        else:
            for t in tg:
                pin = t in pin_here
                retry(lambda: t.send_text(text, silent=silent, pin=pin), f"ارسال به {t.name}")
                log.info("پست ساعتی به %s ارسال شد%s", t.name, " و پین شد" if pin else "")
    if not dry:
        st["marks"]["post"] = now.strftime("%Y-%m-%d %H")


# ───────────────────────── نمونه برای تلگرام ─────────────────────────

def send_samples() -> None:
    """از هر نوع پست یک نمونه فقط به تلگرام می‌فرستد (برای دیدن ظاهر).
    خلاصه، نمودار و هشدار با داده‌ی نمایشی ساخته می‌شوند و این در متن آمده است."""
    import random
    tg = [t for t in targets() if t.kind == "telegram"]
    if not tg:
        sys.exit("تلگرام تنظیم نشده")
    t = tg[0]
    items = selected_items()
    data = collect_prices(items)
    vals = values_of(items, data)
    now_ts, now = time.time(), datetime.now(TEHRAN)
    tag = B0 + "🧪 نمونه‌ی آزمایشی" + B1 + "\n"
    demo = "\n(داده‌ی نمایشی برای دیدن ظاهر؛ قیمت واقعی نیست)"
    header = env("HEADER", "📊 قیمت لحظه‌ای بازار")
    rnd = random.Random(7)

    def send(text):
        retry(lambda: t.send_text(tag + text), "ارسال نمونه")

    # ۱) پیام پین‌شده‌ی زنده
    note = f"🔄 به‌روزرسانی خودکار هر {fa(env('INTERVAL_MINUTES', '10'))} دقیقه"
    send(build_post(items, data, "📌 " + header.lstrip("📊 "), note))
    # ۲) پست ساعتی
    send(build_post(items, data, header))
    # ۳) کارت تصویری
    png = make_card(items, data, t.footer)
    if png:
        retry(lambda: t.send_photo(png, tag + "کارت تصویری قیمت‌ها\n\n" + FOOT), "ارسال کارت")
    # ۴) خلاصه‌ی روز با داده‌ی نمایشی
    start = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    fake = {"samples": [[start + 60, {k: v * (1 + rnd.uniform(-0.02, 0.02)) for k, v in vals.items()}],
                        [start + 120, vals], [start + 180, vals]]}
    s = build_summary(fake, now, items, vals)
    if s:
        send(s.replace("\n\n" + FOOT, demo + "\n\n" + FOOT))
    # ۵) نمودار هفتگی با داده‌ی نمایشی
    hourly = {}
    for k in ("gold18", "usd"):
        if k in vals:
            v, ser = vals[k], []
            for i in range(7 * 24):
                ser.append([now_ts - i * 3600, v])
                v /= 1 + rnd.gauss(0.0003, 0.003)
            hourly[k] = ser[::-1]
    png = make_chart({"hourly": hourly}, t.footer)
    if png:
        cap = "\n".join([tag + "📈 نمودار هفتگی طلا و دلار" + demo, "", FOOT])
        retry(lambda: t.send_photo(png, cap), "ارسال نمودار")
    # ۶) هشدار نوسان با داده‌ی نمایشی
    st = {"samples": [[now_ts - 3000, {k: v / 1.013 for k, v in vals.items()}]], "alerts": {}}
    for a in check_alerts(st, now_ts, vals)[:1]:
        send(a.replace("\n\n" + FOOT, demo + "\n\n" + FOOT))
    log.info("نمونه‌ها به تلگرام فرستاده شد")


# ───────────────────────── اجرا ─────────────────────────

def main() -> None:
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="یک دور اجرا و خروج (برای cron)")
    ap.add_argument("--loop", type=float, metavar="MIN",
                    help="اجرای پیوسته به مدت MIN دقیقه (برای گیت‌هاب اکشنز)")
    ap.add_argument("--dry-run", action="store_true", help="فقط نمایش متن، بدون ارسال")
    ap.add_argument("--preview", choices=["live", "text", "card", "summary", "chart", "alert"],
                    help="ساخت نمونه‌ی یک نوع پست بدون ارسال (تصویرها کنار bot.py ذخیره می‌شوند)")
    ap.add_argument("--sample", action="store_true",
                    help="از هر نوع پست یک نمونه فقط به تلگرام بفرستد")
    ap.add_argument("--check", action="store_true",
                    help="نمایش همه‌ی کلیدهای دریافتی از منابع برای عیب‌یابی")
    ap.add_argument("--bale-chats", action="store_true",
                    help="نمایش شناسه‌ی عددی کانال‌ها/گروه‌هایی که ربات بله در آن‌هاست")
    a = ap.parse_args()
    if a.bale_chats:
        return debug_chats()
    if a.check:
        for name, fn in SOURCES.items():
            try:
                for k, v in sorted(fn().items()):
                    print(k, v)
            except Exception as e:
                print(f"{name}: خطا - {e}")
        return
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if a.sample:
        return send_samples()
    if a.preview or a.dry_run:
        st = load_state() if a.preview in ("summary", "chart") else {
            "live": {}, "samples": [], "hourly": {}, "alerts": {}, "marks": {}}
        if a.preview == "alert":
            items = selected_items()
            vals = values_of(items, collect_prices(items))
            st["samples"] = [[time.time() - 3000, {k: v / 1.013 for k, v in vals.items()}]]
            for x in check_alerts(st, time.time(), vals):
                print(render(x.replace(FOOT, env("FOOTER", "🆔 @gheymat_lahzei")), "plain"))
            return
        tick(st, dry=True, force=a.preview or "text")
        return

    if not targets():
        sys.exit("هیچ کانالی تنظیم نشده. فایل .env را از روی .env.example بسازید.")

    st = load_state()
    if a.once:
        tick(st)
        save_state(st)
        return

    interval = max(1, int(env("INTERVAL_MINUTES", "10"))) * 60
    deadline = time.time() + a.loop * 60 if a.loop else None
    log.info("ربات شروع شد؛ هر %d دقیقه یک دور.", interval // 60)
    while True:
        try:
            tick(st)
        except Exception:
            log.exception("خطای پیش‌بینی‌نشده")
        save_state(st)
        # هم‌تراز با دقیقه‌های رُند (مثلاً ۱۰:۰۰، ۱۰:۱۰، ...)
        wait = interval - (time.time() % interval) + 5
        if deadline and time.time() + wait > deadline:
            log.info("پایان این نوبت؛ اجرای بعدی گیت‌هاب ادامه می‌دهد.")
            return
        time.sleep(wait)


def debug_chats() -> None:
    if env("TELEGRAM_TOKEN"):
        tg = f"https://api.telegram.org/bot{env('TELEGRAM_TOKEN')}"
        me = HTTP.get(f"{tg}/getMe", timeout=TIMEOUT).json()
        print("TG getMe:", me.get("ok"), me.get("result", {}).get("username"))
        print("TG getChat:", HTTP.post(f"{tg}/getChat", json={"chat_id": env("TELEGRAM_CHAT_ID")},
                                       timeout=TIMEOUT).json())
    if not env("BALE_TOKEN"):
        return
    base = f"https://tapi.bale.ai/bot{env('BALE_TOKEN')}"
    j = HTTP.get(f"{base}/getUpdates", timeout=TIMEOUT).json()
    print("getMe:", HTTP.get(f"{base}/getMe", timeout=TIMEOUT).json().get("result", {}).get("username"))
    seen = set()
    for u in j.get("result", []):
        for k in ("channel_post", "message", "my_chat_member", "edited_channel_post"):
            chat = (u.get(k) or {}).get("chat")
            if chat and chat["id"] not in seen:
                seen.add(chat["id"])
                print("CHAT", chat["id"], chat.get("type"), chat.get("username"))
    if not seen:
        print("هیچ چتی پیدا نشد:", str(j)[:300])
    for cid in filter(None, [env("BALE_CHAT_ID")]):
        print("getChat", HTTP.post(f"{base}/getChat", json={"chat_id": cid},
                                   timeout=TIMEOUT).json())


if __name__ == "__main__":
    main()
