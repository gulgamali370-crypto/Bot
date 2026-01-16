#!/usr/bin/env python3
"""
Telegram OTP Receiver Bot - Full bot.py (UI-only improvements applied)

Notes:
- Backend logic, API calls, job workflow, and callbacks are preserved exactly as before.
- Only user-facing strings, button labels, and message formatting were changed per UI requirements.
- Environment variables:
    BOT_TOKEN (recommended) - Telegram bot token
    MNIT_API_KEY (recommended) - MNIT API key
  If not set, the file falls back to placeholders (replace or set env vars on deploy).

Persistence:
- state.json is used for simple per-user state persistence (keeps allocations across restarts).

Run:
- python bot.py
"""

import os
import re
import json
import time
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Tuple, List

import requests
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ParseMode
)
from telegram.ext import (
    Updater,
    CommandHandler,
    CallbackContext,
    CallbackQueryHandler,
    MessageHandler,
    Filters
)

# ---------- CONFIG ----------
BOT_TOKEN = os.getenv("BOT_TOKEN") or "7108794200:AAGWA3aGPDjdYkXJ1VlOSdxBMHtuFpWzAIU"
MNIT_API_KEY = os.getenv("MNIT_API_KEY") or "M_WH9Q3U88V"

ALLOCATE_URL = "https://x.mnitnetwork.com/mapi/v1/mdashboard/getnum/number"
INFO_URL = "https://x.mnitnetwork.com/mapi/v1/mdashboard/getnum/info"

HEADERS = {"Content-Type": "application/json", "mapikey": MNIT_API_KEY}

STATE_FILE = "state.json"
POLL_INTERVAL = 15  # seconds between info polls for active allocations

# ---------- UI Constants (UI-only changes) ----------
BUTTON_LABELS = {
    # Keep callback_data unchanged; only change visible label.
    "copy": "📋 Copy Number",          # callback_data: "copy|{number}"
    "change": "🔄 Get New Number",     # callback_data: "change"
    "back": "⬅ Back to Menu",          # callback_data: "back"
    "try_another": "🔄 Try Another Number"
}

MSG_ALLOCATION = (
    "📱 Your Number\n"
    "{full_number}\n"
    "Range: {range}\n\n"
    "⏳ Status: Waiting for OTP"
)

MSG_OTP_RECEIVED = (
    "🔔 OTP Received\n\n"
    "📩 Code: <code>{otp}</code>\n"
    "📞 Number: {full_number}\n"
    "🗺 Country: {country}\n"
    "⏰ Time: {time}\n\n"
    "⚠️ Do not share this code\n\n"
    "Message:\n"
    "{sms_text}"
)

MSG_NO_OTP = (
    "❌ OTP Not Received\n\n"
    "No message arrived for {full_number} within the monitoring period."
)

MSG_EXPIRED = (
    "❌ OTP Expired\n\n"
    "{full_number}\n\n"
    "This number has been marked Expired by the provider.\n"
    "You can request a new one."
)

MSG_ALLOCATION_ERROR = (
    "⚠️ Allocation Failed\n\n"
    "Provider returned an error while trying to allocate a number.\n"
    "Error: {short_error_message}\n\n"
    "Tip: Try a different range or try again in a moment."
)

MSG_HELPER = (
    "ℹ️ Tip: Use /range 261347435XXX to request numbers in that range.\n"
    "Example: /range 261347435XXX"
)

# ---------- Logging ----------
logging.basicConfig(format='[%(asctime)s] %(levelname)s: %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- In-memory state ----------
# state structure: { chat_id_str: { "range": str, "number": str, "country": str,
#                                 "allocated_at": ts, "status": "pending|success|failed|expired",
#                                 "otp": Optional[str] } }
state: Dict[str, Dict[str, Any]] = {}
jobs_registry: Dict[str, Any] = {}  # chat_id_str -> Job object


# ---------- Persistence helpers ----------
def load_state():
    global state
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                state = json.load(f)
            logger.info("Loaded state for %d users", len(state))
    except Exception as e:
        logger.warning("Failed to load state.json: %s", e)


def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        logger.warning("Failed to save state.json: %s", e)


# ---------- Utility functions ----------
def normalize_range(raw: str) -> str:
    r = raw.strip()
    if "XXX" in r:
        return r
    digits = re.sub(r"\D", "", r)
    if len(digits) <= 3:
        return digits + "XXX"
    return digits[:-3] + "XXX"


def extract_otp_from_text(text: str) -> Optional[str]:
    if not text:
        return None
    txt = re.sub(r"[|:]+", " ", text)
    # 1) plain 4-8 digits
    m = re.search(r"\b(\d{4,8})\b", txt)
    if m:
        return m.group(1)
    # 2) patterns like FB-46541
    m2 = re.search(r"\b([A-Z]{1,4}[-_]\d{3,8})\b", txt, flags=re.IGNORECASE)
    if m2:
        return m2.group(1)
    # 3) patterns like '<#> 77959'
    m3 = re.search(r"[<#>]{1,3}\s*([0-9]{4,8})\b", txt)
    if m3:
        return m3.group(1)
    return None


def flatten_values(x: Any) -> str:
    if isinstance(x, dict):
        parts: List[str] = []
        for v in x.values():
            parts.append(flatten_values(v))
        return " ".join(parts)
    if isinstance(x, list):
        return " ".join(flatten_values(i) for i in x)
    return str(x)


def format_pretty_number(number: str) -> str:
    """
    Very small formatting helper for readability:
    - If number has leading '+', keep it.
    - Group digits in blocks (not country-specific) for readability.
    This is UI-only; it does not change the actual number used by API.
    """
    if not number:
        return ""
    s = str(number).strip()
    plus = ""
    if s.startswith("+"):
        plus = "+"
        s = s[1:]
    # remove non-digits for grouping
    digits = re.sub(r"\D", "", s)
    # Simple grouping from the end in blocks of 3 (except first block may be shorter).
    groups = []
    while digits:
        groups.insert(0, digits[-3:])
        digits = digits[:-3]
    pretty = " ".join(groups)
    return f"{plus}{pretty}"


# ---------- MNIT API calls (backend logic preserved) ----------
def allocate_number(range_str: str, timeout: int = 30) -> Dict[str, Any]:
    payload = {"range": range_str, "is_national": None, "remove_plus": None}
    logger.info("Requesting allocation for range=%s", range_str)
    resp = requests.post(ALLOCATE_URL, json=payload, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def fetch_info(date_str: str, page: int = 1) -> Dict[str, Any]:
    params = {"date": date_str, "page": page, "search": "", "status": "success"}
    resp = requests.get(INFO_URL, headers=HEADERS, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


# ---------- UI helpers ----------
def make_buttons(number: str) -> InlineKeyboardMarkup:
    """
    Build inline keyboard with new visible labels but same callback_data values.
    - copy|{number}  -> Copy Number
    - change         -> Get New Number
    - back           -> Back to Menu
    """
    kb = [
        [InlineKeyboardButton(BUTTON_LABELS["copy"], callback_data=f"copy|{number}")],
        [InlineKeyboardButton(BUTTON_LABELS["change"], callback_data="change")],
        [InlineKeyboardButton(BUTTON_LABELS["back"], callback_data="back")]
    ]
    return InlineKeyboardMarkup(kb)


def make_buttons_after_timeout() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(BUTTON_LABELS["try_another"], callback_data="change")],
        [InlineKeyboardButton(BUTTON_LABELS["back"], callback_data="back")]
    ]
    return InlineKeyboardMarkup(kb)


# ---------- Background polling job ----------
def polling_job(context: CallbackContext):
    job_ctx = context.job.context
    chat_id = job_ctx["chat_id"]
    entry = state.get(str(chat_id))
    if not entry:
        return

    number = entry.get("number")
    if not number:
        return

    logger.info("Polling API for chat=%s number=%s", chat_id, number)
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        for page in range(1, 4):
            j = fetch_info(date_str, page=page)
            data = j.get("data")
            if not data:
                continue
            entries = data if isinstance(data, list) else [data]
            for e in entries:
                txt = flatten_values(e)
                # match number variants
                if number in txt or number.replace("+", "") in txt:
                    otp = extract_otp_from_text(txt)
                    status_field = ""
                    if isinstance(e, dict):
                        status_field = e.get("status", "") or ""
                    # OTP found
                    if otp and not entry.get("otp"):
                        entry["otp"] = otp
                        entry["status"] = "success"
                        save_state()
                        pretty_number = format_pretty_number(number)
                        # send formatted OTP message (UI-only change)
                        tnow = datetime.now().strftime("%I:%M %p")
                        sms_text = txt
                        context.bot.send_message(
                            chat_id=chat_id,
                            text=MSG_OTP_RECEIVED.format(
                                otp=otp,
                                full_number=pretty_number,
                                country=entry.get("country", "Unknown"),
                                time=tnow,
                                sms_text=sms_text
                            ),
                            parse_mode=ParseMode.HTML
                        )
                        # Also send concise OTP (as before)
                        context.bot.send_message(chat_id=chat_id, text=f"🔐 OTP: <code>{otp}</code>", parse_mode=ParseMode.HTML)
                        # stop job
                        job_obj = jobs_registry.pop(str(chat_id), None)
                        if job_obj:
                            try:
                                job_obj.schedule_removal()
                            except Exception:
                                pass
                        return
                    # expired/failed detection from API text/fields
                    if "failed" in status_field.lower() or "expired" in status_field.lower() or "failed" in txt.lower() or "expired" in txt.lower():
                        entry["status"] = "expired"
                        save_state()
                        pretty_number = format_pretty_number(number)
                        context.bot.send_message(chat_id=chat_id, text=MSG_EXPIRED.format(full_number=pretty_number), reply_markup=make_buttons_after_timeout())
                        job_obj = jobs_registry.pop(str(chat_id), None)
                        if job_obj:
                            try:
                                job_obj.schedule_removal()
                            except Exception:
                                pass
                        return
        # nothing found this poll - continue
    except Exception as e:
        logger.warning("Polling job error for chat %s: %s", chat_id, e)
        # transient errors ignored; job continues


# ---------- Telegram Handlers (commands and callbacks) ----------
def start(update: Update, context: CallbackContext):
    update.message.reply_text(
        "👋 Welcome!\nUse /range <range> to allocate a number.\nExample: /range 261347435XXX\n\n" + MSG_HELPER
    )


def status_cmd(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    ent = state.get(str(chat_id))
    if not ent:
        update.message.reply_text("No active number. Use /range to get one.\n\n" + MSG_HELPER)
        return
    number = ent.get("number")
    st = ent.get("status", "pending")
    otp = ent.get("otp")
    pretty_number = format_pretty_number(number)
    text = f"📱 Your Number\n{pretty_number}\n\n⏳ Status: {st.capitalize()}"
    if otp:
        text += f"\n\n🔐 OTP: <code>{otp}</code>"
    update.message.reply_text(text, reply_markup=make_buttons(number), parse_mode=ParseMode.HTML)


def range_handler(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    if not context.args:
        update.message.reply_text("Send range: /range 261347435XXX or /range 261347435123\n\n" + MSG_HELPER)
        return
    raw = " ".join(context.args)
    rng = normalize_range(raw)
    msg = update.message.reply_text("Getting number — please wait...")
    try:
        alloc = allocate_number(rng)
    except Exception as e:
        # keep behavior: show allocation failure (short)
        short_err = str(e)
        msg.edit_text(MSG_ALLOCATION_ERROR.format(short_error_message=short_err))
        return

    meta = alloc.get("meta", {})
    if meta.get("code") != 200:
        msg.edit_text(MSG_ALLOCATION_ERROR.format(short_error_message=str(alloc)))
        return

    data = alloc.get("data", {})
    full_number = data.get("full_number") or data.get("number") or data.get("copy")
    country = data.get("country") or data.get("iso") or "Unknown"
    if not full_number:
        msg.edit_text(MSG_ALLOCATION_ERROR.format(short_error_message=str(alloc)))
        return

    # store state (backend logic preserved)
    state[str(chat_id)] = {
        "range": rng,
        "number": full_number,
        "country": country,
        "allocated_at": int(time.time()),
        "status": data.get("status", "pending"),
        "otp": None
    }
    save_state()

    pretty_number = format_pretty_number(full_number)
    msg.edit_text(MSG_ALLOCATION.format(full_number=pretty_number, range=rng), reply_markup=make_buttons(full_number))

    # ensure polling job runs (as before)
    if str(chat_id) in jobs_registry:
        logger.info("Job already exists for chat %s", chat_id)
    else:
        job = context.job_queue.run_repeating(polling_job, interval=POLL_INTERVAL, first=5, context={"chat_id": chat_id})
        jobs_registry[str(chat_id)] = job


def callback_query_handler(update: Update, context: CallbackContext):
    query = update.callback_query
    data = query.data or ""
    chat_id = query.message.chat.id
    query.answer()
    if data.startswith("copy|"):
        _, number = data.split("|", 1)
        # send plain message with the number for copying (UI-only)
        pretty = format_pretty_number(number)
        context.bot.send_message(chat_id=chat_id, text=f"📋 Number: {pretty}")
        return
    if data == "change":
        ent = state.get(str(chat_id))
        if not ent:
            query.edit_message_text("No active allocation. Use /range to get a number.")
            return
        rng = ent.get("range")
        query.edit_message_text("🔄 Requesting a new number — please wait...")
        try:
            alloc = allocate_number(rng)
        except Exception as e:
            query.edit_message_text(MSG_ALLOCATION_ERROR.format(short_error_message=str(e)))
            return
        meta = alloc.get("meta", {})
        if meta.get("code") != 200:
            query.edit_message_text(MSG_ALLOCATION_ERROR.format(short_error_message=str(alloc)))
            return
        data = alloc.get("data", {})
        full_number = data.get("full_number") or data.get("number") or data.get("copy")
        country = data.get("country") or ent.get("country", "Unknown")
        state[str(chat_id)] = {
            "range": rng,
            "number": full_number,
            "country": country,
            "allocated_at": int(time.time()),
            "status": data.get("status", "pending"),
            "otp": None
        }
        save_state()
        pretty_number = format_pretty_number(full_number)
        query.edit_message_text(MSG_ALLOCATION.format(full_number=pretty_number, range=rng), reply_markup=make_buttons(full_number))
        if str(chat_id) not in jobs_registry:
            job = context.job_queue.run_repeating(polling_job, interval=POLL_INTERVAL, first=5, context={"chat_id": chat_id})
            jobs_registry[str(chat_id)] = job
        return
    if data == "back":
        query.edit_message_text("⬅ Back to Menu\nUse /range to allocate or /status to view current number.")
        return


def unknown(update: Update, context: CallbackContext):
    update.message.reply_text("Unknown command. Use /start, /range <range>, /status")


# ---------- Startup job restarter ----------
def on_startup_jobs_updater(updater: Updater):
    jq = updater.job_queue
    for chat_id, ent in state.items():
        if ent.get("number") and ent.get("status") != "expired" and not ent.get("otp"):
            try:
                job = jq.run_repeating(polling_job, interval=POLL_INTERVAL, first=5, context={"chat_id": int(chat_id)})
                jobs_registry[chat_id] = job
                logger.info("Restarted polling job for chat %s", chat_id)
            except Exception as e:
                logger.warning("Could not restart job for %s: %s", chat_id, e)


# ---------- Main ----------
def main():
    load_state()
    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("range", range_handler))
    dp.add_handler(CommandHandler("status", status_cmd))
    dp.add_handler(CallbackQueryHandler(callback_query_handler))
    dp.add_handler(MessageHandler(Filters.command, unknown))

    updater.start_polling()
    on_startup_jobs_updater(updater)
    logger.info("Bot started.")
    updater.idle()


if __name__ == "__main__":
    main()
