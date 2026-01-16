#!/usr/bin/env python3
"""
Improved Telegram OTP bot:
- Allocate number via MNIT API (/mapi/v1/mdashboard/getnum/number)
- Poll info endpoint (/mapi/v1/mdashboard/getnum/info) until API marks expired or OTP arrives
- No local 3-minute expiry; expiry only when API shows expired/failed
- UI: formatted messages + inline buttons (Copy, Change Number, Back)
- Per-user JSON persistence (state.json) to survive restarts (basic)
Env:
- BOT_TOKEN (recommended) or hardcoded fallback
- MNIT_API_KEY (recommended)
"""
import os
import re
import json
import time
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Tuple

import requests
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ParseMode
)
from telegram.ext import (
    Updater, CommandHandler, CallbackContext, CallbackQueryHandler, MessageHandler, Filters
)

# ---------- Config ----------
BOT_TOKEN = os.getenv("BOT_TOKEN") or "7108794200:AAGWA3aGPDjdYkXJ1VlOSdxBMHtuFpWzAIU"
MNIT_API_KEY = os.getenv("MNIT_API_KEY") or "M_WH9Q3U88V"

ALLOCATE_URL = "https://x.mnitnetwork.com/mapi/v1/mdashboard/getnum/number"
INFO_URL = "https://x.mnitnetwork.com/mapi/v1/mdashboard/getnum/info"

HEADERS = {"Content-Type": "application/json", "mapikey": MNIT_API_KEY}

STATE_FILE = "state.json"   # basic persistence
POLL_INTERVAL = 15          # seconds between API polls for each active allocation

# ---------- Logging ----------
logging.basicConfig(format='[%(asctime)s] %(levelname)s: %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- In-memory state ----------
# structure: {chat_id: { "range": str, "number": str, "country": str, "allocated_at": ts,
#                       "status": "pending|success|failed|expired", "otp": str or None,
#                       "job_name": str}}
state: Dict[str, Dict[str, Any]] = {}
jobs_registry: Dict[str, Any] = {}  # chat_id -> job (for cancelling)


# ---------- Persistence helpers ----------
def load_state():
    global state
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                state = json.load(f)
            logger.info("Loaded state for %d users", len(state))
    except Exception as e:
        logger.warning("Failed loading state: %s", e)


def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        logger.warning("Failed saving state: %s", e)


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
    text = re.sub(r"[|:]+", " ", text)
    m = re.search(r"\b(\d{4,8})\b", text)
    if m:
        return m.group(1)
    m2 = re.search(r"\b([A-Z]{1,4}[-_]\d{3,8})\b", text, flags=re.IGNORECASE)
    if m2:
        return m2.group(1)
    m3 = re.search(r"[<#>]{1,3}\s*([0-9]{4,8})\b", text)
    if m3:
        return m3.group(1)
    return None


def flatten_values(x: Any) -> str:
    if isinstance(x, dict):
        parts = []
        for v in x.values():
            parts.append(flatten_values(v))
        return " ".join(parts)
    if isinstance(x, list):
        return " ".join(flatten_values(i) for i in x)
    return str(x)


# ---------- MNIT API calls ----------
def allocate_number(range_str: str, timeout=30) -> Dict[str, Any]:
    payload = {"range": range_str, "is_national": None, "remove_plus": None}
    logger.info("Allocating range=%s", range_str)
    resp = requests.post(ALLOCATE_URL, json=payload, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def fetch_info(date_str: str, page: int = 1) -> Dict[str, Any]:
    params = {"date": date_str, "page": page, "search": "", "status": "success"}
    resp = requests.get(INFO_URL, headers=HEADERS, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


# ---------- Telegram UI formatting ----------
def format_alloc_block(country: str, number: str, range_str: str, msg_text: str, status_badge: str = "") -> str:
    block = "✅ OTP SUCCESS\n\n" if status_badge == "success" else ""
    block += f"Country: {country}\nNumber: {number}\nRange: {range_str}\n\nMessage:\n{msg_text}"
    return block


def make_buttons(number: str) -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton("📋 Copy Number", callback_data=f"copy|{number}")],
        [InlineKeyboardButton("🔁 Change Number", callback_data="change")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back")]
    ]
    return InlineKeyboardMarkup(kb)


# ---------- Background polling job ----------
def polling_job(context: CallbackContext):
    job_ctx = context.job.context
    chat_id = job_ctx["chat_id"]
    entry = state.get(str(chat_id))
    if not entry:
        # nothing to do
        return

    number = entry.get("number")
    if not number:
        return

    logger.info("Polling API for chat=%s number=%s", chat_id, number)
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        # try first page and a few pages (1..3)
        for page in range(1, 4):
            j = fetch_info(date_str, page=page)
            data = j.get("data")
            if not data:
                continue
            entries = data if isinstance(data, list) else [data]
            for e in entries:
                txt = flatten_values(e)
                if number in txt or number.replace("+", "") in txt:
                    # found related entry
                    otp = extract_otp_from_text(txt)
                    # decide status from entry keys if any
                    status_field = ""
                    if isinstance(e, dict):
                        status_field = e.get("status", "") or ""
                    # if OTP found and not already recorded -> send to user
                    if otp and not entry.get("otp"):
                        entry["otp"] = otp
                        entry["status"] = "success"
                        save_state()
                        # notify user
                        msg_text = txt
                        context.bot.send_message(
                            chat_id=chat_id,
                            text=format_alloc_block(country=entry.get("country", "Unknown"),
                                                    number=number,
                                                    range_str=entry.get("range", ""),
                                                    msg_text=msg_text,
                                                    status_badge="success"),
                        )
                        # concise OTP message
                        context.bot.send_message(chat_id=chat_id, text=f"🔐 OTP found: <code>{otp}</code>", parse_mode=ParseMode.HTML)
                        # stop job
                        jname = jobs_registry.pop(str(chat_id), None)
                        if jname:
                            try:
                                jname.schedule_removal()
                            except Exception:
                                pass
                        return
                    # detect failure/expired from entry (API-dependent)
                    if "failed" in status_field.lower() or "expired" in status_field.lower() or "failed" in txt.lower() or "expired" in txt.lower():
                        entry["status"] = "expired"
                        save_state()
                        context.bot.send_message(chat_id=chat_id, text=f"⚠️ Number {number} marked Expired by API.")
                        # stop job
                        jname = jobs_registry.pop(str(chat_id), None)
                        if jname:
                            try:
                                jname.schedule_removal()
                            except Exception:
                                pass
                        return
        # no relevant entry found this poll; continue
    except Exception as e:
        logger.warning("Polling job error for chat %s: %s", chat_id, e)
        # continue — transient errors should not stop job


# ---------- Telegram handlers ----------
def start(update: Update, context: CallbackContext):
    update.message.reply_text(
        "👋 Welcome!\nUse /range <range> to allocate a number.\nExample: /range 261347435XXX\n\nWhen a number is allocated the bot will wait for OTP until API reports expired/success."
    )


def status_cmd(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    ent = state.get(str(chat_id))
    if not ent:
        update.message.reply_text("No active number. Use /range to get one.")
        return
    number = ent.get("number")
    st = ent.get("status", "pending")
    otp = ent.get("otp")
    text = f"Number: {number}\nStatus: {st}"
    if otp:
        text += f"\nOTP: {otp}"
    update.message.reply_text(text, reply_markup=make_buttons(number))


def range_handler(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    if not context.args:
        update.message.reply_text("Send range: /range 261347435XXX or /range 261347435123")
        return
    raw = " ".join(context.args)
    rng = normalize_range(raw)
    msg = update.message.reply_text("Getting number — please wait...")
    try:
        alloc = allocate_number(rng)
    except Exception as e:
        msg.edit_text(f"Allocation failed: {e}")
        return

    meta = alloc.get("meta", {})
    if meta.get("code") != 200:
        msg.edit_text(f"Allocation error: {alloc}")
        return

    data = alloc.get("data", {})
    full_number = data.get("full_number") or data.get("number") or data.get("copy")
    country = data.get("country") or data.get("iso") or "Unknown"
    if not full_number:
        msg.edit_text(f"Allocation response contained no number: {alloc}")
        return

    # store state
    state[str(chat_id)] = {
        "range": rng,
        "number": full_number,
        "country": country,
        "allocated_at": int(time.time()),
        "status": data.get("status", "pending"),
        "otp": None
    }
    save_state()

    msg.edit_text(f"Number allocated: {full_number}\nNow watching API for OTP and expiry (no local 3-min expiry).", reply_markup=make_buttons(full_number))

    # start polling job (if not already)
    if str(chat_id) in jobs_registry:
        # already polling; update
        logger.info("Job exists for chat %s, leaving it", chat_id)
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
        # send the number as plain message (user can copy)
        context.bot.send_message(chat_id=chat_id, text=f"📋 Number: {number}")
        return
    if data == "change":
        # allocate a new number for same range
        ent = state.get(str(chat_id))
        if not ent:
            query.edit_message_text("No active allocation. Use /range to get a number.")
            return
        rng = ent.get("range")
        query.edit_message_text("Changing number — requesting new one...")
        try:
            alloc = allocate_number(rng)
        except Exception as e:
            query.edit_message_text(f"Allocation failed: {e}")
            return
        meta = alloc.get("meta", {})
        if meta.get("code") != 200:
            query.edit_message_text(f"Allocation error: {alloc}")
            return
        data = alloc.get("data", {})
        full_number = data.get("full_number") or data.get("number") or data.get("copy")
        country = data.get("country") or ent.get("country", "Unknown")
        # update state
        state[str(chat_id)] = {
            "range": rng,
            "number": full_number,
            "country": country,
            "allocated_at": int(time.time()),
            "status": data.get("status", "pending"),
            "otp": None
        }
        save_state()
        query.edit_message_text(f"Number changed: {full_number}\nNow watching API for OTP and expiry.", reply_markup=make_buttons(full_number))
        # ensure polling job exists
        if str(chat_id) not in jobs_registry:
            job = context.job_queue.run_repeating(polling_job, interval=POLL_INTERVAL, first=5, context={"chat_id": chat_id})
            jobs_registry[str(chat_id)] = job
        return
    if data == "back":
        query.edit_message_text("Back. Use /range to allocate or /status to view current number.")
        return


def unknown(update: Update, context: CallbackContext):
    update.message.reply_text("Unknown command. Use /start, /range <range>, /status")


def on_startup_jobs_updater(updater: Updater):
    # restart jobs for persisted state
    jq = updater.job_queue
    for chat_id, ent in state.items():
        if ent.get("number") and ent.get("status") != "expired" and not ent.get("otp"):
            try:
                job = jq.run_repeating(polling_job, interval=POLL_INTERVAL, first=5, context={"chat_id": int(chat_id)})
                jobs_registry[chat_id] = job
                logger.info("Restarted polling job for chat %s", chat_id)
            except Exception as e:
                logger.warning("Could not restart job for %s: %s", chat_id, e)


def main():
    load_state()
    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("range", range_handler))
    dp.add_handler(CommandHandler("status", status_cmd))
    dp.add_handler(CallbackQueryHandler(callback_query_handler))
    dp.add_handler(MessageHandler(Filters.command, unknown))

    # start polling
    updater.start_polling()
    on_startup_jobs_updater(updater)
    logger.info("Bot started.")
    updater.idle()


if __name__ == "__main__":
    main()
