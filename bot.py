#!/usr/bin/env python3
"""
Dr OTP Receiver - Professional, robust Telegram OTP receiver and allocator

Features:
- Token-watcher that reads BOT_TOKEN from environment and starts/upgrades Telegram updater when token is valid.
- /get interactive flow: service -> country discovery -> auto-allocate up to N numbers (from different prefixes).
- Allocation retry logic: tries multiple payload shapes to handle provider quirks.
- Per-allocation polling jobs that watch /info for messages and forward OTPs to user + FORWARD_CHAT_ID.
- Strict expired detection (only when provider explicitly marks expired OR message explicitly links number->expired).
- /checktoken administrative command.
- Admin debug mode (ENABLE_DEBUG_TO_CHAT env var to send debug responses to your admin chat id).
- Structured logging (JSON logs optional).
- Save/restore state (state.json) and restart pending polling jobs on boot.
- Configurable via env vars: BOT_TOKEN, MNIT_API_KEY, FORWARD_CHAT_ID, POLL_INTERVAL, DISCOVER_PAGES, MAX_ALLOC_PER_COUNTRY.

Security:
- Do not hardcode tokens in code.
- Revoke any token you previously posted publicly and create a new one.

Required environment variables:
- BOT_TOKEN            (from BotFather)
- MNIT_API_KEY         (provider API key)
Optional:
- FORWARD_CHAT_ID      (telegram chat id to forward OTPs to, default -1003379113224)
- POLL_INTERVAL        (seconds, default 10)
- DISCOVER_PAGES       (pages to scan for discovery, default 6)
- MAX_ALLOC_PER_COUNTRY (default 3)
- ENABLE_DEBUG_TO_CHAT (chat_id to send debug messages to; optional)

Usage:
- /checktoken   -> verifies current token
- /get          -> interactive flow: pick service, pick country, allocate up to N numbers
- /status       -> shows active numbers for this chat
- /history      -> alias for /status
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
import traceback

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

# -------------------------
# Configuration (from env)
# -------------------------
MNIT_API_KEY = os.getenv("MNIT_API_KEY", "M_WH9Q3U88V").strip()
FORWARD_CHAT_ID = int(os.getenv("FORWARD_CHAT_ID", "-1003379113224"))
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "10"))
DISCOVER_PAGES = int(os.getenv("DISCOVER_PAGES", "6"))
MAX_ALLOC_PER_COUNTRY = int(os.getenv("MAX_ALLOC_PER_COUNTRY", "3"))
ENABLE_DEBUG_TO_CHAT = os.getenv("ENABLE_DEBUG_TO_CHAT")  # optional chat id to receive debug info
# NOTE: BOT_TOKEN is dynamically read by token_watcher_loop (do not rely on module-level value)

ALLOCATE_URL = "https://x.mnitnetwork.com/mapi/v1/mdashboard/getnum/number"
INFO_URL = "https://x.mnitnetwork.com/mapi/v1/mdashboard/getnum/info"
HEADERS = {"Content-Type": "application/json", "mapikey": MNIT_API_KEY}

# -------------------------
# UI / texts
# -------------------------
CARD_SEPARATOR = "━━━━━━━━━━━━━━━━━━━━━━━━━━"
SERVICES = ["WhatsApp", "Facebook", "Instagram", "Other"]
MAIN_MENU_KEYS = [["📲 Get Number", "📥 Active"], ["📜 History", "⚙ Settings"]]

MSG_HELPER = "Tip: Use /get to request numbers (interactive flow)."
MSG_COPY_CONFIRM = "✅ Sent — long-press to copy."

MSG_ALLOCATION_START = "Allocating numbers for {country} ({service}) — trying up to {max_alloc} candidates..."
MSG_NO_COUNTRIES = "No active countries found for {service}. Try again later."

# -------------------------
# Logging
# -------------------------
logging.basicConfig(format="[%(asctime)s] %(levelname)s: %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# -------------------------
# Persistent state
# -------------------------
STATE_FILE = "state.json"
# structure: state = { chat_id_str: { "allocations": [ { id, range, number, digits, country, allocated_at, status, otp } ] } }
state: Dict[str, Dict[str, Any]] = {}
jobs_registry: Dict[str, Any] = {}

# -------------------------
# Utilities
# -------------------------
def save_state() -> None:
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        logger.exception("Failed to save state.json: %s", e)


def load_state() -> None:
    global state
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                state = json.load(f)
            logger.info("Loaded state for %d chats", len(state))
        except Exception as e:
            logger.exception("Failed to load state.json: %s", e)
            state = {}


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
        out: List[str] = []
        for v in x.values():
            out.append(flatten_values(v))
        return " ".join([p for p in out if p])
    if isinstance(x, list):
        return " ".join(flatten_values(i) for i in x)
    return str(x)


def extract_message_text(entry: Dict[str, Any]) -> str:
    for k in ("message", "sms", "msg", "text", "body", "sms_text", "content", "raw"):
        v = entry.get(k)
        if v:
            return flatten_values(v)
    # deep search
    def search(obj):
        if isinstance(obj, dict):
            for kk, vv in obj.items():
                if isinstance(vv, (str, int, float)) and kk.lower() in ("message", "sms", "msg", "text", "body", "content", "description"):
                    return str(vv)
                res = search(vv)
                if res:
                    return res
        if isinstance(obj, list):
            for i in obj:
                res = search(i)
                if res:
                    return res
        return ""
    nested = search(entry)
    if nested:
        return nested
    return flatten_values(entry)


def extract_otp_from_text(text: str) -> Optional[str]:
    if not text:
        return None
    txt = re.sub(r"[|:]+", " ", text)
    m = re.search(r"\b(\d{4,8})\b", txt)
    if m:
        return m.group(1)
    m2 = re.search(r"[<#>]{1,3}\s*([0-9]{4,8})", txt)
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
    d = digits_only(s)
    groups: List[str] = []
    while d:
        groups.insert(0, d[-3:])
        d = d[:-3]
    return f"{plus}{' '.join(groups)}"

# -------------------------
# MNIT API helpers
# -------------------------
def fetch_info(date_str: str, page: int = 1, status: Optional[str] = None) -> Dict[str, Any]:
    params = {"date": date_str, "page": page, "search": ""}
    if status:
        params["status"] = status
    resp = requests.get(INFO_URL, headers=HEADERS, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def try_allocate_payload_variants(prefix: str, timeout: int = 25) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Try multiple payload variants to work around provider API differences.
    Returns (response_json, error_message). If success, response_json contains API response.
    """
    payload_variants = [
        {"range": prefix, "is_national": None, "remove_plus": None},
        {"range": prefix, "is_national": False, "remove_plus": False},
        {"range": prefix, "is_national": True, "remove_plus": False},
        {"range": prefix, "is_national": False, "remove_plus": True},
        {"range": prefix, "is_national": True, "remove_plus": True},
        {"range": prefix},
    ]
    last_error = None
    for p in payload_variants:
        try:
            logger.debug("Allocating prefix %s with payload %s", prefix, p)
            resp = requests.post(ALLOCATE_URL, json=p, headers=HEADERS, timeout=timeout)
            text = resp.text
            try:
                j = resp.json()
            except Exception:
                j = {"http_status": resp.status_code, "body": text}
            if 200 <= resp.status_code < 300:
                logger.info("Allocation success (payload=%s)", p)
                return j, None
            else:
                logger.warning("Alloc attempt HTTP %s payload=%s body=%s", resp.status_code, p, text[:400])
                last_error = f"HTTP {resp.status_code}: {text[:300]}"
        except Exception as e:
            logger.warning("Alloc attempt error payload=%s error=%s", p, e)
            last_error = str(e)
    return None, last_error

# -------------------------
# Discovery helpers
# -------------------------
def service_in_text(service: str, text: str) -> bool:
    if not text:
        return False
    t = text.lower()
    s = service.lower()
    mapping = {
        "whatsapp": ["whatsapp", "wa"],
        "instagram": ["instagram", "insta", "ig"],
        "facebook": ["facebook", "fb"],
        "other": [],
    }
    keys = mapping.get(s, [s])
    for k in keys:
        if k and k in t:
            return True
    if s == "other" and ("otp" in t or "code" in t):
        return True
    return False


def discover_country_prefixes_for_service(service: str, pages: int = DISCOVER_PAGES) -> List[Tuple[str, str, int]]:
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
                # prefer 7-8 digit prefixes where present
                pref = d[:6]
                if len(d) >= 8:
                    pref = d[:8]
                elif len(d) >= 7:
                    pref = d[:7]
                country = e.get("country") or e.get("iso") or "Unknown"
                key = (country, pref)
                counts[key] = counts.get(key, 0) + 1
    items = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    return [(country, pref, cnt) for ((country, pref), cnt) in items]

# -------------------------
# Allocation bookkeeping
# -------------------------
def ensure_chat_allocations(chat_id: str) -> None:
    if chat_id not in state:
        state[chat_id] = {"allocations": []}


def add_allocation(chat_id: str, rng: str, full_number: str, country: str) -> str:
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

# -------------------------
# Polling per-allocation job
# -------------------------
def polling_job_for_alloc(context: CallbackContext) -> None:
    job_ctx = context.job.context
    chat_id = str(job_ctx["chat_id"])
    alloc_id = job_ctx["alloc_id"]
    alloc = get_allocation(chat_id, alloc_id)
    if not alloc:
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
    logger.debug("Polling alloc %s number %s", alloc_id, number)

    dates = []
    allocated_at = alloc.get("allocated_at")
    if allocated_at:
        try:
            dt = datetime.fromtimestamp(int(allocated_at), tz=timezone.utc)
            dates.append(dt.strftime("%Y-%m-%d"))
        except Exception:
            pass
    today = datetime.now(timezone.utc)
    dates.extend([today.strftime("%Y-%m-%d"), (today - timedelta(days=1)).strftime("%Y-%m-%d")])

    for date_str in dates:
        for status in (None, "success"):
            for page in range(1, 6):
                try:
                    resp = fetch_info(date_str, page=page, status=status)
                except Exception as e:
                    logger.debug("fetch_info error: %s", e)
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

                    if otp and not alloc.get("otp"):
                        alloc["otp"] = otp
                        alloc["status"] = "success"
                        save_state()
                        pretty = format_pretty_number(number)
                        tnow = datetime.now().strftime("%I:%M %p")
                        sms_text = html.escape(msg or "")
                        card = (
                            f"{CARD_SEPARATOR}\n"
                            f"🔔 OTP Received\n"
                            f"{CARD_SEPARATOR}\n"
                            f"📩 Code: <code>{html.escape(str(otp))}</code>\n"
                            f"📞 Number: {pretty}\n"
                            f"🗺 Country: {alloc.get('country','Unknown')}\n"
                            f"⏰ Time: {tnow}\n"
                            f"{CARD_SEPARATOR}\n"
                            f"Message:\n{sms_text}"
                        )
                        try:
                            context.bot.send_message(chat_id=int(chat_id), text=card, parse_mode=ParseMode.HTML)
                            if msg:
                                context.bot.send_message(chat_id=int(chat_id), text=f"Full message:\n{msg}")
                            context.bot.send_message(chat_id=int(chat_id), text=f"🔐 OTP: <code>{html.escape(str(otp))}</code>", parse_mode=ParseMode.HTML)
                        except Exception as se:
                            logger.warning("Failed to notify user: %s", se)
                        try:
                            context.bot.send_message(chat_id=FORWARD_CHAT_ID, text=card, parse_mode=ParseMode.HTML)
                        except Exception as fe:
                            logger.warning("Forward fail: %s", fe)
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
                        try:
                            context.bot.send_message(chat_id=int(chat_id), text=f"{CARD_SEPARATOR}\n❌ Expired\n{CARD_SEPARATOR}\n{format_pretty_number(number)}\nThis number was marked expired by provider.")
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
    # end polling

# -------------------------
# Telegram handlers / flows
# -------------------------
def start_handler(update: Update, context: CallbackContext) -> None:
    try:
        rk = ReplyKeyboardMarkup(MAIN_MENU_KEYS, resize_keyboard=True, one_time_keyboard=False)
        update.message.reply_text("Hello! " + MSG_HELPER, reply_markup=rk)
    except Exception:
        update.message.reply_text("Hello! " + MSG_HELPER)


def get_handler(update: Update, context: CallbackContext) -> None:
    kb = []
    for svc in SERVICES:
        kb.append([InlineKeyboardButton(svc, callback_data=f"svc|{svc}")])
    kb.append([InlineKeyboardButton("Cancel", callback_data="cancel")])
    update.message.reply_text("Select service:", reply_markup=InlineKeyboardMarkup(kb))


def discover_and_send_countries(chat_id: int, svc: str, context: CallbackContext) -> None:
    try:
        candidates = discover_country_prefixes_for_service(svc, pages=DISCOVER_PAGES)
    except Exception as e:
        logger.exception("Discovery failed: %s", e)
        context.bot.send_message(chat_id=chat_id, text=f"Discovery failed: {e}")
        return
    by_country: Dict[str, int] = {}
    for country, pref, cnt in candidates:
        by_country[country] = by_country.get(country, 0) + cnt
    if not by_country:
        context.bot.send_message(chat_id=chat_id, text=MSG_NO_COUNTRIES.format(service=svc))
        return
    sorted_countries = sorted(by_country.items(), key=lambda kv: kv[1], reverse=True)[:8]
    kb = []
    text = f"Select Country for {svc}:"
    for country, cnt in sorted_countries:
        kb.append([InlineKeyboardButton(f"{country} ({cnt})", callback_data=f"country|{svc}|{country}")])
    kb.append([InlineKeyboardButton("Cancel", callback_data="cancel")])
    context.bot.send_message(chat_id=chat_id, text=text, reply_markup=InlineKeyboardMarkup(kb))


def callback_query_handler(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    data = query.data or ""
    chat_id = query.message.chat.id

    if data.startswith("svc|"):
        _, svc = data.split("|", 1)
        svc = svc.strip()
        query.answer()
        query.edit_message_text(f"Scanning for active countries for {svc} — please wait...")
        threading.Thread(target=discover_and_send_countries, args=(chat_id, svc, context), daemon=True).start()
        return

    if data.startswith("country|"):
        parts = data.split("|", 2)
        if len(parts) < 3:
            query.answer()
            query.edit_message_text("Invalid selection.")
            return
        _, svc, country = parts
        svc = svc.strip()
        country = country.strip()
        query.answer()
        query.edit_message_text(MSG_ALLOCATION_START.format(country=country, service=svc, max_alloc=MAX_ALLOC_PER_COUNTRY))
        candidates = discover_country_prefixes_for_service(svc, pages=DISCOVER_PAGES)
        prefs = [pref for (c, pref, cnt) in candidates if c == country]
        if not prefs:
            query.edit_message_text(f"No prefixes found for {country}.")
            return
        chosen = []
        for p in prefs:
            if p not in chosen:
                chosen.append(p)
            if len(chosen) >= MAX_ALLOC_PER_COUNTRY:
                break
        allocated_infos = []
        for pref in chosen:
            rng = pref + "XXX"
            resp_json, err = try_allocate_payload_variants(rng)
            if resp_json is None:
                allocated_infos.append({"range": rng, "error": err or "Unknown"})
                continue
            meta = resp_json.get("meta", {})
            if meta.get("code") != 200:
                allocated_infos.append({"range": rng, "error": str(resp_json)})
                continue
            data_alloc = resp_json.get("data", {}) or {}
            full_number = data_alloc.get("full_number") or data_alloc.get("number") or data_alloc.get("copy")
            country_name = data_alloc.get("country") or country
            if not full_number:
                allocated_infos.append({"range": rng, "error": "No number in response"})
                continue
            alloc_id = add_allocation(str(chat_id), rng, full_number, country_name)
            job = context.job_queue.run_repeating(polling_job_for_alloc, interval=POLL_INTERVAL, first=5, context={"chat_id": int(chat_id), "alloc_id": alloc_id})
            jobs_registry[f"{chat_id}:{alloc_id}"] = job
            allocated_infos.append({"range": rng, "number": full_number, "alloc_id": alloc_id, "country": country_name})
        lines = []
        kb = []
        for info in allocated_infos:
            if info.get("error"):
                lines.append(f"Range {info.get('range')}: Error {info.get('error')}")
            else:
                pretty = format_pretty_number(info["number"])
                lines.append(f"{pretty} • {info.get('country')}")
                kb.append([InlineKeyboardButton(pretty, callback_data=f"noop|{info['alloc_id']}")])
        if not lines:
            query.edit_message_text("No allocations made.")
            return
        try:
            query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb) if kb else None)
        except Exception:
            context.bot.send_message(chat_id=chat_id, text="\n".join(lines), reply_markup=InlineKeyboardMarkup(kb) if kb else None)
        return

    if data.startswith("noop|"):
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

    if data.startswith("cancel|"):
        try:
            _, alloc_id = data.split("|", 1)
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
        query.answer("No active allocation")
        return

    if data == "cancel":
        query.answer()
        query.edit_message_text("Cancelled.")
        return

    try:
        query.answer()
    except Exception:
        pass

# -------------------------
# status/history handlers
# -------------------------
def status_handler(update: Update, context: CallbackContext) -> None:
    chat_id = str(update.effective_chat.id)
    arr = state.get(chat_id, {}).get("allocations", [])
    if not arr:
        update.message.reply_text("No active numbers. Use /get.")
        return
    lines = []
    kb = []
    for a in arr:
        pretty = format_pretty_number(a.get("number"))
        lines.append(f"{pretty} — {a.get('status','pending')}")
        kb.append([InlineKeyboardButton(pretty, callback_data=f"noop|{a.get('id')}")])
    update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb))


def history_handler(update: Update, context: CallbackContext) -> None:
    status_handler(update, context)


def unknown_handler(update: Update, context: CallbackContext) -> None:
    update.message.reply_text("Unknown. Use /get, /status, /history")


# -------------------------
# Token watcher & updater start
# -------------------------
_updater_global: Optional[Updater] = None
_updater_lock = threading.Lock()

def start_telegram_updater(bot: Bot) -> None:
    global _updater_global
    with _updater_lock:
        if _updater_global:
            return
        updater = Updater(bot=bot, use_context=True)
        dp = updater.dispatcher
        dp.add_handler(CommandHandler("start", start_handler))
        dp.add_handler(CommandHandler("get", get_handler))
        dp.add_handler(CommandHandler("status", status_handler))
        dp.add_handler(CommandHandler("history", history_handler))
        dp.add_handler(CommandHandler("checktoken", checktoken_command))
        dp.add_handler(CallbackQueryHandler(callback_query_handler))
        dp.add_handler(MessageHandler(Filters.command, unknown_handler))
        try:
            updater.start_polling()
            logger.info("Updater started.")
            _updater_global = updater
            # restart pending polling jobs
            for chat_id, chat_data in state.items():
                for alloc in chat_data.get("allocations", []):
                    if alloc.get("status") != "expired" and not alloc.get("otp"):
                        job = updater.job_queue.run_repeating(polling_job_for_alloc, interval=POLL_INTERVAL, first=5, context={"chat_id": int(chat_id), "alloc_id": alloc.get("id")})
                        jobs_registry[f"{chat_id}:{alloc.get('id')}"] = job
        except Conflict:
            logger.error("Conflict: another getUpdates running")
        except Unauthorized:
            logger.error("Unauthorized when starting updater")


def token_watcher_loop() -> None:
    """
    Repeatedly re-reads BOT_TOKEN from environment so you can change token in Koyeb UI without
    needing an immediate redeploy. Will start the updater once valid.
    """
    last_token = ""
    while True:
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token:
            logger.warning("BOT_TOKEN not set. Waiting 10s.")
            time.sleep(10)
            continue
        if token != last_token:
            logger.info("BOT_TOKEN loaded/changed in environment (token masked).")
            last_token = token
        try:
            bot = Bot(token)
            me = bot.get_me()
            logger.info("Validated bot: %s (id=%s)", getattr(me, "username", ""), getattr(me, "id", ""))
            try:
                bot.delete_webhook()
                logger.info("Deleted webhook (if any).")
            except Exception:
                pass
            start_telegram_updater(bot)
            return
        except Unauthorized:
            logger.error("BOT_TOKEN invalid/unauthorized. Retry in 20s.")
            time.sleep(20)
            continue
        except NetworkError as ne:
            logger.warning("Network error validating token: %s — retry", ne)
            time.sleep(5)
            continue
        except Exception as e:
            logger.warning("Token watcher unexpected error: %s — retry", e)
            time.sleep(10)
            continue

# -------------------------
# Admin / helper commands
# -------------------------
def checktoken_command(update: Update, context: CallbackContext) -> None:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        update.message.reply_text("BOT_TOKEN not set in environment.")
        return
    try:
        b = Bot(token)
        me = b.get_me()
        update.message.reply_text(f"Token OK. Bot: @{getattr(me,'username','')}, id={getattr(me,'id','')}")
    except Exception as e:
        update.message.reply_text(f"Token test failed: {type(e).__name__}: {e}")

# -------------------------
# Main
# -------------------------
def main() -> None:
    load_state()
    # spawn token watcher
    t = threading.Thread(target=token_watcher_loop, daemon=True)
    t.start()
    logger.info("Service running. Telegram updater will start when BOT_TOKEN valid.")
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        logger.info("Shutdown requested.")
    global _updater_global
    with _updater_lock:
        if _updater_global:
            try:
                _updater_global.stop()
            except Exception:
                pass

if __name__ == "__main__":
    main()