#!/usr/bin/env python3
"""
Async Telegram OTP Receiver using python-telegram-bot v20 + aiohttp

- Uses aiohttp for MNIT API (async HTTP).
- Uses asyncio tasks per chat to poll /info until OTP found or expired.
- Forwards OTP + full message to user and forwarding group (FORWARD_CHAT_ID).
- Copy buttons use show_alert and a plain message for long-press copy.
- Validates BOT_TOKEN on startup.
- State persisted to state.json.
"""
from __future__ import annotations

import os
import re
import json
import asyncio
import logging
import html
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List

import aiohttp
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ParseMode,
)
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

# ---------- CONFIG ----------
BOT_TOKEN = os.getenv("BOT_TOKEN", "8338765935:AAHnYQZjI7vlPf26RkaXnioKenEMp7RauPU").strip()
MNIT_API_KEY = os.getenv("MNIT_API_KEY", "M_WH9Q3U88V").strip()
FORWARD_CHAT_ID = int(os.getenv("FORWARD_CHAT_ID", "-1003379113224"))

ALLOCATE_URL = "https://x.mnitnetwork.com/mapi/v1/mdashboard/getnum/number"
INFO_URL = "https://x.mnitnetwork.com/mapi/v1/mdashboard/getnum/info"
HEADERS = {"Content-Type": "application/json", "mapikey": MNIT_API_KEY}

STATE_FILE = "state.json"
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "10"))  # seconds

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

logging.basicConfig(format="[%(asctime)s] %(levelname)s: %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- In-memory state ----------
# structure: { chat_id_str: { range, number, digits, last_variants, country, allocated_at, status, otp } }
state: Dict[str, Dict[str, Any]] = {}
tasks: Dict[str, asyncio.Task] = {}  # chat_id_str -> asyncio.Task


# ---------- Persistence ----------
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
        return " ".join(flatten_values(v) for v in x.values())
    if isinstance(x, list):
        return " ".join(flatten_values(i) for i in x)
    return str(x)


def extract_message_text(entry: Dict[str, Any]) -> str:
    for k in ("message", "sms", "msg", "text", "body", "sms_text", "content"):
        v = entry.get(k)
        if v:
            return flatten_values(v)
    # fallback
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
    groups = []
    while digits:
        groups.insert(0, digits[-3:])
        digits = digits[:-3]
    pretty = " ".join(groups)
    return f"{plus}{pretty}"


def make_inline_buttons(number: str) -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(BUTTON_LABELS["copy"], callback_data=f"copy|{number}")],
        [InlineKeyboardButton(BUTTON_LABELS["change"], callback_data="change")],
        [InlineKeyboardButton(BUTTON_LABELS["cancel"], callback_data=f"cancel|{number}")],
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


# ---------- MNIT API (async) ----------
async def allocate_number_async(range_str: str, session: aiohttp.ClientSession, timeout: int = 30) -> Dict[str, Any]:
    payload = {"range": range_str, "is_national": None, "remove_plus": None}
    logger.info("Requesting allocation for range=%s", range_str)
    async with session.post(ALLOCATE_URL, json=payload, headers=HEADERS, timeout=timeout) as resp:
        resp.raise_for_status()
        return await resp.json()


async def fetch_info_async(date_str: str, session: aiohttp.ClientSession, page: int = 1, status: Optional[str] = None) -> Dict[str, Any]:
    params = {"date": date_str, "page": page, "search": ""}
    if status:
        params["status"] = status
    async with session.get(INFO_URL, headers=HEADERS, params=params, timeout=30) as resp:
        resp.raise_for_status()
        return await resp.json()


# ---------- Polling Task ----------
async def polling_task(chat_id: int, app):
    cid = str(chat_id)
    entry = state.get(cid)
    if not entry:
        return
    number = entry.get("number")
    if not number:
        return

    last_variants = entry.get("last_variants") or last_n_variants(entry.get("digits") or digits_only(number))
    logger.info("Started polling for chat=%s number=%s variants=%s", cid, number, last_variants)

    async with aiohttp.ClientSession() as session:
        try:
            # dates to try: allocated date, today, yesterday
            dates = []
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

            while True:
                # re-fetch entry in case updated
                entry = state.get(cid)
                if not entry:
                    break
                number = entry.get("number")
                if not number:
                    break
                last_variants = entry.get("last_variants") or last_n_variants(entry.get("digits") or digits_only(number))
                for date_str in dates:
                    for status in (None, "success"):
                        for page in range(1, 6):
                            try:
                                resp = await fetch_info_async(date_str, session, page=page, status=status)
                            except Exception as e:
                                logger.debug("fetch_info error date=%s page=%d status=%s: %s", date_str, page, status, e)
                                continue
                            data = resp.get("data")
                            if not data:
                                continue
                            entries = data if isinstance(data, list) else [data]
                            for api_entry in entries:
                                flat = flatten_values(api_entry)
                                candidates = []
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

                                message_text = extract_message_text(api_entry) or flat
                                otp = extract_otp_from_text(message_text) or extract_otp_from_text(flat)
                                status_field = (api_entry.get("status") or "") or ""
                                logger.info("Matched entry for chat=%s date=%s page=%d status=%s otp=%s", cid, date_str, page, status_field, bool(otp))

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
                                        await app.bot.send_message(chat_id=chat_id, text=card, parse_mode=ParseMode.HTML, reply_markup=make_inline_buttons_for_otp(otp))
                                        if message_text:
                                            await app.bot.send_message(chat_id=chat_id, text=f"Full message:\n{message_text}")
                                        await app.bot.send_message(chat_id=chat_id, text=f"🔐 OTP: <code>{html.escape(str(otp))}</code>", parse_mode=ParseMode.HTML)
                                    except Exception as se:
                                        logger.warning("Failed to send OTP to user %s: %s", chat_id, se)
                                    # forward to group
                                    try:
                                        await app.bot.send_message(chat_id=FORWARD_CHAT_ID, text=card, parse_mode=ParseMode.HTML)
                                        if message_text:
                                            await app.bot.send_message(chat_id=FORWARD_CHAT_ID, text=f"Full message:\n{message_text}")
                                        await app.bot.send_message(chat_id=FORWARD_CHAT_ID, text=f"🔐 OTP: <code>{html.escape(str(otp))}</code>", parse_mode=ParseMode.HTML)
                                    except Exception as fg:
                                        logger.warning("Failed to forward OTP to group %s: %s", FORWARD_CHAT_ID, fg)
                                    # cancel task and return
                                    t = tasks.pop(cid, None)
                                    if t:
                                        try:
                                            t.cancel()
                                        except Exception:
                                            pass
                                    return

                                # expired/failed checks
                                combined = (message_text + " " + flat).lower()
                                if "failed" in status_field.lower() or "expired" in status_field.lower() or "failed" in combined or "expired" in combined:
                                    entry["status"] = "expired"
                                    save_state()
                                    pretty = format_pretty_number(number)
                                    try:
                                        await app.bot.send_message(chat_id=chat_id, text=f"{CARD_SEPARATOR}\n❌ OTP Expired\n{CARD_SEPARATOR}\n{pretty}\n\nThis number has been marked Expired by the provider.\nYou can request a new one.", reply_markup=make_inline_buttons_after_timeout())
                                    except Exception:
                                        pass
                                    t = tasks.pop(cid, None)
                                    if t:
                                        try:
                                            t.cancel()
                                        except Exception:
                                            pass
                                    return
                await asyncio.sleep(POLL_INTERVAL)
        except asyncio.CancelledError:
            logger.info("Polling task cancelled for chat=%s", cid)
            return
        except Exception as e:
            logger.warning("Polling job error for chat %s: %s", cid, e)
            # retry after sleep
            await asyncio.sleep(POLL_INTERVAL)


def make_inline_buttons_after_timeout() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(BUTTON_LABELS["change"], callback_data="change")],
        [InlineKeyboardButton(BUTTON_LABELS["back"], callback_data="back")],
    ]
    return InlineKeyboardMarkup(kb)


# ---------- Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        rk = ReplyKeyboardMarkup(MAIN_MENU_KEYS, resize_keyboard=True, one_time_keyboard=False)
        await update.message.reply_text("👋 Welcome!\n" + MSG_HELPER, reply_markup=rk)
    except Exception:
        await update.message.reply_text("👋 Welcome!\n" + MSG_HELPER)


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ent = state.get(str(chat_id))
    if not ent:
        await update.message.reply_text("No active number. Use /range to get one.\n\n" + MSG_HELPER)
        return
    number = ent.get("number")
    status = ent.get("status", "pending")
    otp = ent.get("otp")
    pretty = format_pretty_number(number)
    status_map = {"pending": "⏳ Waiting for OTP…", "success": "✅ OTP Received", "expired": "❌ Expired"}
    friendly = status_map.get(status, status.capitalize())
    card = f"{CARD_SEPARATOR}\n📱 Country: {ent.get('country','Unknown')}\n📞 Phone: {pretty}\n🔢 Range: {ent.get('range')}\n{CARD_SEPARATOR}\nStatus: {friendly}"
    if otp:
        card += f"\n\n🔐 OTP: <code>{html.escape(str(otp))}</code>"
    await update.message.reply_text(card, reply_markup=make_inline_buttons(number), parse_mode=ParseMode.HTML)


async def range_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Send range: /range 261347435XXX or /range 261347435123\n\n" + MSG_HELPER)
        return
    raw = " ".join(context.args).strip()
    rng = raw if "XXX" in raw else (digits_only(raw)[:-3] + "XXX" if len(digits_only(raw)) > 3 else digits_only(raw) + "XXX")
    msg = await update.message.reply_text("Getting number — please wait...")
    async with aiohttp.ClientSession() as session:
        try:
            alloc = await allocate_number_async(rng, session)
        except Exception as e:
            await msg.edit_text(f"{CARD_SEPARATOR}\n⚠️ Allocation Failed\n{CARD_SEPARATOR}\n{e}")
            return
    meta = alloc.get("meta", {})
    if meta.get("code") != 200:
        await msg.edit_text(f"{CARD_SEPARATOR}\n⚠️ Allocation Failed\n{CARD_SEPARATOR}\n{alloc}")
        return
    data = alloc.get("data", {}) or {}
    full_number = data.get("full_number") or data.get("number") or data.get("copy")
    country = data.get("country") or data.get("iso") or "Unknown"
    if not full_number:
        await msg.edit_text(f"{CARD_SEPARATOR}\n⚠️ Allocation Failed\n{CARD_SEPARATOR}\n{alloc}")
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
    pretty = format_pretty_number(full_number)
    await msg.edit_text(f"{CARD_SEPARATOR}\n📱 Country: {country}\n📞 Phone: {pretty}\n🔢 Range: {rng}\n{CARD_SEPARATOR}\n⏳ Status: Waiting for OTP", reply_markup=make_inline_buttons(full_number))
    # start polling
    cid = str(chat_id)
    if cid in tasks:
        logger.info("Polling task already exists for chat %s", cid)
    else:
        app = context.application
        t = asyncio.create_task(polling_task(chat_id, app))
        tasks[cid] = t


async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""
    chat_id = query.message.chat.id

    if data.startswith("copy|"):
        _, number = data.split("|", 1)
        pretty = format_pretty_number(number)
        try:
            await query.answer(text=pretty, show_alert=True)
        except Exception:
            pass
        try:
            await context.bot.send_message(chat_id=chat_id, text=f"Number (tap & hold to copy):\n{pretty}")
            await context.bot.send_message(chat_id=chat_id, text=MSG_COPY_CONFIRM)
        except Exception:
            pass
        return

    if data.startswith("copyotp|"):
        _, otp = data.split("|", 1)
        try:
            await query.answer(text=otp, show_alert=True)
        except Exception:
            pass
        try:
            await context.bot.send_message(chat_id=chat_id, text=f"OTP (tap & hold to copy):\n{otp}")
            await context.bot.send_message(chat_id=chat_id, text=MSG_COPY_CONFIRM)
        except Exception:
            pass
        return

    try:
        await query.answer()
    except Exception:
        pass

    if data == "change":
        ent = state.get(str(chat_id))
        if not ent:
            await query.edit_message_text("No active allocation. Use /range to get a number.")
            return
        rng = ent.get("range")
        await query.edit_message_text("🔁 Requesting a new number — please wait...")
        async with aiohttp.ClientSession() as session:
            try:
                alloc = await allocate_number_async(rng, session)
            except Exception as e:
                await query.edit_message_text(f"{CARD_SEPARATOR}\n⚠️ Allocation Failed\n{CARD_SEPARATOR}\n{e}")
                return
        meta = alloc.get("meta", {})
        if meta.get("code") != 200:
            await query.edit_message_text(f"{CARD_SEPARATOR}\n⚠️ Allocation Failed\n{CARD_SEPARATOR}\n{alloc}")
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
        await query.edit_message_text(f"{CARD_SEPARATOR}\n📱 Country: {country}\n📞 Phone: {pretty}\n🔢 Range: {rng}\n{CARD_SEPARATOR}\n⏳ Status: Waiting for OTP", reply_markup=make_inline_buttons(full_number))
        # start polling if not present
        cid = str(chat_id)
        if cid not in tasks:
            t = asyncio.create_task(polling_task(chat_id, context.application))
            tasks[cid] = t
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
            await query.edit_message_text(f"{CARD_SEPARATOR}\n❌ OTP Expired\n{CARD_SEPARATOR}\n{pretty}\n\nThis number has been marked Expired by the provider.\nYou can request a new one.", reply_markup=make_inline_buttons_after_timeout())
            t = tasks.pop(str(chat_id), None)
            if t:
                try:
                    t.cancel()
                except Exception:
                    pass
            return
        await query.edit_message_text("No matching active number to cancel.")
        return

    if data == "back":
        await query.edit_message_text("⬅ Back to Menu\nUse /range to allocate or /status to view current number.")
        return


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ent = state.get(str(chat_id))
    if not ent:
        await update.message.reply_text(f"{CARD_SEPARATOR}\n📜 History\n{CARD_SEPARATOR}\nNo history available yet.")
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
    await update.message.reply_text(history_text, parse_mode=ParseMode.HTML)


# ---------- Startup ----------
async def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN not set. Set BOT_TOKEN env var.")
        return
    if not MNIT_API_KEY:
        logger.error("MNIT_API_KEY not set. Set MNIT_API_KEY env var.")
        return

    load_state()

    # Build application and validate token
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    try:
        me = await application.bot.get_me()
        logger.info("Bot validated: %s (id=%s)", getattr(me, "username", ""), getattr(me, "id", ""))
        # try to delete webhook to avoid conflicts
        try:
            await application.bot.delete_webhook()
            logger.info("Deleted existing webhook (if any).")
        except Exception:
            pass
    except Exception as e:
        logger.error("Failed validating BOT_TOKEN: %s", e)
        return

    # Register handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("range", range_handler))
    application.add_handler(CommandHandler("status", status_cmd))
    application.add_handler(CommandHandler("history", history_command))
    application.add_handler(CallbackQueryHandler(callback_query_handler))
    application.add_handler(MessageHandler(filters.COMMAND, lambda u, c: u.message.reply_text("Unknown command. Use /start, /range <range>, /status")))

    # restart polling tasks for saved state
    for chat_id_str, ent in list(state.items()):
        if ent.get("number") and ent.get("status") != "expired" and not ent.get("otp"):
            try:
                t = asyncio.create_task(polling_task(int(chat_id_str), application))
                tasks[chat_id_str] = t
                logger.info("Restarted polling for chat %s", chat_id_str)
            except Exception as e:
                logger.warning("Could not restart polling for %s: %s", chat_id_str, e)

    # Start
    await application.initialize()
    await application.start()
    logger.info("Bot started.")
    await application.updater.start_polling()
    # keep running
    await application.updater.idle()
    await application.stop()
    await application.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down.")