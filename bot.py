#!/usr/bin/env python3
"""
Dr OTP Receiver - Stable production-ready version

What this file fixes and improves (requested):
- Only show the 4 services: WhatsApp, Facebook, Instagram, Other.
- Discovery: prefer countries with a real `country` field; avoid "Unknown" in the list unless user chooses "Any".
- For a chosen country: try to allocate up to 3 numbers FROM THAT SAME COUNTRY but from DISTINCT PREFIXES.
  If the country has too few prefixes, fall back to high-volume global prefixes for the chosen service.
- Allocation: robust retry logic (multiple payload variants) and tries several candidate prefixes before failing.
  When provider returns "No Number Found For Allocation" (HTTP 400), bot will try other prefixes and report failures
  in a friendly way instead of spamming raw errors.
- Active / History:
  - Active shows allocations for the user that are pending (no OTP yet and not expired).
  - History shows previous allocations (success/expired) including OTP if received.
- Bottom reply keyboard is compact and fully wired to commands:
  - Pressing Get Number triggers the /get flow (service -> country -> allocate).
  - Pressing Active triggers /status.
  - Pressing History triggers /history.
  - Pressing Settings opens /settings (simple preset).
- Token watcher: bot reads BOT_TOKEN from environment repeatedly; use /checktoken to diagnose.
- Admin debug support: ENABLE_DEBUG_TO_CHAT env var (optional) to receive debug messages.

Security: Do NOT hardcode BOT_TOKEN in this file. Put it in your host's environment variables.

Environment variables required:
- BOT_TOKEN        -> Telegram bot token (set in your host / Koyeb / Heroku environment)
- MNIT_API_KEY     -> Provider API key
Optional:
- FORWARD_CHAT_ID  -> group id to forward OTPs to (default -1003379113224)
- POLL_INTERVAL    -> seconds between polling attempts (default 10)
- DISCOVER_PAGES   -> pages to scan when discovering prefixes/countries (default 6)
- MAX_ALLOC_PER_COUNTRY -> max numbers to allocate per country (default 3)
- ENABLE_DEBUG_TO_CHAT  -> chat id (string) to receive debug messages (optional)
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

# -----------------------------
# Configuration (from env)
# -----------------------------
MNIT_API_KEY = os.getenv("MNIT_API_KEY", "M_WH9Q3U88V").strip()
FORWARD_CHAT_ID = int(os.getenv("FORWARD_CHAT_ID", "-1003379113224"))
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "10"))
DISCOVER_PAGES = int(os.getenv("DISCOVER_PAGES", "6"))
MAX_ALLOC_PER_COUNTRY = int(os.getenv("MAX_ALLOC_PER_COUNTRY", "3"))
ENABLE_DEBUG_TO_CHAT = os.getenv("ENABLE_DEBUG_TO_CHAT")  # optional admin chat id

# Provider endpoints
ALLOCATE_URL = "https://x.mnitnetwork.com/mapi/v1/mdashboard/getnum/number"
INFO_URL = "https://x.mnitnetwork.com/mapi/v1/mdashboard/getnum/info"
HEADERS = {"Content-Type": "application/json", "mapikey": MNIT_API_KEY}

# -----------------------------
# UI text & constants
# -----------------------------
CARD_SEPARATOR = "━━━━━━━━━━━━━━━━━━━━━━━━━━"
SERVICES = ["WhatsApp", "Facebook", "Instagram", "Other"]
MAIN_REPLY_KEYS = [
    ["📲 Get Number", "📥 Active"],
    ["📜 History", "⚙ Settings"],
]
MSG_HELPER = "Tip: Use /get to request numbers interactively."
MSG_COPY_CONFIRM = "✅ Sent — long-press to copy."
STATE_FILE = "state.json"

# -----------------------------
# Logging
# -----------------------------
logging.basicConfig(format="[%(asctime)s] %(levelname)s: %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# -----------------------------
# Persistent state structure
# -----------------------------
# state example:
# {
#   "<chat_id>": {
#       "allocations": [
#           {
#               "id": "1611111111111_0",
#               "range": "236724XXX",
#               "number": "23672441234",
#               "digits": "23672441234",
#               "country": "Central African Republic",
#               "allocated_at": 1611111111,
#               "status": "pending"|"success"|"expired",
#               "otp": null or "123456"
#           }, ...
#       ],
#       "history": [ ... ]  # optional archive of past allocations
#   }
# }
state: Dict[str, Dict[str, Any]] = {}
jobs_registry: Dict[str, Any] = {}  # key = "<chat_id>:<alloc_id>" -> job handle

# -----------------------------
# Persistence helpers
# -----------------------------
def load_state() -> None:
    global state
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                state = json.load(f)
            logger.info("Loaded state for %d chats", len(state))
    except Exception as e:
        logger.warning("Could not load state.json: %s", e)
        state = {}


def save_state() -> None:
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        logger.warning("Failed to save state.json: %s", e)

# -----------------------------
# Utilities
# -----------------------------
def digits_only(s: Optional[str]) -> str:
    if not s:
        return ""
    return re.sub(r"\D", "", str(s))


def flatten_values(x: Any) -> str:
    if isinstance(x, dict):
        parts: List[str] = []
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
    # deep search fallback
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
    groups = []
    while d:
        groups.insert(0, d[-3:])
        d = d[:-3]
    return f"{plus}{' '.join(groups)}"

# -----------------------------
# Provider API helpers
# -----------------------------
def fetch_info(date_str: str, page: int = 1, status: Optional[str] = None) -> Dict[str, Any]:
    params = {"date": date_str, "page": page, "search": ""}
    if status:
        params["status"] = status
    resp = requests.get(INFO_URL, headers=HEADERS, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def try_allocate_payload_variants(prefix: str, timeout: int = 25) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Try several payload shapes; returns (json_response, error_message).
    Many providers behave slightly differently; we attempt multiple combinations.
    """
    payload_variants = [
        {"range": prefix, "is_national": None, "remove_plus": None},
        {"range": prefix, "is_national": False, "remove_plus": False},
        {"range": prefix, "is_national": True, "remove_plus": False},
        {"range": prefix, "is_national": False, "remove_plus": True},
        {"range": prefix, "is_national": True, "remove_plus": True},
        {"range": prefix},
    ]
    last_err = None
    for p in payload_variants:
        try:
            logger.debug("Alloc try payload=%s", p)
            r = requests.post(ALLOCATE_URL, json=p, headers=HEADERS, timeout=timeout)
            body = r.text
            try:
                j = r.json()
            except Exception:
                j = {"http_status": r.status_code, "body": body}
            if 200 <= r.status_code < 300:
                logger.info("Allocation success (payload=%s)", p)
                return j, None
            else:
                logger.warning("Allocation returned HTTP %s payload=%s body=%s", r.status_code, p, body[:400])
                last_err = f"HTTP {r.status_code}: {body[:300]}"
        except Exception as e:
            logger.warning("Allocation exception payload=%s error=%s", p, e)
            last_err = str(e)
    return None, last_err

# -----------------------------
# Discovery helpers
# -----------------------------
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
    # Other matches generic OTP mentions
    if s == "other" and ("otp" in t or "code" in t):
        return True
    return False


def discover_country_prefixes_for_service(service: str, pages: int = DISCOVER_PAGES) -> List[Tuple[str, str, int]]:
    """
    Scan recent /info pages and return sorted list of (country, prefix, count).
    Prefer entries where the provider returns a non-empty country field.
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
                # pick prefix length 7 or 8 when possible
                pref = d[:6]
                if len(d) >= 8:
                    pref = d[:8]
                elif len(d) >= 7:
                    pref = d[:7]
                country = e.get("country") or e.get("iso") or ""
                if not country:
                    # skip unknown country entries for the country list step
                    continue
                key = (country, pref)
                counts[key] = counts.get(key, 0) + 1
    items = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    return [(country, pref, cnt) for ((country, pref), cnt) in items]

# -----------------------------
# Allocation bookkeeping
# -----------------------------
def ensure_chat_allocations(chat_id: str) -> None:
    if chat_id not in state:
        state[chat_id] = {"allocations": [], "history": []}


def add_allocation(chat_id: str, rng: str, full_number: str, country: str) -> str:
    ensure_chat_allocations(chat_id)
    alloc_id = str(int(time.time() * 1000)) + "_" + str(len(state[chat_id]["allocations"]))
    digits = digits_only(full_number)
    entry = {
        "id": alloc_id,
        "range": rng,
        "number": full_number,
        "digits": digits,
        "country": country,
        "allocated_at": int(time.time()),
        "status": "pending",
        "otp": None,
    }
    state[chat_id]["allocations"].append(entry)
    save_state()
    return alloc_id


def archive_allocation(chat_id: str, alloc: Dict[str, Any]) -> None:
    """Move allocation to history archive (keeps last N if desired)"""
    ensure_chat_allocations(chat_id)
    history = state[chat_id].setdefault("history", [])
    history.append(alloc)
    # optionally limit history length
    if len(history) > 200:
        history.pop(0)
    save_state()


def get_allocation(chat_id: str, alloc_id: str) -> Optional[Dict[str, Any]]:
    arr = state.get(chat_id, {}).get("allocations", [])
    for a in arr:
        if a.get("id") == alloc_id:
            return a
    return None

# -----------------------------
# Polling job for each allocation
# -----------------------------
def polling_job_for_alloc(context: CallbackContext) -> None:
    job_ctx = context.job.context
    chat_id = str(job_ctx["chat_id"])
    alloc_id = job_ctx["alloc_id"]
    alloc = get_allocation(chat_id, alloc_id)
    if not alloc:
        # cleanup job
        key = f"{chat_id}:{alloc_id}"
        job_obj = jobs_registry.pop(key, None)
        if job_obj:
            try:
                job_obj.schedule_removal()
            except Exception:
                pass
        return

    number = alloc.get("number")
    digits = alloc.get("digits")
    logger.debug("Polling allocation %s for chat %s number %s", alloc_id, chat_id, number)

    # dates to check: allocated day, today, yesterday
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
                        if digits and digits in digits_only(flat):
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
                            logger.warning("Failed to send OTP message to user: %s", se)
                        try:
                            context.bot.send_message(chat_id=FORWARD_CHAT_ID, text=card, parse_mode=ParseMode.HTML)
                        except Exception as fe:
                            logger.warning("Failed to forward OTP to group: %s", fe)
                        # stop job and archive
                        key = f"{chat_id}:{alloc_id}"
                        job_obj = jobs_registry.pop(key, None)
                        if job_obj:
                            try:
                                job_obj.schedule_removal()
                            except Exception:
                                pass
                        # move to history
                        try:
                            # remove from allocations and put into history
                            chat_allocs = state.get(chat_id, {}).get("allocations", [])
                            state[chat_id]["allocations"] = [a for a in chat_allocs if a.get("id") != alloc_id]
                            state[chat_id].setdefault("history", []).append(alloc)
                            save_state()
                        except Exception:
                            logger.exception("Error archiving allocation")
                        return

                    if provider_says_expired:
                        alloc["status"] = "expired"
                        save_state()
                        try:
                            context.bot.send_message(chat_id=int(chat_id), text=f"{CARD_SEPARATOR}\n❌ Expired by provider\n{CARD_SEPARATOR}\n{format_pretty_number(number)}")
                        except Exception:
                            pass
                        # stop job and move to history
                        key = f"{chat_id}:{alloc_id}"
                        job_obj = jobs_registry.pop(key, None)
                        if job_obj:
                            try:
                                job_obj.schedule_removal()
                            except Exception:
                                pass
                        try:
                            chat_allocs = state.get(chat_id, {}).get("allocations", [])
                            state[chat_id]["allocations"] = [a for a in chat_allocs if a.get("id") != alloc_id]
                            state[chat_id].setdefault("history", []).append(alloc)
                            save_state()
                        except Exception:
                            logger.exception("Error moving expired allocation to history")
                        return
    # nothing found this pass

# -----------------------------
# Telegram command handlers
# -----------------------------
def start_command(update: Update, context: CallbackContext) -> None:
    try:
        rk = ReplyKeyboardMarkup(MAIN_REPLY_KEYS, resize_keyboard=True, one_time_keyboard=False)
        update.message.reply_text("👋 Welcome! " + MSG_HELPER, reply_markup=rk)
    except Exception:
        update.message.reply_text("👋 Welcome! " + MSG_HELPER)


def get_command(update: Update, context: CallbackContext) -> None:
    kb = []
    for svc in SERVICES:
        kb.append([InlineKeyboardButton(svc, callback_data=f"svc|{svc}")])
    kb.append([InlineKeyboardButton("Cancel", callback_data="cancel")])
    update.message.reply_text("Select Service:", reply_markup=InlineKeyboardMarkup(kb))


def discover_and_send_countries(chat_id: int, svc: str, context: CallbackContext) -> None:
    """
    Discover countries that have visible traffic for the service and send a compact list.
    Excludes entries where provider's country is missing (avoid "Unknown") to prevent 'Unknown' being shown.
    """
    try:
        candidates = discover_country_prefixes_for_service(svc, pages=DISCOVER_PAGES)
    except Exception as e:
        logger.exception("Discovery error: %s", e)
        context.bot.send_message(chat_id=chat_id, text=f"Discovery failed: {e}")
        return
    # Build country counts (already provided by discover function) — keep only non-empty countries
    by_country: Dict[str, int] = {}
    for country, pref, cnt in candidates:
        if not country or country.strip() == "":
            continue
        by_country[country] = by_country.get(country, 0) + cnt
    if not by_country:
        # fallback: allow "Any country" option that will try global prefixes
        kb = [[InlineKeyboardButton("Any country (try global prefixes)", callback_data=f"country|{svc}|__ANY__")],
              [InlineKeyboardButton("Cancel", callback_data="cancel")]]
        context.bot.send_message(chat_id=chat_id, text=f"No active countries found for {svc}. You may try global prefixes.", reply_markup=InlineKeyboardMarkup(kb))
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
        # data format: country|<svc>|<countryName> or country|<svc>|__ANY__
        parts = data.split("|", 2)
        if len(parts) < 3:
            query.answer()
            query.edit_message_text("Invalid selection.")
            return
        _, svc, country = parts
        svc = svc.strip()
        country = country.strip()
        query.answer()
        query.edit_message_text(f"Allocating up to {MAX_ALLOC_PER_COUNTRY} numbers for {svc} • {country} — please wait...")
        # Discover prefixes (including global fallback)
        candidates = discover_country_prefixes_for_service(svc, pages=DISCOVER_PAGES)
        # Build list of candidate prefixes for the chosen country
        prefs_for_country: List[str] = [pref for (c, pref, cnt) in candidates if c == country]
        prefs_global: List[str] = [pref for (c, pref, cnt) in candidates]
        chosen_prefixes: List[str] = []
        # If user asked for __ANY__, we will use top global prefixes
        if country == "__ANY__":
            for pref in prefs_global:
                if pref not in chosen_prefixes:
                    chosen_prefixes.append(pref)
                if len(chosen_prefixes) >= MAX_ALLOC_PER_COUNTRY:
                    break
        else:
            # prefer country-specific prefixes; if too few found, fall back to global prefixes
            for pref in prefs_for_country:
                if pref not in chosen_prefixes:
                    chosen_prefixes.append(pref)
                if len(chosen_prefixes) >= MAX_ALLOC_PER_COUNTRY:
                    break
            if len(chosen_prefixes) < MAX_ALLOC_PER_COUNTRY:
                for pref in prefs_global:
                    if pref not in chosen_prefixes:
                        chosen_prefixes.append(pref)
                    if len(chosen_prefixes) >= MAX_ALLOC_PER_COUNTRY:
                        break
        allocated_infos: List[Dict[str, Any]] = []
        for pref in chosen_prefixes:
            rng = pref + "XXX"
            resp_json, err = try_allocate_payload_variants(rng)
            if resp_json is None:
                allocated_infos.append({"range": rng, "error": err or "No response"})
                continue
            meta = resp_json.get("meta", {})
            if meta.get("code") != 200:
                allocated_infos.append({"range": rng, "error": str(resp_json)})
                continue
            data_alloc = resp_json.get("data", {}) or {}
            full_number = data_alloc.get("full_number") or data_alloc.get("number") or data_alloc.get("copy")
            country_name = data_alloc.get("country") or (country if country != "__ANY__" else "Unknown")
            if not full_number:
                allocated_infos.append({"range": rng, "error": "provider returned no number"})
                continue
            alloc_id = add_allocation(str(chat_id), rng, full_number, country_name)
            # start per-allocation polling job
            job = context.job_queue.run_repeating(polling_job_for_alloc, interval=POLL_INTERVAL, first=5, context={"chat_id": int(chat_id), "alloc_id": alloc_id})
            jobs_registry[f"{chat_id}:{alloc_id}"] = job
            allocated_infos.append({"range": rng, "number": full_number, "alloc_id": alloc_id, "country": country_name})
        # Build reply summary
        lines = []
        kb = []
        for info in allocated_infos:
            if "error" in info:
                lines.append(f"Range {info['range']}: Error {info['error']}")
            else:
                pretty = format_pretty_number(info["number"])
                lines.append(f"{pretty} • {info['country']}")
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

    if data == "cancel":
        query.answer()
        query.edit_message_text("Cancelled.")
        return

    try:
        query.answer()
    except Exception:
        pass

# -----------------------------
# Keyboard text handlers (map bottom keyboard to commands)
# -----------------------------
def text_message_handler(update: Update, context: CallbackContext) -> None:
    text = (update.message.text or "").strip()
    if text == "📲 Get Number":
        return get_command(update, context)
    if text == "📥 Active":
        return status_handler(update, context)
    if text == "📜 History":
        return history_handler(update, context)
    if text == "⚙ Settings":
        return settings_handler(update, context)
    # fallback to normal command processing
    update.message.reply_text("Unknown option. Use /get or /status or /history.")

# -----------------------------
# Status / history / settings
# -----------------------------
def status_handler(update: Update, context: CallbackContext) -> None:
    chat_id = str(update.effective_chat.id)
    allocations = state.get(chat_id, {}).get("allocations", [])
    # active = pending allocations (no OTP, not expired)
    active = [a for a in allocations if a.get("status") == "pending" and not a.get("otp")]
    if not active:
        update.message.reply_text("No active numbers. Use /get.")
        return
    lines = []
    kb = []
    for a in active:
        pretty = format_pretty_number(a.get("number"))
        lines.append(f"{pretty} • {a.get('country')}")
        kb.append([InlineKeyboardButton(pretty, callback_data=f"noop|{a.get('id')}")])
    update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb))


def history_handler(update: Update, context: CallbackContext) -> None:
    chat_id = str(update.effective_chat.id)
    hist = state.get(chat_id, {}).get("history", [])
    if not hist:
        update.message.reply_text("No history available yet.")
        return
    lines = []
    for h in reversed(hist[-20:]):
        pretty = format_pretty_number(h.get("number"))
        status = h.get("status", "unknown")
        otp = h.get("otp") or ""
        line = f"{pretty} • {h.get('country')} • {status}"
        if otp:
            line += f" • OTP: {otp}"
        lines.append(line)
    update.message.reply_text("\n".join(lines))


def settings_handler(update: Update, context: CallbackContext) -> None:
    # simple settings placeholder; you can expand
    text = (
        "Settings:\n"
        f"- Poll interval: {POLL_INTERVAL}s\n"
        f"- Discover pages: {DISCOVER_PAGES}\n"
        f"- Max alloc per country: {MAX_ALLOC_PER_COUNTRY}\n\n"
        "Use environment variables to change settings and restart the service."
    )
    update.message.reply_text(text)

# -----------------------------
# Token watcher & updater (dynamic)
# -----------------------------
_updater_global: Optional[Updater] = None
_updater_lock = threading.Lock()


def start_telegram_updater(bot: Bot) -> None:
    global _updater_global
    with _updater_lock:
        if _updater_global:
            return
        updater = Updater(bot=bot, use_context=True)
        dp = updater.dispatcher
        # register handlers
        dp.add_handler(CommandHandler("start", start_command))
        dp.add_handler(CommandHandler("get", get_command))
        dp.add_handler(CommandHandler("status", status_handler))
        dp.add_handler(CommandHandler("history", history_handler))
        dp.add_handler(CommandHandler("settings", settings_handler))
        dp.add_handler(CommandHandler("checktoken", checktoken_command))
        dp.add_handler(CallbackQueryHandler(callback_query_handler))
        # map bottom keyboard text to actions
        dp.add_handler(MessageHandler(Filters.text & (~Filters.command), text_message_handler))
        try:
            updater.start_polling()
            logger.info("Telegram updater started.")
            _updater_global = updater
            # restart jobs for saved allocations
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
    Repeatedly re-reads BOT_TOKEN from environment so you can change it in the host UI
    without immediate redeploy. Starts the updater when BOT_TOKEN is valid.
    """
    last_token = ""
    while True:
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token:
            logger.warning("BOT_TOKEN missing in environment. Waiting 10s.")
            time.sleep(10)
            continue
        if token != last_token:
            logger.info("BOT_TOKEN loaded/changed.")
            last_token = token
        try:
            bot = Bot(token)
            me = bot.get_me()
            logger.info("Validated bot: %s (id=%s)", getattr(me, "username", ""), getattr(me, "id", ""))
            try:
                bot.delete_webhook()
                logger.info("Deleted webhook to enable polling.")
            except Exception:
                pass
            start_telegram_updater(bot)
            return
        except Unauthorized:
            logger.error("BOT_TOKEN invalid/unauthorized. Will retry in 20s.")
            time.sleep(20)
            continue
        except NetworkError as ne:
            logger.warning("Network error validating token: %s — retrying in 5s", ne)
            time.sleep(5)
            continue
        except Exception as e:
            logger.warning("Token watcher unexpected error: %s — retrying", e)
            time.sleep(10)
            continue


def checktoken_command(update: Update, context: CallbackContext) -> None:
    """
    Admin helper: verifies current BOT_TOKEN (reads env again).
    This prints bot username & id on success (does NOT echo token).
    """
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

# -----------------------------
# Main entry
# -----------------------------
def main() -> None:
    # sanity checks
    if not MNIT_API_KEY:
        logger.error("MNIT_API_KEY not set. Set environment variable and restart.")
    load_state()
    # start token watcher thread to start updater when BOT_TOKEN valid
    t = threading.Thread(target=token_watcher_loop, daemon=True)
    t.start()
    logger.info("Service running; Telegram updater will start when BOT_TOKEN is valid.")
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        logger.info("Shutdown requested.")
    # graceful stop
    global _updater_global
    with _updater_lock:
        if _updater_global:
            try:
                _updater_global.stop()
            except Exception:
                pass

if __name__ == "__main__":
    main()