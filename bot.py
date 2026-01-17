#!/usr/bin/env python3
"""
Dr OTP Receiver Bot - Improved matching, expire-fix, and automatic range discovery

Fixes:
- Avoid false "Expired" by matching API entries strictly:
  * Prefer explicit fields "number", "full_number", "copy" to match allocated number.
  * Only use flattened entry as fallback when it clearly contains the full allocated number digits.
  * Mark "expired" only when API entry's status explicitly contains 'expired'/'failed' or
    when the matching API entry's message contains those keywords for the same number.
- Added /discover command to scan recent MNIT /info pages and propose active ranges.
  The bot extracts likely prefixes (first N digits) and suggests them as clickable ranges.
- Adds callback action "use_range|{range}" so user can pick a discovered range quickly.
- Keeps existing functionality: allocation, polling, OTP forwarding, copy UX, forwarding to group.
- Robust to missing/invalid BOT_TOKEN (token watcher thread starts updater when valid).

ENV:
- BOT_TOKEN (recommended)
- MNIT_API_KEY (required)
- FORWARD_CHAT_ID (optional) default: -1003379113224
- POLL_INTERVAL (optional) default: 10

Deploy: replace bot.py, restart service. If OTP still not forwarded, provide one sample JSON response
from the INFO endpoint so matching can be tuned.
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
from typing import Optional, Dict, Any, List
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

# ---------- UI & templates ----------
CARD_SEPARATOR = "━━━━━━━━━━━━━━━━━━━━━━━━━━"

BUTTON_LABELS = {
    "copy": "📋 Copy Number",
    "change": "🔁 Get New Number",
    "cancel": "❌ Cancel Number",
    "back": "⬅ Back to Menu",
    "copyotp": "📋 Copy OTP",
}

MSG_HELPER = "ℹ️ Tip: Use /range 261347435XXX to request numbers in that range."
MSG_COPY_CONFIRM = "✅ Sent — long-press the message or copy from the alert."

MAIN_MENU_KEYS = [
    ["📲 Get Number"],
    ["📥 Active Numbers"],
    ["📜 History"],
    ["💰 Balance"],
    ["⚙ Settings"],
    ["📞 Support"],
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
    "⚠️ Do not share this code\n"
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
    "You can request a new one."
)

MSG_ALLOCATION_ERROR = (
    "{sep}\n"
    "⚠️ Allocation Failed\n"
    "{sep}\n"
    "{short_error_message}\n\n"
    "Tip: Try a different range or try again shortly."
)

# ---------- Logging ----------
logging.basicConfig(format="[%(asctime)s] %(levelname)s: %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- In-memory state ----------
# state: chat_id_str -> { range, number, digits, last_variants, country, allocated_at, status, otp }
state: Dict[str, Dict[str, Any]] = {}
jobs_registry: Dict[str, Any] = {}

# ---------- Persistence ----------
def load_state() -> None:
    global state
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                state = json.load(f)
            logger.info("Loaded state for %d chats", len(state))
    except Exception as e:
        logger.warning("Failed to load state.json: %s", e)


def save_state() -> None:
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        logger.warning("Failed to save state.json: %s", e)


# ---------- Utilities ----------
def digits_only(s: Optional[str]) -> str:
    if not s:
        return ""
    return re.sub(r"\D", "", str(s))


def last_n_variants(s: str, lengths: Optional[List[int]] = None) -> List[str]:
    if lengths is None:
        lengths = [6, 7, 8, 9, 10]
    d = digits_only(s)
    out: List[str] = []
    for n in lengths:
        if len(d) >= n:
            out.append(d[-n:])
    return out


def flatten_values(x: Any) -> str:
    if isinstance(x, dict):
        parts: List[str] = []
        for v in x.values():
            parts.append(flatten_values(v))
        return " ".join([p for p in parts if p])
    if isinstance(x, list):
        return " ".join(flatten_values(i) for i in x)
    return str(x)


def extract_message_text(entry: Dict[str, Any]) -> str:
    for k in ("message", "sms", "msg", "text", "body", "sms_text", "content", "raw"):
        v = entry.get(k)
        if v:
            return flatten_values(v)
    # deep search simple heuristic
    def search(obj):
        if isinstance(obj, dict):
            for kk, vv in obj.items():
                if isinstance(vv, (str, int, float)) and kk.lower() in (
                    "message",
                    "sms",
                    "msg",
                    "text",
                    "body",
                    "content",
                    "description",
                ):
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
    m2 = re.search(r"\b([A-Z0-9]{1,6}[-_]\d{3,8})\b", txt, flags=re.IGNORECASE)
    if m2:
        return m2.group(1)
    m3 = re.search(r"[<#>]{1,3}\s*([0-9]{4,8})\b", txt)
    if m3:
        return m3.group(1)
    return None


def format_pretty_number(number: str) -> str:
    if not number:
        return ""
    s = str(number).strip()
    plus = ""
    if s.startswith("+"):
        plus = "+"
        s = s[1:]
    digits = re.sub(r"\D", "", s)
    groups: List[str] = []
    while digits:
        groups.insert(0, digits[-3:])
        digits = digits[:-3]
    pretty = " ".join(groups)
    return f"{plus}{pretty}"


# ---------- MNIT API ----------
def allocate_number(range_str: str, timeout: int = 30) -> Dict[str, Any]:
    payload = {"range": range_str, "is_national": None, "remove_plus": None}
    logger.info("Requesting allocation for range=%s", range_str)
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


# ---------- Keyboards ----------
def make_inline_buttons(number: str) -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(BUTTON_LABELS["copy"], callback_data=f"copy|{number}")],
        [InlineKeyboardButton(BUTTON_LABELS["change"], callback_data="change")],
        [InlineKeyboardButton(BUTTON_LABELS["cancel"], callback_data=f"cancel|{number}")],
        [InlineKeyboardButton(BUTTON_LABELS["back"], callback_data="back")],
    ]
    return InlineKeyboardMarkup(kb)


def make_inline_buttons_after_timeout() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(BUTTON_LABELS["change"], callback_data="change")],
        [InlineKeyboardButton(BUTTON_LABELS["back"], callback_data="back")],
    ]
    return InlineKeyboardMarkup(kb)


def make_inline_buttons_for_otp(otp: str) -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(BUTTON_LABELS["copyotp"], callback_data=f"copyotp|{otp}")],
        [InlineKeyboardButton(BUTTON_LABELS["change"], callback_data="change")],
        [InlineKeyboardButton(BUTTON_LABELS["back"], callback_data="back")],
    ]
    return InlineKeyboardMarkup(kb)


# ---------- Discover helper ----------
def discover_ranges_from_info(pages: int = 5, dates: Optional[List[str]] = None) -> List[str]:
    """
    Scan recent /info pages and build a list of candidate ranges (prefixes).
    Strategy: collect full numbers from API entries and return distinct prefixes of length 6 (or 7).
    """
    prefixes = {}
    if dates is None:
        today = datetime.now(timezone.utc)
        dates = [today.strftime("%Y-%m-%d"), (today - timedelta(days=1)).strftime("%Y-%m-%d")]
    try:
        for date_str in dates:
            for page in range(1, pages + 1):
                try:
                    resp = fetch_info(date_str, page=page, status=None)
                except Exception:
                    # try with status=success as fallback
                    try:
                        resp = fetch_info(date_str, page=page, status="success")
                    except Exception:
                        continue
                data = resp.get("data")
                if not data:
                    continue
                entries = data if isinstance(data, list) else [data]
                for e in entries:
                    num = e.get("full_number") or e.get("number") or e.get("copy") or ""
                    if not num:
                        # try to extract digits from flattened text
                        flat = flatten_values(e)
                        d = digits_only(flat)
                        if len(d) >= 6:
                            num = d
                    d = digits_only(num)
                    if len(d) >= 6:
                        # choose prefix length 6 or 7 based on length
                        for L in (6, 7, 8):
                            if len(d) > L:
                                pref = d[:L]
                                prefixes[pref] = prefixes.get(pref, 0) + 1
                                break
    except Exception as e:
        logger.debug("discover_ranges error: %s", e)
    # sort by frequency and return top candidates
    sorted_prefs = sorted(prefixes.items(), key=lambda x: x[1], reverse=True)
    return [p for p, _ in sorted_prefs[:10]]


# ---------- Polling job (strict matching) ----------
def polling_job(context: CallbackContext) -> None:
    job_ctx = context.job.context
    chat_id = job_ctx["chat_id"]
    entry = state.get(str(chat_id))
    if not entry:
        return
    number = entry.get("number")
    if not number:
        return

    num_digits = entry.get("digits") or digits_only(number)
    last_variants = entry.get("last_variants") or last_n_variants(num_digits)
    logger.info("Polling chat=%s number=%s variants=%s", chat_id, number, last_variants)

    # dates to try
    dates: List[str] = []
    allocated_at = entry.get("allocated_at")
    if allocated_at:
        try:
            dt = datetime.fromtimestamp(int(allocated_at), tz=timezone.utc)
            dates.append(dt.strftime("%Y-%m-%d"))
        except Exception:
            pass
    today = datetime.now(timezone.utc)
    dates.append(today.strftime("%Y-%m-%d"))
    dates.append((today - timedelta(days=1)).strftime("%Y-%m-%d"))

    try:
        for date_str in dates:
            for status in (None, "success"):
                for page in range(1, 6):
                    try:
                        resp = fetch_info(date_str, page=page, status=status)
                    except Exception as e:
                        logger.debug("fetch_info failed date=%s page=%d status=%s error=%s", date_str, page, status, e)
                        continue
                    data = resp.get("data")
                    if not data:
                        continue
                    entries = data if isinstance(data, list) else [data]
                    for api_entry in entries:
                        # Strict matching:
                        # 1) Prefer explicit fields
                        explicit_number = api_entry.get("full_number") or api_entry.get("number") or api_entry.get("copy")
                        explicit_digits = digits_only(explicit_number) if explicit_number else ""
                        matched = False
                        if explicit_digits:
                            # require the saved digits to be substring of explicit_digits (exact match)
                            if num_digits and (num_digits == explicit_digits or num_digits in explicit_digits or explicit_digits in num_digits):
                                matched = True
                        if not matched:
                            # 2) fallback: check flattened text but only if it contains the full allocated digits
                            flat = flatten_values(api_entry)
                            flat_digits = digits_only(flat)
                            if num_digits and num_digits in flat_digits:
                                matched = True
                        if not matched:
                            continue

                        # Now we are confident this API entry refers to our number
                        message_text = extract_message_text(api_entry) or flatten_values(api_entry)
                        otp = extract_otp_from_text(message_text) or extract_otp_from_text(flatten_values(api_entry))
                        status_field = (api_entry.get("status") or "") or ""
                        logger.info("Found matching info entry for chat=%s status=%s otp=%s", chat_id, status_field, bool(otp))

                        # Only mark expired if explicit status indicates it or message explicitly mentions expired/failed for this number
                        provider_says_expired = False
                        if isinstance(status_field, str) and ("expired" in status_field.lower() or "failed" in status_field.lower()):
                            provider_says_expired = True
                        else:
                            # check message text for clear indicators tied to this number
                            low = (message_text or "").lower()
                            if ("expired" in low or "failed" in low) and num_digits and num_digits in digits_only(message_text):
                                provider_says_expired = True

                        if otp and not entry.get("otp"):
                            entry["otp"] = otp
                            entry["status"] = "success"
                            save_state()
                            pretty = format_pretty_number(number)
                            tnow = datetime.now().strftime("%I:%M %p")
                            sms_text = html.escape(message_text or "")
                            card = MSG_OTP_CARD.format(sep=CARD_SEPARATOR, otp=html.escape(str(otp)), pretty_number=pretty, country=entry.get("country", "Unknown"), time=tnow, sms_text=sms_text)
                            # send to user
                            try:
                                context.bot.send_message(chat_id=chat_id, text=card, parse_mode=ParseMode.HTML, reply_markup=make_inline_buttons_for_otp(otp))
                                if message_text:
                                    context.bot.send_message(chat_id=chat_id, text=f"Full message:\n{message_text}")
                                context.bot.send_message(chat_id=chat_id, text=f"🔐 OTP: <code>{html.escape(str(otp))}</code>", parse_mode=ParseMode.HTML)
                            except Exception as se:
                                logger.warning("Failed to send OTP to user %s: %s", chat_id, se)
                            # forward to group
                            try:
                                context.bot.send_message(chat_id=FORWARD_CHAT_ID, text=card, parse_mode=ParseMode.HTML)
                                if message_text:
                                    context.bot.send_message(chat_id=FORWARD_CHAT_ID, text=f"Full message:\n{message_text}")
                                context.bot.send_message(chat_id=FORWARD_CHAT_ID, text=f"🔐 OTP: <code>{html.escape(str(otp))}</code>", parse_mode=ParseMode.HTML)
                            except Exception as fg:
                                logger.warning("Failed to forward OTP to group %s: %s", FORWARD_CHAT_ID, fg)
                            # stop job
                            job_obj = jobs_registry.pop(str(chat_id), None)
                            if job_obj:
                                try:
                                    job_obj.schedule_removal()
                                except Exception:
                                    pass
                            return

                        if provider_says_expired:
                            entry["status"] = "expired"
                            save_state()
                            pretty = format_pretty_number(number)
                            try:
                                context.bot.send_message(chat_id=chat_id, text=MSG_EXPIRED.format(sep=CARD_SEPARATOR, pretty_number=pretty), reply_markup=make_inline_buttons_after_timeout())
                            except Exception:
                                pass
                            job_obj = jobs_registry.pop(str(chat_id), None)
                            if job_obj:
                                try:
                                    job_obj.schedule_removal()
                                except Exception:
                                    pass
                            return
        # no match yet
    except Exception as e:
        logger.warning("Polling job error for chat %s: %s", chat_id, e)


# ---------- Handlers ----------
def start(update: Update, context: CallbackContext) -> None:
    try:
        rk = ReplyKeyboardMarkup(MAIN_MENU_KEYS, resize_keyboard=True, one_time_keyboard=False)
        update.message.reply_text("👋 Welcome!\n" + MSG_HELPER, reply_markup=rk)
    except Exception:
        update.message.reply_text("👋 Welcome!\n" + MSG_HELPER)


def status_cmd(update: Update, context: CallbackContext) -> None:
    chat_id = update.effective_chat.id
    ent = state.get(str(chat_id))
    if not ent:
        update.message.reply_text("No active number. Use /range to get one.\n\n" + MSG_HELPER)
        return
    number = ent.get("number")
    st = ent.get("status", "pending")
    otp = ent.get("otp")
    pretty_number = format_pretty_number(number)
    status_map = {"pending": "⏳ Waiting for OTP…", "success": "✅ OTP Received", "expired": "❌ Expired"}
    friendly = status_map.get(st, st.capitalize())
    card_text = (
        f"{CARD_SEPARATOR}\n"
        f"📱 Country: {ent.get('country','Unknown')}\n"
        f"📞 Phone: {pretty_number}\n"
        f"🔢 Range: {ent.get('range')}\n"
        f"{CARD_SEPARATOR}\n"
        f"Status: {friendly}"
    )
    if otp:
        card_text += f"\n\n🔐 OTP: <code>{html.escape(str(otp))}</code>"
    update.message.reply_text(card_text, reply_markup=make_inline_buttons(number), parse_mode=ParseMode.HTML)


def range_handler(update: Update, context: CallbackContext) -> None:
    chat_id = update.effective_chat.id
    if not context.args:
        update.message.reply_text("Send range: /range 261347435XXX or /range 261347435123\n\n" + MSG_HELPER)
        return
    raw = " ".join(context.args)
    rng = raw.strip()
    if "XXX" not in rng:
        digits = digits_only(rng)
        rng = (digits[:-3] + "XXX") if len(digits) > 3 else (digits + "XXX")
    msg = update.message.reply_text("Getting number — please wait...")
    try:
        alloc = allocate_number(rng)
    except Exception as e:
        msg.edit_text(MSG_ALLOCATION_ERROR.format(sep=CARD_SEPARATOR, short_error_message=str(e)))
        return

    meta = alloc.get("meta", {})
    if meta.get("code") != 200:
        msg.edit_text(MSG_ALLOCATION_ERROR.format(sep=CARD_SEPARATOR, short_error_message=str(alloc)))
        return

    data = alloc.get("data", {}) or {}
    full_number = data.get("full_number") or data.get("number") or data.get("copy")
    country = data.get("country") or data.get("iso") or "Unknown"
    if not full_number:
        msg.edit_text(MSG_ALLOCATION_ERROR.format(sep=CARD_SEPARATOR, short_error_message=str(alloc)))
        return

    digits = digits_only(full_number)
    last_variants = last_n_variants(digits)
    state[str(chat_id)] = {
        "range": rng,
        "number": full_number,
        "digits": digits,
        "last_variants": last_variants,
        "country": country,
        "allocated_at": int(time.time()),
        "status": data.get("status", "pending"),
        "otp": None,
    }
    save_state()

    pretty_number = format_pretty_number(full_number)
    msg.edit_text(MSG_ALLOCATION_CARD.format(sep=CARD_SEPARATOR, country=country, pretty_number=pretty_number, range=rng), reply_markup=make_inline_buttons(full_number))

    if str(chat_id) not in jobs_registry:
        job = context.job_queue.run_repeating(polling_job, interval=POLL_INTERVAL, first=5, context={"chat_id": chat_id})
        jobs_registry[str(chat_id)] = job


def discover_handler(update: Update, context: CallbackContext) -> None:
    """
    /discover [optional-prefix]
    Scans recent /info and suggests candidate ranges (prefixes) that appear active.
    """
    chat_id = update.effective_chat.id
    arg = None
    if context.args:
        arg = context.args[0].strip()
    update.message.reply_text("Scanning provider data for active prefixes — please wait...")
    candidates = discover_ranges_from_info()
    if arg:
        # prioritize prefixes that start with arg digits
        arg_digits = digits_only(arg)
        candidates = [p for p in candidates if p.startswith(arg_digits)] + [p for p in candidates if not p.startswith(arg_digits)]
    if not candidates:
        update.message.reply_text("No candidate prefixes found. Try again later or provide a longer starting prefix.")
        return
    text = "Found candidate prefixes (tap to use as range):\n"
    kb = []
    for p in candidates[:8]:
        rng = p + ("XXX" if len(p) < 10 else "XXX")
        text += f"- {rng}\n"
        kb.append([InlineKeyboardButton(f"Use {rng}", callback_data=f"use_range|{rng}")])
    update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))


def callback_query_handler(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    data = query.data or ""
    chat_id = query.message.chat.id

    if data.startswith("use_range|"):
        _, rng = data.split("|", 1)
        rng = rng.strip()
        query.answer()
        query.edit_message_text(f"Using range {rng} — requesting number...")
        # call allocation flow (reuse code)
        try:
            alloc = allocate_number(rng)
        except Exception as e:
            query.edit_message_text(MSG_ALLOCATION_ERROR.format(sep=CARD_SEPARATOR, short_error_message=str(e)))
            return
        meta = alloc.get("meta", {})
        if meta.get("code") != 200:
            query.edit_message_text(MSG_ALLOCATION_ERROR.format(sep=CARD_SEPARATOR, short_error_message=str(alloc)))
            return
        data = alloc.get("data", {}) or {}
        full_number = data.get("full_number") or data.get("number") or data.get("copy")
        country = data.get("country") or "Unknown"
        if not full_number:
            query.edit_message_text(MSG_ALLOCATION_ERROR.format(sep=CARD_SEPARATOR, short_error_message=str(alloc)))
            return
        digits = digits_only(full_number)
        last_variants = last_n_variants(digits)
        state[str(chat_id)] = {
            "range": rng,
            "number": full_number,
            "digits": digits,
            "last_variants": last_variants,
            "country": country,
            "allocated_at": int(time.time()),
            "status": data.get("status", "pending"),
            "otp": None,
        }
        save_state()
        pretty_number = format_pretty_number(full_number)
        query.edit_message_text(MSG_ALLOCATION_CARD.format(sep=CARD_SEPARATOR, country=country, pretty_number=pretty_number, range=rng), reply_markup=make_inline_buttons(full_number))
        if str(chat_id) not in jobs_registry:
            job = context.job_queue.run_repeating(polling_job, interval=POLL_INTERVAL, first=5, context={"chat_id": chat_id})
            jobs_registry[str(chat_id)] = job
        return

    if data.startswith("copy|"):
        _, number = data.split("|", 1)
        pretty = format_pretty_number(number)
        try:
            query.answer(text=pretty, show_alert=True)
        except Exception:
            pass
        try:
            context.bot.send_message(chat_id=chat_id, text=f"Number (tap & hold to copy):\n{pretty}")
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
            context.bot.send_message(chat_id=chat_id, text=f"OTP (tap & hold to copy):\n{otp}")
            context.bot.send_message(chat_id=chat_id, text=MSG_COPY_CONFIRM)
        except Exception:
            pass
        return

    try:
        query.answer()
    except Exception:
        pass

    if data == "change":
        ent = state.get(str(chat_id))
        if not ent:
            query.edit_message_text("No active allocation. Use /range to get a number.")
            return
        rng = ent.get("range")
        query.edit_message_text("🔁 Requesting a new number — please wait...")
        try:
            alloc = allocate_number(rng)
        except Exception as e:
            query.edit_message_text(MSG_ALLOCATION_ERROR.format(sep=CARD_SEPARATOR, short_error_message=str(e)))
            return
        meta = alloc.get("meta", {})
        if meta.get("code") != 200:
            query.edit_message_text(MSG_ALLOCATION_ERROR.format(sep=CARD_SEPARATOR, short_error_message=str(alloc)))
            return
        data = alloc.get("data", {}) or {}
        full_number = data.get("full_number") or data.get("number") or data.get("copy")
        country = data.get("country") or ent.get("country", "Unknown")
        digits = digits_only(full_number)
        last_variants = last_n_variants(digits)
        state[str(chat_id)] = {
            "range": rng,
            "number": full_number,
            "digits": digits,
            "last_variants": last_variants,
            "country": country,
            "allocated_at": int(time.time()),
            "status": data.get("status", "pending"),
            "otp": None,
        }
        save_state()
        pretty = format_pretty_number(full_number)
        query.edit_message_text(MSG_ALLOCATION_CARD.format(sep=CARD_SEPARATOR, country=country, pretty_number=pretty, range=rng), reply_markup=make_inline_buttons(full_number))
        return

    if data.startswith("cancel|"):
        try:
            _, number = data.split("|", 1)
        except Exception:
            number = None
        ent = state.get(str(chat_id))
        if ent and (not number or ent.get("number") == number):
            ent["status"] = "expired"
            save_state()
            pretty = format_pretty_number(number or ent.get("number"))
            query.edit_message_text(MSG_EXPIRED.format(sep=CARD_SEPARATOR, pretty_number=pretty), reply_markup=make_inline_buttons_after_timeout())
            job_obj = jobs_registry.pop(str(chat_id), None)
            if job_obj:
                try:
                    job_obj.schedule_removal()
                except Exception:
                    pass
            return
        query.edit_message_text("No matching active number to cancel.")
        return

    if data == "back":
        query.edit_message_text("⬅ Back to Menu\nUse /range to allocate or /status to view current number.")
        return


def discover_callback_example_text() -> str:
    return "Use /discover to scan provider data and suggest active prefixes (ranges)."


def unknown(update: Update, context: CallbackContext) -> None:
    update.message.reply_text("Unknown command. Use /start, /range <range>, /discover, /status")


def history_command(update: Update, context: CallbackContext) -> None:
    chat_id = update.effective_chat.id
    ent = state.get(str(chat_id))
    if not ent:
        update.message.reply_text(f"{CARD_SEPARATOR}\n📜 History\n{CARD_SEPARATOR}\nNo history available yet.")
        return
    pretty = format_pretty_number(ent.get("number"))
    allocated_time = datetime.fromtimestamp(ent.get("allocated_at")).strftime("%Y-%m-%d %H:%M:%S")
    otp_line = f"🔐 OTP: <code>{html.escape(str(ent['otp']))}</code>" if ent.get("otp") else ""
    history_text = (
        f"{CARD_SEPARATOR}\n"
        f"📞 {pretty}\n"
        f"🗺 {ent.get('country','Unknown')}\n"
        f"🔢 Range: {ent.get('range')}\n"
        f"📅 Allocated: {allocated_time}\n"
        f"🧾 Status: {ent.get('status')}\n"
        f"{otp_line}\n"
        f"{CARD_SEPARATOR}"
    )
    update.message.reply_text(history_text, parse_mode=ParseMode.HTML)


# ---------- Startup / Telegram lifecycle ----------
_updater_global: Optional[Updater] = None
_updater_lock = threading.Lock()


def start_telegram_updater(bot: Bot) -> None:
    global _updater_global
    with _updater_lock:
        if _updater_global:
            return
        updater = Updater(bot=bot, use_context=True)
        dp = updater.dispatcher
        dp.add_handler(CommandHandler("start", start))
        dp.add_handler(CommandHandler("range", range_handler))
        dp.add_handler(CommandHandler("status", status_cmd))
        dp.add_handler(CommandHandler("history", history_command))
        dp.add_handler(CommandHandler("discover", discover_handler))
        dp.add_handler(CallbackQueryHandler(callback_query_handler))
        dp.add_handler(MessageHandler(Filters.command, unknown))
        try:
            updater.start_polling()
            logger.info("Telegram updater started.")
            _updater_global = updater
            on_startup_jobs_updater(updater)
        except Conflict:
            logger.error("Conflict: another getUpdates process is running for this token.")
        except Unauthorized:
            logger.error("Unauthorized when starting polling; check BOT_TOKEN.")


def token_watcher_loop() -> None:
    global BOT_TOKEN
    while True:
        if not BOT_TOKEN:
            logger.warning("BOT_TOKEN not set. Waiting for BOT_TOKEN env to be provided.")
            time.sleep(10)
            continue
        try:
            bot = Bot(BOT_TOKEN)
            me = bot.get_me()
            logger.info("Bot validated: %s (id=%s)", getattr(me, "username", ""), getattr(me, "id", ""))
            try:
                bot.delete_webhook()
            except Exception:
                pass
            start_telegram_updater(bot)
            return
        except Unauthorized as e:
            logger.error("BOT_TOKEN invalid/unauthorized. Will retry in 20s.")
            time.sleep(20)
            continue
        except NetworkError as ne:
            logger.warning("Network error validating token: %s. Retry in 5s.", ne)
            time.sleep(5)
            continue
        except Exception as e:
            logger.warning("Unexpected error validating token: %s. Retry in 10s.", e)
            time.sleep(10)
            continue


def on_startup_jobs_updater(updater: Updater) -> None:
    jq = updater.job_queue
    for chat_id, ent in state.items():
        if ent.get("number") and ent.get("status") != "expired" and not ent.get("otp"):
            try:
                job = jq.run_repeating(polling_job, interval=POLL_INTERVAL, first=5, context={"chat_id": int(chat_id)})
                jobs_registry[chat_id] = job
                logger.info("Restarted polling job for chat %s", chat_id)
            except Exception as e:
                logger.warning("Could not restart job for %s: %s", chat_id, e)


def main() -> None:
    load_state()
    t = threading.Thread(target=token_watcher_loop, daemon=True)
    t.start()
    logger.info("Service running. Telegram will start when BOT_TOKEN is valid.")
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        logger.info("Shutting down.")
    global _updater_global
    with _updater_lock:
        if _updater_global:
            try:
                _updater_global.stop()
            except Exception:
                pass


if __name__ == "__main__":
    main()