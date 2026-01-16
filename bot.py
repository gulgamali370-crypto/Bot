#!/usr/bin/env python3
"""
Telegram bot that:
- Allocates a number from MNIT API using a range (POST /mapi/v1/mdashboard/getnum/number)
- Polls the SMS/info endpoint (GET /mapi/v1/mdashboard/getnum/info) to find OTPs
- Sends formatted messages to user like the screenshot (✅ OTP SUCCESS, Country, Number, Range, Message:)
- Forwards the extracted OTP (and raw SMS text) to the user when found

Environment:
- BOT_TOKEN (recommended) or default token embedded below (you provided it)
- MNIT_API_KEY (recommended) or default provided below
"""

import os
import time
import re
import logging
from datetime import datetime, timezone
from typing import Optional, Tuple, Any, Dict

import requests
from telegram import Update, ParseMode
from telegram.ext import Updater, CommandHandler, CallbackContext, MessageHandler, Filters

# ----- CONFIG -----
BOT_TOKEN = os.getenv("BOT_TOKEN") or "7108794200:AAGWA3aGPDjdYkXJ1VlOSdxBMHtuFpWzAIU"
MNIT_API_KEY = os.getenv("MNIT_API_KEY") or "M_WH9Q3U88V"

ALLOCATE_URL = "https://x.mnitnetwork.com/mapi/v1/mdashboard/getnum/number"
INFO_URL = "https://x.mnitnetwork.com/mapi/v1/mdashboard/getnum/info"

HEADERS = {
    "Content-Type": "application/json",
    "mapikey": MNIT_API_KEY
}

# Polling settings for OTP lookup
OTP_POLL_INTERVAL = 5         # seconds between info requests
OTP_POLL_TIMEOUT = 180        # total wait time in seconds

# Logging
logging.basicConfig(
    format='[%(asctime)s] %(levelname)s: %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)


# ----- UTILITIES -----
def normalize_range(raw: str) -> str:
    """
    Normalize user-supplied range into API format.
    If user supplies digits, replace last 3 digits with 'XXX'.
    If user supplies already 'XXX' — keep.
    Examples:
      "261347435XXX" -> "261347435XXX"
      "261347435123" -> "261347435XXX"
      "88017528" -> "88017XXX"  (logic: keep prefix, mask last 3)
    """
    r = raw.strip()
    if "XXX" in r:
        return r
    digits = re.sub(r"\D", "", r)
    if len(digits) <= 3:
        return digits + "XXX"
    # keep everything except final 3 digits, add XXX
    return digits[:-3] + "XXX"


def extract_otp_candidates(text: str) -> Optional[str]:
    """
    Try multiple patterns to extract an OTP from SMS text:
    1) Plain 4-8 digit sequences (preferred).
    2) Patterns like 'FB-46541' where code may include prefix+digits.
    3) Patterns like '<#> 77959' or '#> 77959'
    Return the best matched token (digits or prefix+digits).
    """
    if not text:
        return None
    # Normalize whitespace and remove weird separators
    txt = re.sub(r"[|:]+", " ", text)
    txt = txt.strip()

    # 1) Look for plain 4-8 digit number (most common OTP)
    m = re.search(r"\b(\d{4,8})\b", txt)
    if m:
        return m.group(1)

    # 2) Look for patterns like FB-46541 or AB-12345
    m2 = re.search(r"\b([A-Z]{1,4}[-_]\d{3,8})\b", txt, flags=re.IGNORECASE)
    if m2:
        return m2.group(1)

    # 3) Look for patterns like '<#> 77959' or '#> 77959'
    m3 = re.search(r"[<#>]{1,3}\s*([0-9]{4,8})\b", txt)
    if m3:
        return m3.group(1)

    # 4) Last resort: any 3-8 digit near the end
    m4 = re.search(r"(\d{3,8})\D*$", txt)
    if m4:
        return m4.group(1)

    return None


def allocate_number(range_str: str, timeout: int = 30) -> Dict[str, Any]:
    """
    Call MNIT allocate API and return parsed JSON.
    Raises requests.HTTPError on non-2xx.
    """
    payload = {
        "range": range_str,
        "is_national": None,
        "remove_plus": None
    }
    logger.info("Requesting allocation for range=%s", range_str)
    resp = requests.post(ALLOCATE_URL, json=payload, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def search_sms_for_number(full_number: str, max_wait: int = OTP_POLL_TIMEOUT,
                          interval: int = OTP_POLL_INTERVAL) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """
    Poll the info endpoint repeatedly until we find an SMS containing the full_number and an OTP pattern.
    Returns (otp, raw_entry) on success, (None, None) on timeout.
    """
    end_time = time.time() + max_wait
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    page = 1

    logger.info("Start polling for OTP for %s (timeout=%ds)", full_number, max_wait)
    while time.time() < end_time:
        params = {
            "date": date_str,
            "page": page,
            "search": "",
            "status": "success"
        }
        try:
            resp = requests.get(INFO_URL, headers=HEADERS, params=params, timeout=30)
            resp.raise_for_status()
            j = resp.json()
        except Exception as e:
            logger.warning("Error fetching info endpoint: %s (will retry)", e)
            time.sleep(interval)
            continue

        # The API sometimes returns a dict or list in data; handle both
        data = j.get("data")
        # If data is a dict with single item, or list of entries
        entries = []
        if isinstance(data, list):
            entries = data
        elif isinstance(data, dict):
            # Some APIs return a dict containing items or a single message
            entries = [data]

        # Search entries for our number and OTP
        for entry in entries:
            # Convert any nested content to string
            entry_text = " ".join([str(v) for v in flatten_dict_values(entry)])
            if full_number in entry_text or full_number.replace('+', '') in entry_text or full_number.replace('+', '')[-8:] in entry_text:
                otp = extract_otp_candidates(entry_text)
                if otp:
                    logger.info("Found OTP for %s: %s", full_number, otp)
                    return otp, entry
        # No OTP found on this page — advance or wait
        # If API supports paging increase page to search older/newer entries
        page += 1
        time.sleep(interval)
    logger.info("Timeout reached while polling for OTP for %s", full_number)
    return None, None


def flatten_dict_values(d: Any) -> list:
    """Recursively collect string representations of values from dict/list for searching."""
    out = []
    if isinstance(d, dict):
        for v in d.values():
            out.extend(flatten_dict_values(v))
    elif isinstance(d, list):
        for item in d:
            out.extend(flatten_dict_values(item))
    else:
        out.append(str(d))
    return out


def format_success_message(country: str, number: str, range_str: str, message_text: str) -> str:
    """
    Format result similar to the screenshot:
    ✅ OTP SUCCESS

    Country: Madagascar
    Number: 26XXX5175
    Range: 261347435XXX

    Message:
    <sms text...>
    """
    # Use simple plain text with emoji and newlines; Telegram will display it well.
    return (
        "✅ OTP SUCCESS\n\n"
        f"Country: {country}\n"
        f"Number: {number}\n"
        f"Range: {range_str}\n\n"
        "Message:\n"
        f"{message_text}"
    )


# ----- TELEGRAM HANDLERS -----
def start(update: Update, context: CallbackContext):
    update.message.reply_text(
        "Welcome — main commands:\n"
        "/range <range>  — allocate a number, e.g. /range 261347435XXX or /range 261347435123\n\n"
        "Bot will allocate a number and forward OTP when received."
    )


def range_handler(update: Update, context: CallbackContext):
    user = update.effective_user
    chat_id = update.effective_chat.id
    logger.info("User %s started allocation with args=%s", user.username or user.id, context.args)

    if not context.args:
        update.message.reply_text("Send the range like: /range 261347435XXX  (or numeric prefix; last 3 digits will be replaced with XXX)")
        return

    raw_range = " ".join(context.args)
    range_str = normalize_range(raw_range)

    # Inform user
    waiting_msg = update.message.reply_text("Getting number — please wait...")

    try:
        alloc_resp = allocate_number(range_str)
    except requests.HTTPError as e:
        logger.exception("Allocation HTTP error")
        waiting_msg.edit_text(f"Allocation failed (HTTP): {e}")
        return
    except Exception as e:
        logger.exception("Allocation error")
        waiting_msg.edit_text(f"Allocation failed: {e}")
        return

    # Validate API response structure
    meta = alloc_resp.get("meta", {})
    code = meta.get("code", 0)
    if code != 200:
        waiting_msg.edit_text(f"Allocation error: {alloc_resp}")
        return

    data = alloc_resp.get("data", {})
    # fields seen in docs: copy, full_number, number
    full_number = data.get("full_number") or data.get("number") or data.get("copy")
    country = data.get("country") or data.get("iso") or "Unknown"
    # Some responses include leading plus; normalize for searching
    if full_number and not full_number.startswith("+"):
        # If the API returned without +, try to add + if copy contains it
        full_number = full_number

    # Edit message to show allocated number
    waiting_msg.edit_text(f"Number allocated: {full_number}\nNow searching for OTP... (up to {OTP_POLL_TIMEOUT} seconds)")

    # Poll for OTP
    otp, raw_entry = search_sms_for_number(full_number, max_wait=OTP_POLL_TIMEOUT, interval=OTP_POLL_INTERVAL)

    if otp:
        raw_message_text = ""
        # Try to extract raw SMS text from entry fields intelligently
        if raw_entry:
            # prefer 'message' or 'sms' or 'msg' keys if present
            for k in ("message", "sms", "msg", "body", "text"):
                if isinstance(raw_entry, dict) and k in raw_entry:
                    raw_message_text = str(raw_entry[k])
                    break
            if not raw_message_text:
                # fallback to stringifying the entry
                raw_message_text = " ".join(flatten_dict_values(raw_entry))
        else:
            raw_message_text = f"OTP extracted: {otp}"

        # Format and send main success block (like screenshot)
        success_text = format_success_message(country=country, number=full_number, range_str=range_str, message_text=raw_message_text)
        # Send as plain text (safe)
        update.message.reply_text(success_text)

        # Also send a concise OTP message and the raw OTP to forward/use
        update.message.reply_text(f"Forwarding OTP for {full_number}:\n<code>{otp}</code>", parse_mode=ParseMode.HTML)
    else:
        update.message.reply_text(f"No OTP found for {full_number} within timeout ({OTP_POLL_TIMEOUT}s).")


def unknown(update: Update, context: CallbackContext):
    update.message.reply_text("Unknown command. Use /start or /range <range>")


def error_handler(update: Update, context: CallbackContext):
    logger.exception("Update caused error: %s", context.error)
    try:
        if update and update.effective_message:
            update.effective_message.reply_text("An internal error occurred.")
    except Exception:
        pass


# ----- MAIN -----
def main():
    logger.info("Starting bot (polling mode).")
    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("range", range_handler))
    dp.add_handler(MessageHandler(Filters.command, unknown))
    dp.add_error_handler(error_handler)

    # Start polling
    updater.start_polling()
    logger.info("Bot started. Press Ctrl-C to stop.")
    updater.idle()


if __name__ == "__main__":
    main()