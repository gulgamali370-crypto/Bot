#!/usr/bin/env python3
"""
Telegram OTP Receiver Bot - fixed syntax (no lone expressions next to string literals).
Forwards OTP to user and forwarding group. Shows alert + plain message for copy.
"""
import os
import re
import json
import time
import logging
import html
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List

import requests
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
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
BOT_TOKEN = os.getenv("BOT_TOKEN") or "7108794200:AAGD3dLoK6u9FfwOt7eVvRfuT3ZE9Hwyfuw"
MNIT_API_KEY = os.getenv("MNIT_API_KEY") or "M_WH9Q3U88V"
FORWARD_CHAT_ID = int(os.getenv("FORWARD_CHAT_ID", "-1003379113224"))

ALLOCATE_URL = "https://x.mnitnetwork.com/mapi/v1/mdashboard/getnum/number"
INFO_URL = "https://x.mnitnetwork.com/mapi/v1/mdashboard/getnum/info"

HEADERS = {"Content-Type": "application/json", "mapikey": MNIT_API_KEY}

STATE_FILE = "state.json"
POLL_INTERVAL = 12  # seconds between polls

# ---------- UI Templates & Buttons ----------
CARD_SEPARATOR = "━━━━━━━━━━━━━━━━━━━━━━━━━━"

BUTTON_LABELS = {
    "copy": "📋 Copy Number",
    "change": "🔁 Get New Number",
    "cancel": "❌ Cancel Number",
    "back": "⬅ Back to Menu",
    "copyotp": "📋 Copy OTP",
}

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

MSG_NO_OTP = (
    "{sep}\n"
    "❌ OTP Not Received\n"
    "{sep}\n"
    "No message arrived for {pretty_number} within the monitoring period."
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

MSG_COPY_CONFIRM = "✅ Sent — long-press the message or copy from the alert."

MSG_HELPER = "ℹ️ Tip: Use /range 261347435XXX to request numbers in that range."

MAIN_MENU_KEYS = [
    ["📲 Get Number"],
    ["📥 Active Numbers"],
    ["📜 History"],
    ["💰 Balance"],
    ["⚙ Settings"],
    ["📞 Support"]
]

# ---------- Logging ----------
logging.basicConfig(format='[%(asctime)s] %(levelname)s: %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- State ----------
state: Dict[str, Dict[str, Any]] = {}
jobs_registry: Dict[str, Any] = {}  # chat_id_str -> Job object


# ---------- Persistence ----------
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


# ---------- Utilities ----------
def normalize_range(raw: str) -> str:
    r = raw.strip()
    if "XXX" in r:
        return r
    digits = re.sub(r"\D", "", r)
    if len(digits) <= 3:
        return digits + "XXX"
    return digits[:-3] + "XXX"


def digits_only(s: str) -> str:
    if not s:
        return ""
    return re.sub(r"\D", "", str(s))


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


def flatten_values(x: Any) -> str:
    if isinstance(x, dict):
        parts: List[str] = []
        for v in x.values():
            parts.append(flatten_values(v))
        return " ".join(parts)
    if isinstance(x, list):
        return " ".join(flatten_values(i) for i in x)
    return str(x)


def extract_message_text(entry: Dict[str, Any]) -> str:
    for k in ("message", "sms", "msg", "text", "body", "sms_text", "content"):
        v = entry.get(k)
        if v:
            return flatten_values(v)
    return flatten_values(entry)


def format_pretty_number(number: str) -> str:
    if not number:
        return ""
    s = str(number).strip()
    plus = ""
    if s.startswith("+"):
        plus = "+"
        s = s[1:]
    digits = re.sub(r"\D", "", s)
    groups = []
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


def fetch_info(date_str: str, page: int = 1) -> Dict[str, Any]:
    params = {"date": date_str, "page": page, "search": "", "status": "success"}
    resp = requests.get(INFO_URL, headers=HEADERS, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


# ---------- Keyboards ----------
def make_inline_buttons(number: str) -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(BUTTON_LABELS["copy"], callback_data=f"copy|{number}")],
        [InlineKeyboardButton(BUTTON_LABELS["change"], callback_data="change")],
        [InlineKeyboardButton(BUTTON_LABELS["cancel"], callback_data=f"cancel|{number}")],
        [InlineKeyboardButton(BUTTON_LABELS["back"], callback_data="back")]
    ]
    return InlineKeyboardMarkup(kb)


def make_inline_buttons_after_timeout() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(BUTTON_LABELS["change"], callback_data="change")],
        [InlineKeyboardButton(BUTTON_LABELS["back"], callback_data="back")]
    ]
    return InlineKeyboardMarkup(kb)


def make_inline_buttons_for_otp(otp: str) -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(BUTTON_LABELS["copyotp"], callback_data=f"copyotp|{otp}")],
        [InlineKeyboardButton(BUTTON_LABELS["change"], callback_data="change")],
        [InlineKeyboardButton(BUTTON_LABELS["back"], callback_data="back")]
    ]
    return InlineKeyboardMarkup(kb)


# ---------- Polling job ----------
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
    num_digits = digits_only(number)

    dates_to_try = []
    allocated_at = entry.get("allocated_at")
    if allocated_at:
        try:
            dt = datetime.fromtimestamp(int(allocated_at), tz=timezone.utc)
            dates_to_try.append(dt.strftime("%Y-%m-%d"))
        except Exception:
            pass
    today = datetime.now(timezone.utc)
    dates_to_try.append(today.strftime("%Y-%m-%d"))
    dates_to_try.append((today - timedelta(days=1)).strftime("%Y-%m-%d"))

    seen_dates = set()
    try:
        for date_str in dates_to_try:
            if date_str in seen_dates:
                continue
            seen_dates.add(date_str)
            for page in range(1, 7):
                try:
                    j = fetch_info(date_str, page=page)
                except Exception as e:
                    logger.debug("fetch_info error for %s page %d: %s", date_str, page, e)
                    continue
                data = j.get("data")
                if not data:
                    continue
                entries = data if isinstance(data, list) else [data]
                for e in entries:
                    flattened = flatten_values(e)
                    flattened_digits = digits_only(flattened)
                    e_number = (e.get("number") or e.get("full_number") or e.get("copy") or "")
                    e_number_digits = digits_only(e_number)
                    matched = False
                    if num_digits:
                        if num_digits == e_number_digits or num_digits in e_number_digits or num_digits in flattened_digits:
                            matched = True
                    if not matched:
                        alt = num_digits.lstrip("0")
                        if alt and (alt in flattened_digits):
                            matched = True
                    if not matched:
                        continue

                    message_text = extract_message_text(e) or flattened
                    otp = extract_otp_from_text(message_text) or extract_otp_from_text(flattened)
                    status_field = (e.get("status") or "") or ""
                    if otp and not entry.get("otp"):
                        entry["otp"] = otp
                        entry["status"] = "success"
                        save_state()
                        pretty_number = format_pretty_number(number)
                        tnow = datetime.now().strftime("%I:%M %p")
                        sms_text = html.escape(message_text or flattened)
                        card = MSG_OTP_CARD.format(sep=CARD_SEPARATOR, otp=html.escape(str(otp)), pretty_number=pretty_number, country=entry.get("country", "Unknown"), time=tnow, sms_text=sms_text)
                        try:
                            context.bot.send_message(chat_id=chat_id, text=card, parse_mode=ParseMode.HTML, reply_markup=make_inline_buttons_for_otp(otp))
                            if message_text:
                                context.bot.send_message(chat_id=chat_id, text=f"Full message:\n{message_text}")
                            context.bot.send_message(chat_id=chat_id, text=f"🔐 OTP: <code>{html.escape(str(otp))}</code>", parse_mode=ParseMode.HTML)
                        except Exception as send_err:
                            logger.warning("Failed to send OTP to user %s: %s", chat_id, send_err)
                        try:
                            context.bot.send_message(chat_id=FORWARD_CHAT_ID, text=card, parse_mode=ParseMode.HTML)
                            if message_text:
                                context.bot.send_message(chat_id=FORWARD_CHAT_ID, text=f"Full message:\n{message_text}")
                            context.bot.send_message(chat_id=FORWARD_CHAT_ID, text=f"🔐 OTP: <code>{html.escape(str(otp))}</code>", parse_mode=ParseMode.HTML)
                        except Exception as fg_err:
                            logger.warning("Failed to forward OTP to group %s: %s", FORWARD_CHAT_ID, fg_err)
                        job_obj = jobs_registry.pop(str(chat_id), None)
                        if job_obj:
                            try:
                                job_obj.schedule_removal()
                            except Exception:
                                pass
                        return

                    combined = (message_text + " " + flattened).lower()
                    if "failed" in status_field.lower() or "expired" in status_field.lower() or "failed" in combined or "expired" in combined:
                        entry["status"] = "expired"
                        save_state()
                        pretty_number = format_pretty_number(number)
                        try:
                            context.bot.send_message(chat_id=chat_id, text=MSG_EXPIRED.format(sep=CARD_SEPARATOR, pretty_number=pretty_number), reply_markup=make_inline_buttons_after_timeout())
                        except Exception:
                            pass
                        job_obj = jobs_registry.pop(str(chat_id), None)
                        if job_obj:
                            try:
                                job_obj.schedule_removal()
                            except Exception:
                                pass
                        return
    except Exception as e:
        logger.warning("Polling job error for chat %s: %s", chat_id, e)


# ---------- Handlers ----------
def start(update: Update, context: CallbackContext):
    try:
        rk = ReplyKeyboardMarkup(MAIN_MENU_KEYS, resize_keyboard=True, one_time_keyboard=False)
        update.message.reply_text("👋 Welcome!\n" + MSG_HELPER, reply_markup=rk)
    except Exception:
        update.message.reply_text("👋 Welcome!\n" + MSG_HELPER)


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
    status_map = {"pending": "⏳ Waiting for OTP…", "success": "✅ OTP Received", "expired": "❌ Expired"}
    friendly = status_map.get(st, st.capitalize())
    card_text = (
        f"{CARD_SEPARATOR}\n"
        f"📱 Country: {ent.get('country', 'Unknown')}\n"
        f"📞 Phone: {pretty_number}\n"
        f"🔢 Range: {ent.get('range')}\n"
        f"{CARD_SEPARATOR}\n"
        f"Status: {friendly}"
    )
    if otp:
        card_text += f"\n\n🔐 OTP: <code>{html.escape(str(otp))}</code>"
    update.message.reply_text(card_text, reply_markup=make_inline_buttons(number), parse_mode=ParseMode.HTML)


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
        short_err = str(e)
        msg.edit_text(MSG_ALLOCATION_ERROR.format(sep=CARD_SEPARATOR, short_error_message=short_err))
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
    msg.edit_text(MSG_ALLOCATION_CARD.format(sep=CARD_SEPARATOR, country=country, pretty_number=pretty_number, range=rng), reply_markup=make_inline_buttons(full_number))

    if str(chat_id) not in jobs_registry:
        job = context.job_queue.run_repeating(polling_job, interval=POLL_INTERVAL, first=5, context={"chat_id": chat_id})
        jobs_registry[str(chat_id)] = job


def callback_query_handler(update: Update, context: CallbackContext):
    query = update.callback_query
    data = query.data or ""
    chat_id = query.message.chat.id

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
        query.edit_message_text(MSG_ALLOCATION_CARD.format(sep=CARD_SEPARATOR, country=country, pretty_number=pretty_number, range=rng), reply_markup=make_inline_buttons(full_number))
        if str(chat_id) not in jobs_registry:
            job = context.job_queue.run_repeating(polling_job, interval=POLL_INTERVAL, first=5, context={"chat_id": chat_id})
            jobs_registry[str(chat_id)] = job
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
        else:
            query.edit_message_text("No matching active number to cancel.")
            return

    if data == "back":
        query.edit_message_text("⬅ Back to Menu\nUse /range to allocate or /status to view current number.")
        return


def unknown(update: Update, context: CallbackContext):
    update.message.reply_text("Unknown command. Use /start, /range <range>, /status")


def menu_command(update: Update, context: CallbackContext):
    try:
        rk = ReplyKeyboardMarkup(MAIN_MENU_KEYS, resize_keyboard=True, one_time_keyboard=False)
        update.message.reply_text("🏠 Main Menu\n" + CARD_SEPARATOR, reply_markup=rk)
    except Exception:
        update.message.reply_text("🏠 Main Menu\n" + CARD_SEPARATOR)


def history_command(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    ent = state.get(str(chat_id))
    if not ent:
        update.message.reply_text(f"{CARD_SEPARATOR}\n📜 History\n{CARD_SEPARATOR}\nNo history available yet.")
        return
    pretty_number = format_pretty_number(ent.get("number"))
    allocated_time = datetime.fromtimestamp(ent.get("allocated_at")).strftime("%Y-%m-%d %H:%M:%S")
    otp_line = f"🔐 OTP: <code>{html.escape(str(ent['otp']))}</code>" if ent.get("otp") else ""
    history_text = (
        f"{CARD_SEPARATOR}\n"
        f"📞 {pretty_number}\n"
        f"🗺 {ent.get('country', 'Unknown')}\n"
        f"🔢 Range: {ent.get('range')}\n"
        f"📅 Allocated: {allocated_time}\n"
        f"🧾 Status: {ent.get('status')}\n"
        f"{otp_line}\n"
        f"{CARD_SEPARATOR}"
    )
    update.message.reply_text(history_text, parse_mode=ParseMode.HTML)


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


def main():
    load_state()
    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("range", range_handler))
    dp.add_handler(CommandHandler("status", status_cmd))
    dp.add_handler(CommandHandler("menu", menu_command))
    dp.add_handler(CommandHandler("history", history_command))

    dp.add_handler(CallbackQueryHandler(callback_query_handler))
    dp.add_handler(MessageHandler(Filters.command, unknown))

    updater.start_polling()
    on_startup_jobs_updater(updater)
    logger.info("Bot started.")
    updater.idle()


if __name__ == "__main__":
    main()