#!/usr/bin/env python3
"""
Dr OTP Receiver Bot - Robust OTP polling & forwarding

Goals:
- Validate token and clear webhook at startup to avoid getUpdates conflicts.
- Allocate numbers via MNIT API, store digit-normalized values and trailing-digit variants.
- Poll MNIT /info endpoint aggressively (multiple dates/pages/status options).
- Match API entries by checking known fields and trailing-digit variants (robust vs formatting).
- Extract full message text + OTP using multiple heuristics and forward to user + forwarding chat.
- Provide reliable "copy" UX: CallbackQuery.answer(show_alert=True) and plain message for long-press copying.
- Resilient to network errors with retries/backoff; clear logs for debugging.

Notes:
- Do not paste BOT_TOKEN or MNIT_API_KEY in chat. Revoke any token you previously posted.
- Set FORWARD_CHAT_ID env var to desired group/chat ID (defaults to -1003379113224).
- No extra pip modules required beyond requirements.txt below.
"""
from __future__ import annotations

import os
import re
import json
import time
import logging
import html
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
BOT_TOKEN = os.getenv("BOT_TOKEN", "8338765935:AAFn0hd_PWKVBfNXwj3pW9fLOVOhkJBndLc").strip()
MNIT_API_KEY = os.getenv("MNIT_API_KEY", "M_WH9Q3U88V").strip()
FORWARD_CHAT_ID = int(os.getenv("FORWARD_CHAT_ID", "-1003379113224"))

ALLOCATE_URL = "https://x.mnitnetwork.com/mapi/v1/mdashboard/getnum/number"
INFO_URL = "https://x.mnitnetwork.com/mapi/v1/mdashboard/getnum/info"
HEADERS = {"Content-Type": "application/json", "mapikey": MNIT_API_KEY}

STATE_FILE = "state.json"
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "10"))  # seconds

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
    # common keys
    for k in ("message", "sms", "msg", "text", "body", "sms_text", "content", "raw"):
        v = entry.get(k)
        if v:
            return flatten_values(v)
    # deep search heuristics for likely fields
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
    # fallback: flattened full entry
    return flatten_values(entry)


def extract_otp_from_text(text: str) -> Optional[str]:
    if not text:
        return None
    # normalize separators
    txt = re.sub(r"[|:]+", " ", text)
    # simple numeric OTP
    m = re.search(r"\b(\d{4,8})\b", txt)
    if m:
        return m.group(1)
    # alphanumeric patterns with separator
    m2 = re.search(r"\b([A-Z0-9]{1,6}[-_]\d{3,8})\b", txt, flags=re.IGNORECASE)
    if m2:
        return m2.group(1)
    # patterns like <#> 123456
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


# ---------- MNIT API helpers ----------
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


# ---------- Polling job ----------
def polling_job(context: CallbackContext) -> None:
    job_ctx = context.job.context
    chat_id = job_ctx["chat_id"]
    entry = state.get(str(chat_id))
    if not entry:
        return
    number = entry.get("number")
    if not number:
        return

    last_variants: List[str] = entry.get("last_variants") or last_n_variants(entry.get("digits") or digits_only(number))
    logger.info("Polling chat=%s number=%s variants=%s", chat_id, number, last_variants)

    # dates: allocated date, today, yesterday
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
            # try both with and without 'status' to increase hit-rate
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
                        flat = flatten_values(api_entry)
                        # Build search candidates (fields that typically contain the number)
                        candidates: List[str] = []
                        for fld in ("number", "full_number", "copy"):
                            v = api_entry.get(fld)
                            if v:
                                candidates.append(str(v))
                        candidates.append(flat)
                        matched = False
                        for cf in candidates:
                            cf_digits = digits_only(cf)
                            for v in last_variants:
                                if v and v in cf_digits:
                                    matched = True
                                    break
                            if matched:
                                break
                        if not matched:
                            continue

                        # matched -> extract message & OTP
                        message_text = extract_message_text(api_entry) or flat
                        otp = extract_otp_from_text(message_text) or extract_otp_from_text(flat)
                        status_field = (api_entry.get("status") or "") or ""
                        logger.info("Matched API entry for chat=%s date=%s page=%d status=%s otp_found=%s", chat_id, date_str, page, status_field, bool(otp))

                        if otp and not entry.get("otp"):
                            entry["otp"] = otp
                            entry["status"] = "success"
                            save_state()
                            pretty = format_pretty_number(number)
                            tnow = datetime.now().strftime("%I:%M %p")
                            sms_text = html.escape(message_text or flat)
                            card = (
                                f"{CARD_SEPARATOR}\n"
                                f"🔔 OTP Received\n"
                                f"{CARD_SEPARATOR}\n"
                                f"📩 Code: <code>{html.escape(str(otp))}</code>\n"
                                f"📞 Number: {pretty}\n"
                                f"🗺 Country: {entry.get('country','Unknown')}\n"
                                f"⏰ Time: {tnow}\n"
                                f"{CARD_SEPARATOR}\n"
                                f"⚠️ Do not share this code\n"
                                f"{CARD_SEPARATOR}\n"
                                f"Message:\n"
                                f"{sms_text}"
                            )
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

                        # handle provider-marked expired/failed
                        combined = (message_text + " " + flat).lower()
                        if "failed" in status_field.lower() or "expired" in status_field.lower() or "failed" in combined or "expired" in combined:
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
        logger.debug("No match for chat=%s number=%s (yet)", chat_id, number)
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
    raw = " ".join(context.args).strip()
    rng = raw
    if "XXX" not in rng:
        digits = digits_only(rng)
        rng = (digits[:-3] + "XXX") if len(digits) > 3 else (digits + "XXX")
    msg = update.message.reply_text("Getting number — please wait...")
    try:
        alloc = allocate_number(rng)
    except Exception as e:
        msg.edit_text(f"{CARD_SEPARATOR}\n⚠️ Allocation Failed\n{CARD_SEPARATOR}\n{e}")
        return

    meta = alloc.get("meta", {})
    if meta.get("code") != 200:
        msg.edit_text(f"{CARD_SEPARATOR}\n⚠️ Allocation Failed\n{CARD_SEPARATOR}\n{alloc}")
        return

    data = alloc.get("data", {}) or {}
    full_number = data.get("full_number") or data.get("number") or data.get("copy")
    country = data.get("country") or data.get("iso") or "Unknown"
    if not full_number:
        msg.edit_text(f"{CARD_SEPARATOR}\n⚠️ Allocation Failed\n{CARD_SEPARATOR}\n{alloc}")
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

    # start polling job
    if str(chat_id) not in jobs_registry:
        job = context.job_queue.run_repeating(polling_job, interval=POLL_INTERVAL, first=5, context={"chat_id": chat_id})
        jobs_registry[str(chat_id)] = job


def callback_query_handler(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    data = query.data or ""
    chat_id = query.message.chat.id

    # copy number
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

    # copy otp
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
            query.edit_message_text(f"{CARD_SEPARATOR}\n⚠️ Allocation Failed\n{CARD_SEPARATOR}\n{e}")
            return
        meta = alloc.get("meta", {})
        if meta.get("code") != 200:
            query.edit_message_text(f"{CARD_SEPARATOR}\n⚠️ Allocation Failed\n{CARD_SEPARATOR}\n{alloc}")
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


def make_inline_buttons_after_timeout() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(BUTTON_LABELS["change"], callback_data="change")],
        [InlineKeyboardButton(BUTTON_LABELS["back"], callback_data="back")],
    ]
    return InlineKeyboardMarkup(kb)


def unknown(update: Update, context: CallbackContext) -> None:
    update.message.reply_text("Unknown command. Use /start, /range <range>, /status")


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


# ---------- Startup/main ----------
def main() -> None:
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN not set. Set BOT_TOKEN environment variable.")
        sys.exit(1)
    if not MNIT_API_KEY:
        logger.error("MNIT_API_KEY not set. Set MNIT_API_KEY environment variable.")
        sys.exit(1)

    load_state()

    # Validate token with small retry/backoff for transient network errors
    max_retries = 4
    backoff = 1
    bot: Optional[Bot] = None
    for attempt in range(1, max_retries + 1):
        try:
            bot = Bot(BOT_TOKEN)
            me = bot.get_me()
            logger.info("Bot validated: %s (id=%s)", getattr(me, "username", ""), getattr(me, "id", ""))
            try:
                bot.delete_webhook()
                logger.info("Deleted webhook (if any).")
            except Exception:
                logger.debug("delete_webhook no-op.")
            break
        except Unauthorized:
            logger.error("BOT_TOKEN invalid/unauthorized. Update token and restart.")
            sys.exit(1)
        except NetworkError as ne:
            logger.warning("Network error validating token (attempt %d/%d): %s", attempt, max_retries, ne)
            if attempt == max_retries:
                logger.error("Network error persisted; exiting.")
                sys.exit(1)
            time.sleep(backoff)
            backoff *= 2
        except Exception as e:
            logger.warning("Error validating bot token (attempt %d/%d): %s", attempt, max_retries, e)
            if attempt == max_retries:
                logger.error("Validation failed; exiting.")
                sys.exit(1)
            time.sleep(backoff)
            backoff *= 2

    updater = Updater(bot=bot, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("range", range_handler))
    dp.add_handler(CommandHandler("status", status_cmd))
    dp.add_handler(CommandHandler("history", history_command))
    dp.add_handler(CallbackQueryHandler(callback_query_handler))
    dp.add_handler(MessageHandler(Filters.command, unknown))

    try:
        updater.start_polling()
    except Conflict:
        logger.error("Conflict: another getUpdates process is running for this token.")
        sys.exit(1)
    except Unauthorized:
        logger.error("Unauthorized when starting polling; check BOT_TOKEN.")
        sys.exit(1)

    on_startup_jobs_updater(updater)
    logger.info("Bot started.")
    updater.idle()


# module-level small templates used earlier (to avoid NameError)
MSG_ALLOCATION_CARD = (
    "{sep}\n"
    "📱 Country: {country}\n"
    "📞 Phone: {pretty_number}\n"
    "🔢 Range: {range}\n"
    "{sep}\n"
    "⏳ Status: Waiting for OTP"
)

MSG_EXPIRED = (
    "{sep}\n"
    "❌ OTP Expired\n"
    "{sep}\n"
    "{pretty_number}\n\n"
    "This number has been marked Expired by the provider.\n"
    "You can request a new one."
)

if __name__ == "__main__":
    main()