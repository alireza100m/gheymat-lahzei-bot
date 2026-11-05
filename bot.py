#!/usr/bin/env python3
"""
ربات انتشار قیمت در کانال‌های ایتا و بله

هر چند دقیقه قیمت طلا، سکه، دلار، تتر، بیت‌کوین، اتریوم، نفت، نقره و انس‌ها را
می‌گیرد و یک پست فارسی مرتب در کانال‌ها منتشر می‌کند.

اجرا:
    python bot.py            # اجرای دائمی با فاصله‌ی تنظیم‌شده
    python bot.py --once     # یک بار ارسال و خروج (برای cron)
    python bot.py --dry-run  # فقط نمایش متن پست، بدون ارسال
    python bot.py --check    # نمایش همه‌ی داده‌های خام منابع (عیب‌یابی)
"""
from __future__ import annotations

import argparse
import logging
import os
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
# هر قلم: عنوان، ایموجی، واحد، تعداد اعشار، ضریب (مثلاً ریال→تومان = 0.1)،
# و فهرست منابع به ترتیب اولویت. اولین منبعی که جواب بدهد استفاده می‌شود.
# برای حذف یک قلم کافی است کلیدش را از ITEMS در .env بردارید.

R2T = 0.1  # ریال به تومان

CATALOG = {
    "usd":       dict(title="دلار آزاد", emoji="💵", unit="تومان", dec=0, mul=R2T,
                      src=["tgju:price_dollar_rl"]),
    "usdt":      dict(title="تتر", emoji="💲", unit="تومان", dec=0, mul=R2T,
                      src=["nobitex:usdt-rls", "tgju:crypto-tether-irr"]),
    "gold18":    dict(title="طلای ۱۸ عیار (گرم)", emoji="🟡", unit="تومان", dec=0, mul=R2T,
                      src=["tgju:geram18"]),
    "coin":      dict(title="سکه امامی", emoji="🪙", unit="تومان", dec=0, mul=R2T,
                      src=["tgju:sekee"]),
    "gold_oz":   dict(title="انس طلا", emoji="🥇", unit="دلار", dec=2, mul=1,
                      src=["tgju:ons", "goldapi:XAU"]),
    "silver_oz": dict(title="انس نقره", emoji="🥈", unit="دلار", dec=2, mul=1,
                      src=["tgju:silver", "goldapi:XAG"]),
    "silver":    dict(title="نقره ۹۹۹ (گرم)", emoji="⚪️", unit="تومان", dec=0, mul=R2T,
                      src=["tgju:silver_999"]),
    "oil":       dict(title="نفت برنت", emoji="🛢", unit="دلار", dec=2, mul=1,
                      src=["tgju:oil_brent", "tgju:oil"]),
    "btc":       dict(title="بیت‌کوین", emoji="₿", unit="دلار", dec=0, mul=1,
                      src=["nobitex:btc-usdt", "coingecko:bitcoin", "tgju:crypto-bitcoin"]),
    "eth":       dict(title="اتریوم", emoji="💎", unit="دلار", dec=0, mul=1,
                      src=["nobitex:eth-usdt", "coingecko:ethereum", "tgju:crypto-ethereum"]),
}

DEFAULT_ITEMS = "usd,usdt,gold18,coin,gold_oz,silver_oz,silver,oil,btc,eth"


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
    return f" {arrow}{fa(f'{abs(pct):.2f}')}٪"


def build_post(items: list[str], data: dict, header: str, footer: str) -> str | None:
    lines = [header, f"🗓 {persian_now()}", ""]
    found = 0
    for it in items:
        c = CATALOG[it]
        val = next((data[s] for s in c["src"] if s in data and data[s][0]), None)
        if val is None:
            log.warning("قیمت «%s» پیدا نشد", c["title"])
            continue
        price, pct = val
        found += 1
        lines.append(f"{c['emoji']} {c['title']}: {fmt_num(price * c['mul'], c['dec'])} "
                     f"{c['unit']}{change_badge(pct)}")
    if not found:
        return None
    if footer:
        lines += ["", footer]
    return "\n".join(lines)


# ───────────────────────── ارسال به کانال‌ها ─────────────────────────

def send_bale(token: str, chat_id: str, text: str) -> None:
    r = HTTP.post(f"https://tapi.bale.ai/bot{token}/sendMessage",
                  json={"chat_id": chat_id, "text": text}, timeout=TIMEOUT)
    j = r.json()
    if not j.get("ok"):
        raise RuntimeError(f"Bale: {j}")


def send_eitaa(token: str, chat_id: str, text: str) -> None:
    r = HTTP.post(f"https://eitaayar.ir/api/{token}/sendMessage",
                  data={"chat_id": chat_id, "text": text}, timeout=TIMEOUT)
    j = r.json()
    if not j.get("ok"):
        raise RuntimeError(f"Eitaa: {j}")


def send_telegram(token: str, chat_id: str, text: str) -> None:
    r = HTTP.post(f"https://api.telegram.org/bot{token}/sendMessage",
                  json={"chat_id": chat_id, "text": text}, timeout=TIMEOUT)
    j = r.json()
    if not j.get("ok"):
        raise RuntimeError(f"Telegram: {j}")


def publish(text: str) -> None:
    # پانویس تلگرام جداست، چون آیدی کانال تلگرام با بقیه فرق دارد
    default_footer = env("FOOTER", "🆔 @gheymat_lahzei")
    tg_footer = env("TELEGRAM_FOOTER", "🆔 @gheymat_lahzei_ir")
    tg_text = (text[: -len(default_footer)] + tg_footer
               if default_footer and text.endswith(default_footer) else text)
    targets = [
        ("بله", send_bale, env("BALE_TOKEN"), env("BALE_CHAT_ID")),
        ("ایتا", send_eitaa, env("EITAA_TOKEN"), env("EITAA_CHAT_ID")),
        ("تلگرام", send_telegram, env("TELEGRAM_TOKEN"), env("TELEGRAM_CHAT_ID")),
    ]
    for name, fn, token, chat in targets:
        msg = tg_text if fn is send_telegram else text
        if not (token and chat):
            continue
        for attempt in range(3):
            try:
                fn(token, chat, msg)
                log.info("ارسال به %s انجام شد", name)
                break
            except Exception as e:
                log.warning("ارسال به %s ناموفق (تلاش %d): %s", name, attempt + 1, e)
                time.sleep(5 * (attempt + 1))


# ───────────────────────── حلقه‌ی اصلی ─────────────────────────

def run_once(dry: bool) -> None:
    items = [i.strip() for i in env("ITEMS", DEFAULT_ITEMS).split(",") if i.strip()]
    bad = [i for i in items if i not in CATALOG]
    if bad:
        log.error("اقلام ناشناخته در ITEMS: %s (مجاز: %s)", bad, ",".join(CATALOG))
        items = [i for i in items if i in CATALOG]
    header = env("HEADER", "📊 قیمت لحظه‌ای بازار")
    footer = env("FOOTER", "🆔 @gheymat_lahzei")
    text = build_post(items, collect_prices(items), header, footer)
    if text is None:
        log.error("هیچ قیمتی دریافت نشد؛ پستی ارسال نشد.")
        return
    if dry:
        print(text)
    else:
        publish(text)


def in_quiet_hours() -> bool:
    quiet = env("QUIET_HOURS")  # مثلاً 0-7 یعنی از ۰ تا ۷ صبح پست نشود
    if not quiet:
        return False
    a_, b_ = (int(x) for x in quiet.split("-"))
    hour = datetime.now(TEHRAN).hour
    return (a_ <= hour < b_) if a_ < b_ else (hour >= a_ or hour < b_)


def main() -> None:
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="یک بار ارسال و خروج")
    ap.add_argument("--dry-run", action="store_true", help="فقط نمایش متن، بدون ارسال")
    ap.add_argument("--check", action="store_true",
                    help="نمایش همه‌ی کلیدهای دریافتی از منابع برای عیب‌یابی")
    ap.add_argument("--bale-chats", action="store_true",
                    help="نمایش شناسه‌ی عددی کانال‌ها/گروه‌هایی که ربات بله در آن‌هاست")
    a = ap.parse_args()
    if a.bale_chats and env("TELEGRAM_TOKEN"):
        tg = f"https://api.telegram.org/bot{env('TELEGRAM_TOKEN')}"
        me = HTTP.get(f"{tg}/getMe", timeout=TIMEOUT).json()
        print("TG getMe:", me.get("ok"), me.get("result", {}).get("username"), me.get("description"))
        cid = env("TELEGRAM_CHAT_ID")
        print("TG chat_id len:", len(cid), "starts@:", cid.startswith("@"),
              "has_space:", cid != cid.strip() or " " in cid)
        print("TG getChat:", HTTP.post(f"{tg}/getChat", json={"chat_id": cid},
                                       timeout=TIMEOUT).json())
        for u in HTTP.get(f"{tg}/getUpdates", timeout=TIMEOUT).json().get("result", []):
            for k in ("channel_post", "my_chat_member", "message"):
                chat = (u.get(k) or {}).get("chat")
                if chat:
                    print("TG CHAT", chat["id"], chat.get("type"), chat.get("username"))
    if a.bale_chats:
        r = HTTP.get(f"https://tapi.bale.ai/bot{env('BALE_TOKEN')}/getUpdates", timeout=TIMEOUT)
        j = r.json()
        print("getMe:", HTTP.get(f"https://tapi.bale.ai/bot{env('BALE_TOKEN')}/getMe",
                                 timeout=TIMEOUT).json().get("result", {}).get("username"))
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
            print("getChat", HTTP.post(f"https://tapi.bale.ai/bot{env('BALE_TOKEN')}/getChat",
                                       json={"chat_id": cid}, timeout=TIMEOUT).json())
        return
    if a.check:
        for name, fn in SOURCES.items():
            try:
                for k, v in sorted(fn().items()):
                    print(k, v)
            except Exception as e:
                print(f"{name}: خطا - {e}")
        return
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if not a.dry_run and not ((env("BALE_TOKEN") and env("BALE_CHAT_ID"))
                              or (env("EITAA_TOKEN") and env("EITAA_CHAT_ID"))
                              or (env("TELEGRAM_TOKEN") and env("TELEGRAM_CHAT_ID"))):
        sys.exit("هیچ کانالی تنظیم نشده. فایل .env را از روی .env.example بسازید.")

    if a.once or a.dry_run:
        if a.once and in_quiet_hours():
            log.info("ساعت سکوت است؛ پستی ارسال نشد.")
            return
        run_once(a.dry_run)
        return

    interval = max(1, int(env("INTERVAL_MINUTES", "10"))) * 60
    log.info("ربات شروع شد؛ هر %d دقیقه یک پست.", interval // 60)
    while True:
        if not in_quiet_hours():
            try:
                run_once(False)
            except Exception:
                log.exception("خطای پیش‌بینی‌نشده")
        # هم‌تراز با دقیقه‌های رُند (مثلاً ۱۰:۰۰، ۱۰:۱۰، ...)
        time.sleep(interval - (time.time() % interval))


if __name__ == "__main__":
    main()
