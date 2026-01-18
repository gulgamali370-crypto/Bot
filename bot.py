#!/usr/bin/env python3
"""
Dr OTP Receiver Bot - Interactive allocation: 3 numbers per country, country-only discovery,
compact UI, strict expire detection, per-number polling jobs.

Flow:
- /get -> choose service (WhatsApp, Facebook, Instagram, Other)
- Bot discovers countries for that service (from /info) and shows country list
- User picks a country -> bot allocates up to 3 numbers from different prefixes for that country
- Bot starts a polling job per allocated number; forwards OTPs to user + FORWARD_CHAT_ID
- Status displayed per allocation; "Expired" set only when provider explicitly marks expired/failed
"""
from __future__ import annotations

import os
import re
import json
import time
import logging
import html
import threading
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List, Tuple
import sys

import requests
from telegram import (
    Bot,
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ParseMode,
)
from telegram.ext import (
    Updater,
    CommandHandler,
    CallbackContext,
    CallbackQueryHandler,
    MessageHandler,
    Filters,
)
from telegram.error import Unauthorized, NetworkError, Conflict

# ---------- CONFIG ----------
BOT_TOKEN = os.getenv("BOT_TOKEN", "8338765935:AAHnYQZjI7vlPf26RkaXnioKenEMp7RauPU").strip()
MNIT_API_KEY = os.getenv("MNIT_API_KEY", "M_WH9Q3U88V").strip()
FORWARD_CHAT_ID = int(os.getenv("FORWARD_CHAT_ID", "-1003379113224"))
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "10"))

ALLOCATE_URL = "https://x.mnitnetwork.com/mapi/v1/mdashboard/getnum/number"
INFO_URL = "https://x.mnitnetwork.com/mapi/v1/mdashboard/getnum/info"
HEADERS = {"Content-Type": "application/json", "mapikey": MNIT_API_KEY}

STATE_FILE = "state.json"

# ---------- UI & templates (compact bottom menu) ----------
CARD_SEPARATOR = "━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Only these services shown
SERVICES = ["WhatsApp", "Facebook", "Instagram", "Other"]

BUTTON_LABELS = {
    "copy": "📋 Copy",
    "change": "🔁 Change",
    "cancel": "❌ Cancel",
    "back": "⬅ Back",
    "copyotp": "🔐 CopyOTP",
}

MSG_HELPER = "Tip: Use /get to request numbers (interactive)."
MSG_COPY_CONFIRM = "✅ Sent — long-press to copy."

MAIN_MENU_KEYS = [
    ["📲 Get Number", "📥 Active"],
    ["📜 History", "⚙ Settings"],
]

MSG_ALLOCATION_CARD = (
    "{sep}\n"
    "📱 Country: {country}\n"
    "📞 Phone: {pretty_number}\n"
    "🔢 Range: {range}\n"
    "{sep}\n"
    "⏳ Status: Waiting for OTP"
)

MSG_OTP_CARD = (
    "{sep}\n"
    "🔔 OTP Received\n"
    "{sep}\n"
    "📩 Code: <code>{otp}</code>\n"
    "📞 Number: {pretty_number}\n"
    "🗺 Country: {country}\n"
    "⏰ Time: {time}\n"
    "{sep}\n"
    "Message:\n"
    "{sms_text}"
)

MSG_EXPIRED = (
    "{sep}\n"
    "❌ OTP Expired\n"
    "{sep}\n"
    "{pretty_number}\n\n"
    "This number has been marked Expired by the provider.\n"
)

MSG_ALLOCATION_ERROR = (
    "{sep}\n"
    "⚠️ Allocation Failed\n"
    "{sep}\n"
    "{short_error_message}\n"
)

# ---------- Logging ----------
logging.basicConfig(format="[%(asctime)s] %(levelname)s: %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- state ----------
# state[chat_id] = { "allocations": [ {id, range, number, digits, last_variants, country, allocated_at, status, otp} ] }
state: Dict[str, Dict[str, Any]] = {}
jobs_registry: Dict[str, Any] = {}  # key = f"{chat_id}:{alloc_id}" -> job

# ---------- persistence ----------
def load_state():
    global state
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                state = json.load(f)
            logger.info("Loaded state for %d chats", len(state))
    except Exception as e:
        logger.warning("Failed to load state.json: %s", e)


def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        logger.warning("Failed to save state.json: %s", e)


# ---------- utilities ----------
def digits_only(s: Optional[str]) -> str:
    if not s:
        return ""
    return re.sub(r"\D", "", str(s))


def last_n_variants(s: str, lengths: Optional[List[int]] = None) -> List[str]:
    if lengths is None:
        lengths = [6, 7, 8, 9]
    d = digits_only(s)
    return [d[-n:] for n in lengths if len(d) >= n]


def flatten_values(x: Any) -> str:
    if isinstance(x, dict):
        parts = []
        for v in x.values():
            parts.append(flatten_values(v))
        return " ".join(p for p in parts if p)
    if isinstance(x, list):
        return " ".join(flatten_values(i) for i in x)
    return str(x)


def extract_message_text(entry: Dict[str, Any]) -> str:
    for k in ("message", "sms", "msg", "text", "body", "sms_text", "content", "raw"):
        v = entry.get(k)
        if v:
            return flatten_values(v)
    # deep search
    def search(o):
        if isinstance(o, dict):
            for kk, vv in o.items():
                if isinstance(vv, (str, int, float)) and kk.lower() in ("message","sms","msg","text","body","content","description"):
                    return str(vv)
                res = search(vv)
                if res:
                    return res
        if isinstance(o, list):
            for i in o:
                r = search(i)
                if r:
                    return r
        return ""
    nested = search(entry)
    if nested:
        return nested
    return flatten_values(entry)


def extract_otp_from_text(text: str) -> Optional[str]:
    if not text:
        return None
    t = re.sub(r"[|:]+", " ", text)
    m = re.search(r"\b(\d{4,8})\b", t)
    if m:
        return m.group(1)
    m2 = re.search(r"[<#>]{1,3}\s*([0-9]{4,8})", t)
    if m2:
        return m2.group(1)
    return None


def format_pretty_number(number: str) -> str:
    if not number:
        return ""
    s = str(number).strip()
    plus = ""
    if s.startswith("+"):
        plus = "+"
        s = s[1:]
    d = re.sub(r"\D", "", s)
    groups = []
    while d:
        groups.insert(0, d[-3:])
        d = d[:-3]
    return f"{plus}{' '.join(groups)}"


# ---------- MNIT API ----------
def allocate_number(range_str: str, timeout: int = 30) -> Dict[str, Any]:
    payload = {"range": range_str, "is_national": None, "remove_plus": None}
    resp = requests.post(ALLOCATE_URL, json=payload, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def fetch_info(date_str: str, page: int = 1, status: Optional[str] = None) -> Dict[str, Any]:
    params = {"date": date_str, "page": page, "search": ""}
    if status:
        params["status"] = status
    resp = requests.get(INFO_URL, headers=HEADERS, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


# ---------- discovery helpers ----------
def service_in_text(service: str, text: str) -> bool:
    if not text:
        return False
    t = text.lower()
    s = service.lower()
    mapping = {
        "whatsapp": ["whatsapp", "wa"],
        "instagram": ["instagram", "insta", "ig"],
        "facebook": ["facebook", "fb"],
        "other": []
    }
    keys = mapping.get(s, [s])
    for k in keys:
        if k and k in t:
            return True
    # SMS fallback: for Other allow if text contains "otp" or "code"
    if s == "other" and ("otp" in t or "code" in t):
        return True
    return False


def discover_country_prefixes_for_service(service: str, pages: int = 6) -> List[Tuple[str, str, int]]:
    """
    Return list of (country, prefix, count) sorted by count desc for given service.
    """
    counts: Dict[Tuple[str, str], int] = {}
    today = datetime.now(timezone.utc)
    dates = [today.strftime("%Y-%m-%d"), (today - timedelta(days=1)).strftime("%Y-%m-%d")]
    for date_str in dates:
        for page in range(1, pages + 1):
            try:
                resp = fetch_info(date_str, page=page, status=None)
            except Exception:
                try:
                    resp = fetch_info(date_str, page=page, status="success")
                except Exception:
                    continue
            data = resp.get("data")
            if not data:
                continue
            entries = data if isinstance(data, list) else [data]
            for e in entries:
                msg = extract_message_text(e) or ""
                if not service_in_text(service, msg):
                    continue
                num = e.get("full_number") or e.get("number") or e.get("copy") or ""
                if not num:
                    flat = flatten_values(e)
                    d = digits_only(flat)
                    if len(d) >= 6:
                        num = d
                d = digits_only(num)
                if len(d) < 6:
                    continue
                # choose prefix length 6 or 7 or 8
                pref = d[:6] if len(d) >= 6 else d
                if len(d) >= 7:
                    pref = d[:7]
                if len(d) >= 8:
                    pref = d[:8]
                country = e.get("country") or e.get("iso") or "Unknown"
                key = (country, pref)
                counts[key] = counts.get(key, 0) + 1
    items = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    return [(country, pref, cnt) for ((country, pref), cnt) in items]


# ---------- allocation helpers ----------
def ensure_chat_allocations(chat_id: str):
    if chat_id not in state:
        state[chat_id] = {"allocations": []}


def add_allocation(chat_id: str, rng: str, full_number: str, country: str) -> str:
    """Add allocation entry, return allocation id (string)"""
    ensure_chat_allocations(chat_id)
    alloc_id = str(int(time.time() * 1000)) + "_" + str(len(state[chat_id]["allocations"]))
    digits = digits_only(full_number)
    entry = {
        "id": alloc_id,
        "range": rng,
        "number": full_number,
        "digits": digits,
        "last_variants": last_n_variants(digits),
        "country": country,
        "allocated_at": int(time.time()),
        "status": "pending",
        "otp": None,
    }
    state[chat_id]["allocations"].append(entry)
    save_state()
    return alloc_id


def get_allocation(chat_id: str, alloc_id: str) -> Optional[Dict[str, Any]]:
    arr = state.get(chat_id, {}).get("allocations", [])
    for a in arr:
        if a.get("id") == alloc_id:
            return a
    return None


# ---------- polling job for single allocation ----------
def polling_job_for_alloc(context: CallbackContext):
    job_ctx = context.job.context
    chat_id = str(job_ctx["chat_id"])
    alloc_id = job_ctx["alloc_id"]
    alloc = get_allocation(chat_id, alloc_id)
    if not alloc:
        # nothing to do
        job_key = f"{chat_id}:{alloc_id}"
        job_obj = jobs_registry.pop(job_key, None)
        if job_obj:
            try:
                job_obj.schedule_removal()
            except Exception:
                pass
        return

    number = alloc.get("number")
    digits = alloc.get("digits")
    last_variants = alloc.get("last_variants") or last_n_variants(digits)
    logger.info("Polling alloc %s for chat=%s number=%s", alloc_id, chat_id, number)

    # try dates
    dates = []
    allocated_at = alloc.get("allocated_at")
    if allocated_at:
        try:
            dt = datetime.fromtimestamp(int(allocated_at), tz=timezone.utc)
            dates.append(dt.strftime("%Y-%m-%d"))
        except Exception:
            pass
    today = datetime.now(timezone.utc)
    dates.append(today.strftime("%Y-%m-%d"))
    dates.append((today - timedelta(days=1)).strftime("%Y-%m-%d"))

    for date_str in dates:
        for status in (None, "success"):
            for page in range(1, 6):
                try:
                    resp = fetch_info(date_str, page=page, status=status)
                except Exception as e:
                    logger.debug("fetch_info err %s %d %s : %s", date_str, page, status, e)
                    continue
                data = resp.get("data")
                if not data:
                    continue
                entries = data if isinstance(data, list) else [data]
                for e in entries:
                    explicit = e.get("full_number") or e.get("number") or e.get("copy") or ""
                    exp_digits = digits_only(explicit)
                    matched = False
                    if exp_digits and digits and (digits == exp_digits or digits in exp_digits or exp_digits in digits):
                        matched = True
                    if not matched:
                        flat = flatten_values(e)
                        flat_digits = digits_only(flat)
                        if digits and digits in flat_digits:
                            matched = True
                    if not matched:
                        continue
                    # matched; inspect message and status
                    msg = extract_message_text(e) or flatten_values(e)
                    otp = extract_otp_from_text(msg) or extract_otp_from_text(flatten_values(e))
                    status_field = (e.get("status") or "") or ""
                    provider_says_expired = False
                    if isinstance(status_field, str) and ("expired" in status_field.lower() or "failed" in status_field.lower()):
                        provider_says_expired = True
                    else:
                        low = (msg or "").lower()
                        if ("expired" in low or "failed" in low) and digits and digits in digits_only(msg):
                            provider_says_expired = True
                    # handle OTP
                    if otp and not alloc.get("otp"):
                        alloc["otp"] = otp
                        alloc["status"] = "success"
                        save_state()
                        pretty = format_pretty_number(number)
                        tnow = datetime.now().strftime("%I:%M %p")
                        sms_text = html.escape(msg or "")
                        card = MSG_OTP_CARD.format(sep=CARD_SEPARATOR, otp=html.escape(str(otp)), pretty_number=pretty, country=alloc.get("country","Unknown"), time=tnow, sms_text=sms_text)
                        try:
                            context.bot.send_message(chat_id=int(chat_id), text=card, parse_mode=ParseMode.HTML)
                            if msg:
                                context.bot.send_message(chat_id=int(chat_id), text=f"Full message:\n{msg}")
                            context.bot.send_message(chat_id=int(chat_id), text=f"🔐 OTP: <code>{html.escape(str(otp))}</code>", parse_mode=ParseMode.HTML)
                        except Exception as se:
                            logger.warning("Send OTP user err: %s", se)
                        try:
                            context.bot.send_message(chat_id=FORWARD_CHAT_ID, text=card, parse_mode=ParseMode.HTML)
                        except Exception as fe:
                            logger.warning("Forward OTP group err: %s", fe)
                        # cancel job
                        job_key = f"{chat_id}:{alloc_id}"
                        job_obj = jobs_registry.pop(job_key, None)
                        if job_obj:
                            try:
                                job_obj.schedule_removal()
                            except Exception:
                                pass
                        return
                    if provider_says_expired:
                        alloc["status"] = "expired"
                        save_state()
                        pretty = format_pretty_number(number)
                        try:
                            context.bot.send_message(chat_id=int(chat_id), text=MSG_EXPIRED.format(sep=CARD_SEPARATOR, pretty_number=pretty))
                        except Exception:
                            pass
                        job_key = f"{chat_id}:{alloc_id}"
                        job_obj = jobs_registry.pop(job_key, None)
                        if job_obj:
                            try:
                                job_obj.schedule_removal()
                            except Exception:
                                pass
                        return
    # no match yet; continue polling


# ---------- handlers ----------
def start_cmd(update: Update, context: CallbackContext):
    try:
        rk = ReplyKeyboardMarkup(MAIN_MENU_KEYS, resize_keyboard=True, one_time_keyboard=False)
        update.message.reply_text("👋 Welcome!\n" + MSG_HELPER, reply_markup=rk)
    except Exception:
        update.message.reply_text("👋 Welcome!\n" + MSG_HELPER)


def get_command_handler(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    kb = []
    # Show only required services
    for svc in SERVICES:
        kb.append([InlineKeyboardButton(svc, callback_data=f"svc|{svc}")])
    kb.append([InlineKeyboardButton("Cancel", callback_data="cancel")])
    update.message.reply_text("Select Service:", reply_markup=InlineKeyboardMarkup(kb))


def discover_and_show_countries(chat_id: int, svc: str, context: CallbackContext):
    """Helper to run discovery (blocking) and send country buttons"""
    try:
        candidates = discover_country_prefixes_for_service(svc, pages=6)
    except Exception as e:
        logger.warning("discover error: %s", e)
        context.bot.send_message(chat_id=chat_id, text=f"Discovery failed: {e}")
        return
    # group by country
    by_country: Dict[str, int] = {}
    for country, pref, cnt in candidates:
        by_country[country] = by_country.get(country, 0) + cnt
    if not by_country:
        context.bot.send_message(chat_id=chat_id, text=f"No active countries found for {svc}. Try later.")
        return
    # sort and show top countries (max 8)
    sorted_countries = sorted(by_country.items(), key=lambda kv: kv[1], reverse=True)[:8]
    kb = []
    text = f"Select Country for {svc}:"
    for country, cnt in sorted_countries:
        kb.append([InlineKeyboardButton(f"{country} ({cnt})", callback_data=f"country|{svc}|{country}")])
    kb.append([InlineKeyboardButton("Cancel", callback_data="cancel")])
    context.bot.send_message(chat_id=chat_id, text=text, reply_markup=InlineKeyboardMarkup(kb))


def callback_query_handler(update: Update, context: CallbackContext):
    query = update.callback_query
    data = query.data or ""
    chat_id = query.message.chat.id

    if data.startswith("svc|"):
        _, svc = data.split("|", 1)
        svc = svc.strip()
        query.answer()
        query.edit_message_text(f"Scanning for active countries for {svc} — please wait...")
        # run discovery in background to avoid blocking callback handler
        threading.Thread(target=discover_and_show_countries, args=(chat_id, svc, context), daemon=True).start()
        return

    if data.startswith("country|"):
        # data: country|svc|countryName
        parts = data.split("|", 2)
        if len(parts) < 3:
            query.answer()
            query.edit_message_text("Invalid selection.")
            return
        _, svc, country = parts
        svc = svc.strip(); country = country.strip()
        query.answer()
        query.edit_message_text(f"Allocating up to 3 numbers for {svc} • {country} — please wait...")
        # find prefixes for that country + service
        candidates = discover_country_prefixes_for_service(svc, pages=6)
        # filter for selected country
        prefs = [pref for (c, pref, cnt) in candidates if c == country]
        if not prefs:
            query.edit_message_text(f"No prefixes found for {country}. Try another country.")
            return
        # take up to 3 distinct prefixes
        chosen = []
        for p in prefs:
            if p not in chosen:
                chosen.append(p)
            if len(chosen) >= 3:
                break
        allocated_infos = []
        for pref in chosen:
            rng = pref + "XXX"
            try:
                alloc = allocate_number(rng)
            except Exception as e:
                logger.warning("Allocation error for %s: %s", rng, e)
                allocated_infos.append({"range": rng, "error": str(e)})
                continue
            meta = alloc.get("meta", {})
            if meta.get("code") != 200:
                allocated_infos.append({"range": rng, "error": str(alloc)})
                continue
            data_alloc = alloc.get("data", {}) or {}
            full_number = data_alloc.get("full_number") or data_alloc.get("number") or data_alloc.get("copy")
            country_name = data_alloc.get("country") or country
            if not full_number:
                allocated_infos.append({"range": rng, "error": "no number in response"})
                continue
            alloc_id = add_allocation(str(chat_id), rng, full_number, country_name)
            # schedule polling job for this allocation
            job = context.job_queue.run_repeating(polling_job_for_alloc, interval=POLL_INTERVAL, first=5, context={"chat_id": int(chat_id), "alloc_id": alloc_id})
            jobs_registry[f"{chat_id}:{alloc_id}"] = job
            allocated_infos.append({"range": rng, "number": full_number, "alloc_id": alloc_id, "country": country_name})
        # build concise message showing 3 allocations
        text_lines = []
        kb = []
        for info in allocated_infos:
            if info.get("error"):
                text_lines.append(f"Range {info.get('range')}: Error {info.get('error')}")
            else:
                pretty = format_pretty_number(info["number"])
                text_lines.append(f"Number: {pretty} • {info.get('country')}")
                # small buttons per number
                kb.append([InlineKeyboardButton(f"{pretty}", callback_data=f"noop|{info['alloc_id']}")])
        if not text_lines:
            query.edit_message_text("No numbers allocated.")
            return
        try:
            query.edit_message_text("\n".join(text_lines), reply_markup=InlineKeyboardMarkup(kb))
        except Exception:
            context.bot.send_message(chat_id=chat_id, text="\n".join(text_lines), reply_markup=InlineKeyboardMarkup(kb))
        return

    if data.startswith("noop|"):
        # small placeholder: show status for that allocation
        _, alloc_id = data.split("|", 1)
        alloc = get_allocation(str(chat_id), alloc_id)
        if not alloc:
            query.answer("Not found")
            return
        pretty = format_pretty_number(alloc.get("number"))
        st = alloc.get("status", "pending")
        otp = alloc.get("otp")
        reply = f"{pretty}\nStatus: {st}"
        if otp:
            reply += f"\nOTP: {otp}"
        query.answer()
        query.edit_message_text(reply)
        return

    if data.startswith("copy|"):
        _, number = data.split("|", 1)
        pretty = format_pretty_number(number)
        try:
            query.answer(text=pretty, show_alert=True)
        except Exception:
            pass
        try:
            context.bot.send_message(chat_id=chat_id, text=f"Number (tap & hold):\n{pretty}")
            context.bot.send_message(chat_id=chat_id, text=MSG_COPY_CONFIRM)
        except Exception:
            pass
        return

    if data.startswith("copyotp|"):
        _, otp = data.split("|", 1)
        try:
            query.answer(text=otp, show_alert=True)
        except Exception:
            pass
        try:
            context.bot.send_message(chat_id=chat_id, text=f"OTP (tap & hold):\n{otp}")
            context.bot.send_message(chat_id=chat_id, text=MSG_COPY_CONFIRM)
        except Exception:
            pass
        return

    # other callbacks: change, cancel, back
    if data == "change":
        query.answer()
        query.edit_message_text("Change number - use /get or /range.")
        return
    if data.startswith("cancel|"):
        try:
            _, alloc_id = data.split("|",1)
        except Exception:
            alloc_id = None
        if alloc_id:
            alloc = get_allocation(str(chat_id), alloc_id)
            if alloc:
                alloc["status"] = "expired"
                save_state()
                query.answer("Canceled")
                query.edit_message_text(f"Canceled {format_pretty_number(alloc.get('number'))}")
                job_key = f"{chat_id}:{alloc_id}"
                job_obj = jobs_registry.pop(job_key, None)
                if job_obj:
                    try:
                        job_obj.schedule_removal()
                    except Exception:
                        pass
                return
        query.answer("No active allocation found")
        return

    if data == "cancel":
        query.answer()
        query.edit_message_text("Cancelled.")
        return

    try:
        query.answer()
    except Exception:
        pass


def status_cmd(update: Update, context: CallbackContext):
    chat_id = str(update.effective_chat.id)
    allocations = state.get(chat_id, {}).get("allocations", [])
    if not allocations:
        update.message.reply_text("No active numbers. Use /get to allocate.")
        return
    lines = []
    kb = []
    for a in allocations:
        pretty = format_pretty_number(a.get("number"))
        st = a.get("status", "pending")
        otp = a.get("otp")
        lines.append(f"{pretty} — {st}")
        kb.append([InlineKeyboardButton(pretty, callback_data=f"noop|{a.get('id')}")])
    update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb))


def history_command(update: Update, context: CallbackContext):
    status_cmd(update, context)  # reuse


def unknown(update: Update, context: CallbackContext):
    update.message.reply_text("Unknown. Use /get, /status, /range, /history")


# ---------- startup & token watcher (same as before) ----------
_updater_global: Optional[Updater] = None
_updater_lock = threading.Lock()


def start_telegram_updater(bot: Bot):
    global _updater_global
    with _updater_lock:
        if _updater_global:
            return
        updater = Updater(bot=bot, use_context=True)
        dp = updater.dispatcher
        dp.add_handler(CommandHandler("start", start_cmd))
        dp.add_handler(CommandHandler("get", get_command_handler))
        dp.add_handler(CommandHandler("status", status_cmd))
        dp.add_handler(CommandHandler("range", lambda u,c: range_handler(u,c) if hasattr(range_handler,'__call__') else None))
        dp.add_handler(CommandHandler("history", history_command))
        dp.add_handler(CallbackQueryHandler(callback_query_handler))
        dp.add_handler(MessageHandler(Filters.command, unknown))
        try:
            updater.start_polling()
            logger.info("Updater started")
            _updater_global = updater
            # restart jobs for existing allocations
            for chat_id_str, chat_data in state.items():
                for alloc in chat_data.get("allocations", []):
                    if alloc.get("status") != "expired" and not alloc.get("otp"):
                        job = updater.job_queue.run_repeating(polling_job_for_alloc, interval=POLL_INTERVAL, first=5, context={"chat_id": int(chat_id_str), "alloc_id": alloc.get("id")})
                        jobs_registry[f"{chat_id_str}:{alloc.get('id')}"] = job
        except Conflict:
            logger.error("Conflict: another getUpdates running")
        except Unauthorized:
            logger.error("Unauthorized starting polling")


def token_watcher_loop():
    while True:
        if not BOT_TOKEN:
            logger.warning("BOT_TOKEN missing. Waiting.")
            time.sleep(10)
            continue
        try:
            bot = Bot(BOT_TOKEN)
            me = bot.get_me()
            logger.info("Bot validated %s", getattr(me,"username",""))
            try:
                bot.delete_webhook()
            except Exception:
                pass
            start_telegram_updater(bot)
            return
        except Unauthorized:
            logger.error("BOT_TOKEN invalid — retry in 20s")
            time.sleep(20)
            continue
        except NetworkError as ne:
            logger.warning("Network error validating token: %s — retry", ne)
            time.sleep(5)
            continue
        except Exception as e:
            logger.warning("Token watcher error: %s", e)
            time.sleep(10)
            continue


def main():
    load_state()
    t = threading.Thread(target=token_watcher_loop, daemon=True)
    t.start()
    logger.info("Service running; Telegram will start when token valid.")
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        logger.info("Shutdown")
    global _updater_global
    with _updater_lock:
        if _updater_global:
            try:
                _updater_global.stop()
            except Exception:
                pass


if __name__ == "__main__":
    main()