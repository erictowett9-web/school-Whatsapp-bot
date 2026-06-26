# Gevent monkey patching must happen before any other imports.
# This makes all I/O operations (network, DB, sleep) non-blocking,
# so a flood of health check requests or slow DB queries don't block
# other requests — they yield to each other instead of queuing up.
from gevent import monkey; monkey.patch_all()

import os
import re
import time as _time_module
import logging
import hashlib
import hmac
import threading
import collections
import requests
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, session, render_template_string
from groq import Groq
from dotenv import load_dotenv
import database as db

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config.update(
    SESSION_COOKIE_SECURE=True,      # only sent over HTTPS (Koyeb terminates TLS for us)
    SESSION_COOKIE_HTTPONLY=True,    # not accessible to JS — mitigates XSS cookie theft
    SESSION_COOKIE_SAMESITE="Lax",   # CSRF mitigation while still allowing normal navigation
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
)

_SECRET_KEY_ENV = os.getenv("SECRET_KEY", "")
if _SECRET_KEY_ENV:
    app.secret_key = _SECRET_KEY_ENV
else:
    import secrets as _secrets
    app.secret_key = _secrets.token_hex(32)
    logging.getLogger(__name__).warning(
        "⚠️⚠️⚠️ SECRET_KEY env var is NOT set! Generated a random one-time secret "
        "for this process — all admin sessions will be invalidated on every "
        "restart/redeploy. Set SECRET_KEY in Koyeb's environment variables to "
        "fix this permanently."
    )

# ── Env vars ───────────────────────────────────────────────────────────────────
GROQ_API_KEY        = os.getenv("GROQ_API_KEY", "")
WHATSAPP_TOKEN      = os.getenv("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID     = os.getenv("PHONE_NUMBER_ID", "")
VERIFY_TOKEN        = os.getenv("VERIFY_TOKEN", "")
GEMINI_API_KEY      = os.getenv("GEMINI_API_KEY", "")
APP_SECRET          = os.getenv("APP_SECRET", "")
_ADMIN_PASSWORD_ENV = os.getenv("ADMIN_PASSWORD", "")
if _ADMIN_PASSWORD_ENV:
    ADMIN_PASSWORD = _ADMIN_PASSWORD_ENV
else:
    import secrets as _secrets
    ADMIN_PASSWORD = _secrets.token_urlsafe(12)
    logging.getLogger(__name__).warning(
        f"⚠️⚠️⚠️ ADMIN_PASSWORD env var is NOT set! Generated a random one-time "
        f"password for THIS DEPLOY ONLY: {ADMIN_PASSWORD} — check Koyeb logs now "
        f"to log in, then set ADMIN_PASSWORD in Koyeb's environment variables "
        f"immediately. This password will be different every restart until you do."
    )
ADMIN_WHATSAPP_NUMBER = os.getenv("ADMIN_WHATSAPP_NUMBER", "")  # e.g. whatsapp:+254723422407

groq_client = Groq(api_key=GROQ_API_KEY, timeout=15.0)

# ── Init DB ────────────────────────────────────────────────────────────────────
db.init_db()

# ── School info cache ──────────────────────────────────────────────────────────
# db.get_school_info() is called on every AI request (for the system prompt)
# AND for every menu lookup. Caching it with a short TTL means a single DB
# round-trip serves many concurrent messages. The cache is invalidated
# automatically after _SCHOOL_INFO_TTL seconds so dashboard edits propagate
# without a restart. Call invalidate_school_info_cache() in any route that
# writes to school_info so changes appear immediately.
_school_info_cache: dict = {"data": None, "ts": 0.0}
_SCHOOL_INFO_TTL = 300  # seconds (5 minutes)

def get_cached_school_info() -> dict:
    now = _time_module.time()
    if _school_info_cache["data"] is None or now - _school_info_cache["ts"] > _SCHOOL_INFO_TTL:
        _school_info_cache["data"] = db.get_school_info()
        _school_info_cache["ts"] = now
    return _school_info_cache["data"]

def invalidate_school_info_cache():
    _school_info_cache["data"] = None

# ── Startup diagnostics (always visible at INFO level) ────────────────────────
if ADMIN_WHATSAPP_NUMBER:
    logger.info(f"✅ ADMIN_WHATSAPP_NUMBER configured: {ADMIN_WHATSAPP_NUMBER}")
else:
    logger.warning("⚠️ ADMIN_WHATSAPP_NUMBER is NOT set — admin alerts and reply-by-phone will not work")

# ── School context ─────────────────────────────────────────────────────────────
def build_school_context(info=None):
    """Builds the AI system prompt from school_info.
    Accepts a pre-fetched info dict to avoid a redundant DB call when the
    caller already has it. Falls back to the cache (or a live DB read) when
    info is not supplied."""
    if info is None:
        info = get_cached_school_info()
    if not info:
        return "You are a helpful WhatsApp assistant for Sally-Ann School Limited in Litein, Kenya."

    context = f"""You are a friendly and helpful WhatsApp assistant for Sally-Ann School Limited in Litein, Kenya.
Answer questions from parents about the school using the information below.

SCHOOL FEES 2026 — DAY SCHOLARS:
- PP1: {info.get('fee_pp1', 'Contact school office')}
- PP2: {info.get('fee_pp2', 'Contact school office')}
- Grade 1: {info.get('fee_grade_1', 'Contact school office')}
- Grade 2: {info.get('fee_grade_2', 'Contact school office')}
- Grade 3: {info.get('fee_grade_3', 'Contact school office')}
- Grade 4: {info.get('fee_grade_4', 'Contact school office')}
- Grade 5: {info.get('fee_grade_5', 'Contact school office')}
- Grade 6–9: Boarding only (no day scholar option for these grades — see boarding fees below)
- New admission: Ksh {info.get('fee_admission', '2,000')}
- At least {info.get('fee_minimum_percent', '60')}% paid on Reporting Day. No cash accepted.
- ICT/Coding & Robotics: Ksh {info.get('fee_ict', '1,500')}/term (included in school fees)

SCHOOL FEES 2026 — BOARDING:
- Grade 6 boarding: {info.get('fee_grade_6_boarding', 'Contact school office')}
- Grade 7 boarding: {info.get('fee_grade_7_boarding', 'Contact school office')}
- Grade 8 boarding: {info.get('fee_grade_8_boarding', 'Contact school office')}
- Grade 9 boarding: {info.get('fee_grade_9_boarding', 'Contact school office')}

PAYMENT (FEES):
- M-Pesa Paybill: {info.get('pay_mpesa_paybill', '777643')}, Account: ADM number
- KCB: {info.get('pay_kcb', '1135294917')}
- Equity: {info.get('pay_equity', '0530291926992')}
- Equity Paybill: {info.get('pay_equity_paybill', '247247')}, Account: ADM number
- Coop Bank: {info.get('pay_coop', '01148786054900')}
- Chai Sacco: {info.get('pay_chai_sacco', '1083225')}

PAYMENT (TRIPS) — different paybill:
- M-Pesa Paybill: {info.get('trip_paybill', '328585')}, Account: {info.get('trip_account_format', '111444#ADM number')}

BUS ROUTES (per month):
- Kapkatet: {info.get('bus_kapkatet', '')}
- Litein: {info.get('bus_litein', '')}
- Tebesonik: {info.get('bus_tebesonik', '')}
- Chemosot: {info.get('bus_chemosot', '')}
- Mogogosiek: {info.get('bus_mogogosiek', '')}

TRIPS TERM II 2026:
- Grade 4: {info.get('trip_grade_4', '')}
- Grade 5: {info.get('trip_grade_5', '')}
- Grade 6: {info.get('trip_grade_6', '')}
- Grade 7: {info.get('trip_grade_7', '')}
- Grade 8: {info.get('trip_grade_8', '')}

PARENTAL ENGAGEMENT DAYS: {info.get('parental_days', '')}
HALF TERM: {info.get('term_half_term', '')}
SCHOOL CONTACT: Phone {info.get('school_phone', '0727839424')} | Email {info.get('school_email', 'sas@sallyannschool.sc.ke')}

RULES: Reply in same language as parent (English/Swahili). Max 3 sentences. Never make up info. If you don't know the answer or it's outside what's listed above, say exactly: "I don't have that information — the school office will get back to you shortly." (or Swahili: "Sina taarifa hiyo — ofisi ya shule itawasiliana nawe hivi karibuni.")
"""
    # Append any custom fields added from the dashboard
    custom_fields = {k: v for k, v in info.items()
                     if k.startswith('custom_') and not k.endswith('__label')}
    if custom_fields:
        context += "\nADDITIONAL SCHOOL INFORMATION:\n"
        for key, value in custom_fields.items():
            label_key = key + '__label'
            label = info.get(label_key, key.replace('custom_', '').replace('_', ' ').title())
            context += f"- {label}: {value}\n"

    return context

GREETING_MENU = """👋 Welcome to *Sally-Ann School* — Litein, Kenya!

Please choose an option by replying with the number:

1️⃣ School Fees & Payment
2️⃣ Bus Routes & Fares
3️⃣ Educational Trips
4️⃣ Admissions Enquiry
5️⃣ Parental Engagement Days
6️⃣ Half Term & School Calendar
7️⃣ Other / Ask a Question

_Reply with a number or type your question directly._"""

GREETING_MENU_SW = """👋 Karibu *Sally-Ann School* — Litein, Kenya!

Tafadhali chagua kwa kujibu nambari:

1️⃣ Ada za Shule & Malipo
2️⃣ Njia za Basi & Nauli
3️⃣ Safari za Elimu
4️⃣ Maombi ya Kujiunga
5️⃣ Siku za Wazazi Shuleni
6️⃣ Mapumziko & Kalenda ya Shule
7️⃣ Nyingine / Uliza Swali

_Jibu kwa nambari au andika swali lako moja kwa moja._"""

def get_menu_response(incoming, info):
    """Handle numbered menu selections and sub-menu selections."""
    msg = incoming.strip()

    # ── Main menu ─────────────────────────────────────────────────────────────
    if msg in ["1", "1️⃣"]:
        return (f"💰 *School Fees 2026 — Per Term*\n\n"
                f"• PP1 & PP2: Ksh 13,500 – 14,500/term\n"
                f"• Grade 1 & 2: Ksh 15,500 – 17,000/term\n"
                f"• Grade 3, 4 & 5: Ksh 16,500 – 18,000/term\n"
                f"• Grade 6–9: Ksh 25,000 – 28,000/term _(includes boarding)_\n\n"
                f"_Reply with your child's grade for the exact figure, e.g. *Grade 3* or *PP1*_")

    if msg in ["2", "2️⃣"]:
        return ("🚌 *Bus Routes & Fares*\n\n"
                "Which route would you like fares for? Reply with the route name:\n\n"
                "• *Kapkatet*\n"
                "• *Litein*\n"
                "• *Tebesonik*\n"
                "• *Chemosot*\n"
                "• *Mogogosiek*")

    if msg in ["3", "3️⃣"]:
        return (f"✈️ *Educational Trips — Term II 2026*\n\n"
                f"• Grade 4: {info.get('trip_grade_4', 'TBC')}\n"
                f"• Grade 5: {info.get('trip_grade_5', 'TBC')}\n"
                f"• Grade 6: {info.get('trip_grade_6', 'TBC')}\n"
                f"• Grade 7: {info.get('trip_grade_7', 'TBC')}\n"
                f"• Grade 8: {info.get('trip_grade_8', 'TBC')}\n\n"
                f"💳 Pay via M-Pesa Paybill *{info.get('trip_paybill', '328585')}*, "
                f"Account: {info.get('trip_account_format', '111444#ADM number')}")

    if msg in ["4", "4️⃣"]:
        link = info.get('admissions_form_link', '')
        return (f"🏫 *Admissions — Sally-Ann School*\n\n"
                f"Fill in the form below with your child's details and birth certificate:\n\n"
                f"📋 {link}\n\n"
                f"Our admissions office will contact you within 2 working days.")

    if msg in ["5", "5️⃣"]:
        return (f"👨‍👩‍👧 *Parental Engagement Days*\n\n"
                f"{info.get('parental_days', '')}\n\n"
                f"Please attend on your child's grade day.")

    if msg in ["6", "6️⃣"]:
        return f"📅 *Half Term & Calendar*\n\n{info.get('term_half_term', '')}"

    # ── Fee sub-menu — grade-specific replies ─────────────────────────────────
    msg_lower = msg.lower()

    if msg_lower in ["pp1", "pre-primary 1"]:
        return ("💰 *PP1 Fees 2026*\n\n"
                "• Term 1: Ksh 14,500\n"
                "• Term 2 & 3: Ksh 13,500 each\n\n"
                f"💳 M-Pesa Paybill *{info.get('pay_mpesa_paybill', '777643')}*, Account: ADM No\n"
                f"_Min {info.get('fee_minimum_percent', '60')}% on Reporting Day. No cash._")

    if msg_lower in ["pp2", "pre-primary 2"]:
        return ("💰 *PP2 Fees 2026*\n\n"
                "• All terms: Ksh 13,500/term\n\n"
                f"💳 M-Pesa Paybill *{info.get('pay_mpesa_paybill', '777643')}*, Account: ADM No\n"
                f"_Min {info.get('fee_minimum_percent', '60')}% on Reporting Day. No cash._")

    if msg_lower in ["grade 1", "gr 1", "grade1", "std 1"]:
        return ("💰 *Grade 1 Fees 2026*\n\n"
                "• Term 1: Ksh 15,500 + Ksh 3,500 books = *Ksh 19,000*\n"
                "• Term 2 & 3: Ksh 17,000 each\n\n"
                f"💳 M-Pesa Paybill *{info.get('pay_mpesa_paybill', '777643')}*, Account: ADM No\n"
                f"_Min {info.get('fee_minimum_percent', '60')}% on Reporting Day. No cash._")

    if msg_lower in ["grade 2", "gr 2", "grade2", "std 2"]:
        return ("💰 *Grade 2 Fees 2026*\n\n"
                "• Term 1: Ksh 15,500 + Ksh 1,000 books = *Ksh 16,500*\n"
                "• Term 2 & 3: Ksh 17,000 each\n\n"
                f"💳 M-Pesa Paybill *{info.get('pay_mpesa_paybill', '777643')}*, Account: ADM No\n"
                f"_Min {info.get('fee_minimum_percent', '60')}% on Reporting Day. No cash._")

    if msg_lower in ["grade 3", "gr 3", "grade3", "std 3"]:
        return ("💰 *Grade 3 Fees 2026*\n\n"
                "• Term 1: Ksh 16,500 + Ksh 1,000 books = *Ksh 17,500*\n"
                "• Term 2 & 3: Ksh 18,000 each\n\n"
                f"💳 M-Pesa Paybill *{info.get('pay_mpesa_paybill', '777643')}*, Account: ADM No\n"
                f"_Min {info.get('fee_minimum_percent', '60')}% on Reporting Day. No cash._")

    if msg_lower in ["grade 4", "gr 4", "grade4", "std 4"]:
        return ("💰 *Grade 4 Fees 2026*\n\n"
                "• Term 1: Ksh 16,500 + Ksh 1,000 books = *Ksh 17,500*\n"
                "• Term 2 & 3: Ksh 18,000 each\n\n"
                f"💳 M-Pesa Paybill *{info.get('pay_mpesa_paybill', '777643')}*, Account: ADM No\n"
                f"_Min {info.get('fee_minimum_percent', '60')}% on Reporting Day. No cash._")

    if msg_lower in ["grade 5", "gr 5", "grade5", "std 5"]:
        return ("💰 *Grade 5 Fees 2026*\n\n"
                "• Term 1: Ksh 16,500 + Ksh 1,000 books = *Ksh 17,500*\n"
                "• Term 2 & 3: Ksh 18,000 each\n\n"
                f"💳 M-Pesa Paybill *{info.get('pay_mpesa_paybill', '777643')}*, Account: ADM No\n"
                f"_Min {info.get('fee_minimum_percent', '60')}% on Reporting Day. No cash._")

    if msg_lower in ["grade 6", "gr 6", "grade6", "std 6"]:
        return ("💰 *Grade 6 Fees 2026*\n\n"
                "• Term 1: Ksh 25,000 + Ksh 1,000 books = *Ksh 26,000*\n"
                "• Term 2 & 3: Ksh 26,500 each\n"
                "_(Includes boarding)_\n\n"
                f"💳 M-Pesa Paybill *{info.get('pay_mpesa_paybill', '777643')}*, Account: ADM No\n"
                f"_Min {info.get('fee_minimum_percent', '60')}% on Reporting Day. No cash._")

    if msg_lower in ["grade 7", "gr 7", "grade7", "std 7"]:
        return ("💰 *Grade 7 Fees 2026*\n\n"
                "• Term 1: Ksh 26,500 + Ksh 1,000 books = *Ksh 27,500*\n"
                "• Term 2 & 3: Ksh 28,000 each\n"
                "_(Includes boarding)_\n\n"
                f"💳 M-Pesa Paybill *{info.get('pay_mpesa_paybill', '777643')}*, Account: ADM No\n"
                f"_Min {info.get('fee_minimum_percent', '60')}% on Reporting Day. No cash._")

    if msg_lower in ["grade 8", "gr 8", "grade8", "std 8"]:
        return ("💰 *Grade 8 Fees 2026*\n\n"
                "• Term 1: Ksh 26,500 + Ksh 1,000 books = *Ksh 27,500*\n"
                "• Term 2 & 3: Ksh 28,000 each\n"
                "_(Includes boarding)_\n\n"
                f"💳 M-Pesa Paybill *{info.get('pay_mpesa_paybill', '777643')}*, Account: ADM No\n"
                f"_Min {info.get('fee_minimum_percent', '60')}% on Reporting Day. No cash._")

    if msg_lower in ["grade 9", "gr 9", "grade9", "std 9"]:
        return ("💰 *Grade 9 Fees 2026*\n\n"
                "• Term 1: Ksh 28,000 + Ksh 1,000 books = *Ksh 29,000*\n"
                "• Term 2: Ksh 28,000\n"
                "• Term 3: Ksh 25,000\n"
                "_(Includes boarding)_\n\n"
                f"💳 M-Pesa Paybill *{info.get('pay_mpesa_paybill', '777643')}*, Account: ADM No\n"
                f"_Min {info.get('fee_minimum_percent', '60')}% on Reporting Day. No cash._")

    # ── Bus route sub-menu ────────────────────────────────────────────────────
    if msg_lower == "kapkatet":
        return f"🚌 *Kapkatet Route — Monthly Fares*\n\n{info.get('bus_kapkatet', '')}"

    if msg_lower == "litein":
        return f"🚌 *Litein Route — Monthly Fares*\n\n{info.get('bus_litein', '')}"

    if msg_lower == "tebesonik":
        return f"🚌 *Tebesonik Route — Monthly Fares*\n\n{info.get('bus_tebesonik', '')}"

    if msg_lower == "chemosot":
        return f"🚌 *Chemosot Route — Monthly Fares*\n\n{info.get('bus_chemosot', '')}"

    if msg_lower == "mogogosiek":
        return f"🚌 *Mogogosiek Route — Monthly Fares*\n\n{info.get('bus_mogogosiek', '')}"

    return None  # Not a menu selection — let normal flow handle it

TOPIC_GROUPS = {
    "School fees & payment":  ["fee","ada","pay","mpesa"],
    "Bus routes & fares":     ["bus","basi","kapkatet","litein","tebesonik","chemosot","mogogosiek"],
    "Educational trips":      ["trip","safari"],
    "Parental engagement":    ["meeting"],
    "ICT programme":          ["ict"],
}
FAQ_KEYWORDS = [kw for kws in TOPIC_GROUPS.values() for kw in kws] + \
               ["holiday","likizo","admission","uniform","grade","class","result","exam"]

# ── Escalation triggers ─────────────────────────────────────────────────────────
ESCALATION_KEYWORDS = [
    "complaint", "complain", "refund", "transfer", "lost", "emergency", "urgent",
    "sick", "injury", "injured", "accident", "bully", "bullying", "abuse",
    "harassment", "lawyer", "police", "expel", "expelled", "suspend", "suspended",
    "receipt",
    "malalamiko", "dharura", "kashe", "unyanyasaji", "udhalilishaji",
]

UNCERTAINTY_PHRASES = [
    "i'm not sure", "i am not sure", "i don't have that information",
    "i do not have that information", "i'm unable to", "i am unable to",
    "i don't know", "i do not know", "please call the school office",
    "contact the school office", "i can't help with that", "i cannot help with that",
]

def needs_escalation(parent_message, bot_reply=None):
    """Returns (escalate: bool, keyword_hit: str | None).
    keyword_hit is the matched ESCALATION_KEYWORD, or None when the trigger
    was an uncertainty phrase in the bot's reply rather than the parent's text.
    Callers should unpack: escalates, keyword_hit = needs_escalation(...)"""
    msg_lower = parent_message.lower()
    for kw in ESCALATION_KEYWORDS:
        if kw in msg_lower:
            return True, kw
    if bot_reply:
        reply_lower = bot_reply.lower()
        for phrase in UNCERTAINTY_PHRASES:
            if phrase in reply_lower:
                return True, None
    return False, None

KEYWORD_RESPONSES = {
    "hello": GREETING_MENU,
    "hi": GREETING_MENU,
    "hey": GREETING_MENU,
    "start": GREETING_MENU,
    "menu": GREETING_MENU,
    "hujambo": GREETING_MENU_SW,
    "habari": GREETING_MENU_SW,
    "sasa": GREETING_MENU_SW,
    "mambo": GREETING_MENU_SW,
    "msaada": GREETING_MENU_SW,
    # Payment quick answers — most asked, needs zero AI
    "paybill": "💳 *Fee Payment Paybill:* 777643, Account: ADM number\n💳 *Trip Payment Paybill:* 328585, Account: 111444#ADM number\n\nNo cash accepted at school.",
    "mpesa": "💳 *Fee Payment:* Paybill 777643, Account: ADM number\n💳 *Trip Payment:* Paybill 328585, Account: 111444#ADM number",
    "how to pay": "💳 *Fee Payment:* M-Pesa Paybill *777643*, Account: your child's ADM number\n💳 *Trip Payment:* Paybill *328585*, Account: 111444#ADM number\n\nAlso accepted: KCB 1135294917 | Equity 0530291926992 | Coop 01148786054900 | Chai Sacco 1083225",
    "jinsi ya kulipa": "💳 Lipa ada: Paybill *777643*, Akaunti: Nambari ya ADM\n💳 Safari: Paybill *328585*, Akaunti: 111444#ADM",
    # Admissions
    "admission": f"🏫 *Admissions — Sally-Ann School*\n\nFill in the form below with your child's details and birth certificate:\n\nhttps://docs.google.com/forms/d/e/1FAIpQLSemf1iZghMpupJ98AeCqyMSdUfqqsqyPmaTdnmtm9Pc2LLkFg/viewform\n\nOur admissions office will contact you within 2 working days.",
    "admissions": f"🏫 *Admissions — Sally-Ann School*\n\nFill in the form below with your child's details and birth certificate:\n\nhttps://docs.google.com/forms/d/e/1FAIpQLSemf1iZghMpupJ98AeCqyMSdUfqqsqyPmaTdnmtm9Pc2LLkFg/viewform\n\nOur admissions office will contact you within 2 working days.",
    "enroll": f"🏫 *Admissions — Sally-Ann School*\n\nFill the admissions form here:\nhttps://docs.google.com/forms/d/e/1FAIpQLSemf1iZghMpupJ98AeCqyMSdUfqqsqyPmaTdnmtm9Pc2LLkFg/viewform\n\nWe'll contact you within 2 working days.",
    "join": f"🏫 To apply for admission to Sally-Ann School, fill in this form:\nhttps://docs.google.com/forms/d/e/1FAIpQLSemf1iZghMpupJ98AeCqyMSdUfqqsqyPmaTdnmtm9Pc2LLkFg/viewform",
    # Contacts
    "phone": "📞 *Sally-Ann School:* 0727839424\n📧 sas@sallyannschool.sc.ke\n📍 P.O Box 401-20210, Litein",
    "contact": "📞 *Sally-Ann School:* 0727839424\n📧 sas@sallyannschool.sc.ke\n📍 P.O Box 401-20210, Litein",
    "number": "📞 *School phone:* 0727839424",
    "location": "📍 Sally-Ann School is located in Litein, Kericho County.\nP.O Box 401-20210, Litein",
    # Polite closers
    "thank": "You're welcome! Feel free to ask anything else. 😊",
    "thanks": "You're welcome! Feel free to ask anything else. 😊",
    "thank you": "You're welcome! Feel free to ask anything else. 😊",
    "asante": "Karibu sana! Niulize swali lolote. 😊",
    "ok": "👍 Let me know if you need anything else!",
    "okay": "👍 Let me know if you need anything else!",
    "sawa": "👍 Niulize kama una swali lingine!",
}

# Both dicts are capped (OrderedDict + popitem) so they don't grow forever.
# _last_inbound_time: tracks when each phone last sent a message, used to
#   measure bot response latency. Capped at 1 000 entries (more than enough
#   for any realistic simultaneous-conversation count for a school bot).
# _seen_message_ids: deduplicates Meta webhook retries.
_last_inbound_time: collections.OrderedDict = collections.OrderedDict()
_LAST_INBOUND_MAX = 1000

# ── Webhook deduplication ──────────────────────────────────────────────────────
# Meta retries webhook delivery if it doesn't get a fast enough 200 response,
# which can cause the same message to be processed (and replied to) multiple
# times. Each WhatsApp message has a unique id (WAMID) we can use to detect
# and skip duplicates. Kept small and capped so memory doesn't grow forever.
_seen_message_ids: collections.OrderedDict = collections.OrderedDict()
_SEEN_IDS_MAX = 500

def is_duplicate_message(msg_id):
    """Returns True if we've already processed this WhatsApp message id."""
    if not msg_id:
        return False
    if msg_id in _seen_message_ids:
        return True
    _seen_message_ids[msg_id] = True
    if len(_seen_message_ids) > _SEEN_IDS_MAX:
        _seen_message_ids.popitem(last=False)  # drop oldest
    return False

def find_keyword_response(message):
    msg_lower = message.lower().strip()
    if msg_lower in KEYWORD_RESPONSES:
        return KEYWORD_RESPONSES[msg_lower], False
    for keyword, response in KEYWORD_RESPONSES.items():
        if keyword in msg_lower:
            return response, False
    return None, True

def ask_groq(messages):
    # Primary: llama-3.3-70b-versatile for better reasoning and accuracy.
    # Falls back to the faster 8b model if the 70b times out or is overloaded.
    for model in ("llama-3.3-70b-versatile", "llama-3.1-8b-instant"):
        try:
            response = groq_client.chat.completions.create(
                messages=messages, model=model,
                max_tokens=300, temperature=0.4,
            )
            logger.info(f"Groq model used: {model}")
            return response.choices[0].message.content.strip()
        except Exception as e:
            if model == "llama-3.1-8b-instant":
                raise  # last resort — let ask_ai() handle the error
            logger.warning(f"Groq {model} failed ({e}), trying fallback model")

def ask_gemini(user_message, history, school_context=None):
    """Call Gemini. Accepts an optional pre-built school_context string to
    avoid rebuilding (and re-querying) it when ask_ai() already did so."""
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}")
    gemini_history = []
    for msg in history:
        role = "user" if msg["role"] == "user" else "model"
        gemini_history.append({"role": role, "parts": [{"text": msg["content"]}]})
    gemini_history.append({"role": "user", "parts": [{"text": user_message}]})
    payload = {
        "system_instruction": {"parts": [{"text": school_context or build_school_context()}]},
        "contents": gemini_history,
        "generationConfig": {"maxOutputTokens": 300, "temperature": 0.4},
    }
    r = requests.post(url, json=payload, timeout=10)
    r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()

def ask_ai(phone, message):
    history = db.get_history(phone)
    # Build context once — reused for both Groq and the Gemini fallback so we
    # don't hit the DB (or rebuild the prompt string) a second time on fallback.
    context = build_school_context()
    messages = [{"role": "system", "content": context}]
    messages.extend(history)
    messages.append({"role": "user", "content": message})
    reply = None
    try:
        reply = ask_groq(messages)
        logger.info(f"[{phone}] Groq OK")
    except Exception as e:
        logger.error(f"[{phone}] Groq error: {e}")
        try:
            reply = ask_gemini(message, history, school_context=context)
            logger.info(f"[{phone}] Gemini fallback OK")
        except Exception as e2:
            logger.error(f"[{phone}] Gemini error: {e2}")
            reply = "Sorry, I'm having trouble right now. Please call the school office directly."
    history.append({"role": "user", "content": message})
    history.append({"role": "assistant", "content": reply})
    db.save_history(phone, history[-20:])
    return reply

def normalize_phone(p):
    """Strip whatsapp: prefix, +, spaces, and dashes so numbers compare reliably,
    since Meta's webhook 'from' field is digits-only (no + and no prefix)."""
    if not p:
        return ""
    return re.sub(r"[^\d]", "", p)

def send_whatsapp(to, body):
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": body}}
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=10)
        if not r.ok:
            logger.error(f"Meta send error: {r.status_code} {r.text}")
        return r.ok
    except Exception as e:
        logger.error(f"send_whatsapp error: {e}")
        return False

def get_media_url(media_id):
    """Step 1 of downloading media from Meta: resolve a media id to a temporary download URL."""
    url = f"https://graph.facebook.com/v19.0/{media_id}"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    try:
        r = requests.get(url, headers=headers, timeout=10)
        r.raise_for_status()
        return r.json().get("url")
    except Exception as e:
        logger.error(f"get_media_url error: {e}")
        return None

def download_media(media_url):
    """Step 2: download the actual file bytes from the resolved URL."""
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    try:
        r = requests.get(media_url, headers=headers, timeout=20)
        r.raise_for_status()
        return r.content
    except Exception as e:
        logger.error(f"download_media error: {e}")
        return None

def upload_media(file_bytes, mime_type):
    """Upload raw bytes to Meta so we can re-send them as a message (used when
    forwarding a parent's image to the admin, or vice versa)."""
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/media"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    files = {"file": ("file", file_bytes, mime_type)}
    data = {"messaging_product": "whatsapp"}
    try:
        r = requests.post(url, headers=headers, files=files, data=data, timeout=30)
        r.raise_for_status()
        return r.json().get("id")
    except Exception as e:
        logger.error(f"upload_media error: {e}")
        return None

def send_whatsapp_media(to, media_id, media_type, caption=None):
    """Send an image or document (already uploaded to Meta, or forwarded by id) to a number."""
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    media_obj = {"id": media_id}
    if caption:
        media_obj["caption"] = caption
    payload = {"messaging_product": "whatsapp", "to": to, "type": media_type, media_type: media_obj}
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=15)
        if not r.ok:
            logger.error(f"Meta media send error: {r.status_code} {r.text}")
        return r.ok
    except Exception as e:
        logger.error(f"send_whatsapp_media error: {e}")
        return False

def forward_media(media_id, mime_type, media_type, to, caption=None):
    """Download a piece of media from Meta and re-send it to a different number.
    Used to forward a parent's payment receipt to the admin, or an admin's
    image/document to a parent."""
    media_url = get_media_url(media_id)
    if not media_url:
        return False
    file_bytes = download_media(media_url)
    if not file_bytes:
        return False
    new_media_id = upload_media(file_bytes, mime_type)
    if not new_media_id:
        return False
    return send_whatsapp_media(to, new_media_id, media_type, caption=caption)

def alert_admin(parent_phone, parent_message, reason):
    """Notify the school admin on WhatsApp that a parent query needs a human reply.
    Includes a short reply code the admin can use to reply directly from their phone."""
    if not ADMIN_WHATSAPP_NUMBER:
        logger.warning("ADMIN_WHATSAPP_NUMBER not set — cannot send admin alert")
        return False
    parent_display = parent_phone.replace("whatsapp:", "")
    code = normalize_phone(parent_phone)[-4:]
    db.save_reply_code(code, parent_phone)
    alert_text = (
        f"⚠️ Sally-Ann Bot Alert\n\n"
        f"Parent: {parent_display}\n"
        f"Reason: {reason}\n\n"
        f"Message: \"{parent_message}\"\n\n"
        f"📲 To reply directly, send:\n"
        f"{code}: your reply here\n\n"
        f"Or open the dashboard to take over fully."
    )
    return send_whatsapp(ADMIN_WHATSAPP_NUMBER, alert_text)

_warned_no_app_secret = False

def verify_signature(req):
    global _warned_no_app_secret
    if not APP_SECRET:
        if not _warned_no_app_secret:
            logger.warning(
                "⚠️⚠️⚠️ APP_SECRET is NOT set — webhook signature verification "
                "is DISABLED. Anyone who finds this URL can POST fake messages "
                "as if they came from WhatsApp. Set APP_SECRET in Koyeb env vars "
                "(from your Meta App's dashboard) to fix this."
            )
            _warned_no_app_secret = True
        return True
    signature = req.headers.get("X-Hub-Signature-256", "")
    if not signature.startswith("sha256="):
        return False
    expected = hmac.new(APP_SECRET.encode(), req.data, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)

def log_msg(phone, message, direction="inbound", sender="bot"):
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    db.log_message(phone, message, direction, sender)

    if direction == "inbound":
        db.increment_daily(today_str, "inbound")
        _last_inbound_time[phone] = now
        if len(_last_inbound_time) > _LAST_INBOUND_MAX:
            _last_inbound_time.popitem(last=False)  # evict oldest
        for kw in FAQ_KEYWORDS:
            if kw in message.lower():
                db.increment_faq(kw)
    else:
        metric = "outbound_admin" if sender == "admin" else "outbound_bot"
        db.increment_daily(today_str, metric)
        if phone in _last_inbound_time:
            delta = (now - _last_inbound_time[phone]).total_seconds()
            if 0 <= delta < 600:
                db.add_response_time(delta)

def get_conv_status(last_message, last_direction):
    if last_direction == "inbound":
        return "pending"
    if last_message and (last_message.startswith("[ADMIN]") or last_message.startswith("[DIRECT]") or last_message.startswith("[BROADCAST]")):
        return "override"
    return "resolved"

# ══════════════════════════════════════════════════════════════════════════════
# WHATSAPP WEBHOOK
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/")
def home():
    return "✅ Sally-Ann School WhatsApp Bot is running! Admin: /admin"

@app.route("/health")
def health():
    # Lightweight, dependency-free endpoint for Koyeb's HTTP health check.
    # Deliberately does not touch the database or any external API — it only
    # needs to prove the Flask worker itself is alive and able to respond,
    # so it stays fast and reliable even if Groq or Postgres are briefly slow.
    return jsonify({"status": "ok"}), 200

@app.route("/webhook", methods=["GET"])
def verify():
    mode, token, challenge = (request.args.get("hub.mode"),
                               request.args.get("hub.verify_token"),
                               request.args.get("hub.challenge"))
    if mode == "subscribe" and token == VERIFY_TOKEN:
        logger.info("Webhook verified ✅")
        return challenge, 200
    return "Forbidden", 403

@app.route("/webhook", methods=["POST"])
def webhook():
    if not verify_signature(request):
        return "Unauthorized", 401
    data = request.get_json()

    # Acknowledge Meta immediately. All real work happens in a background
    # thread — Meta retries delivery if it doesn't get a fast 200, and those
    # retries were causing duplicate AI replies and duplicate sends. Returning
    # right away (before any AI calls, WhatsApp sends, or media downloads)
    # means Meta never has a reason to retry in the first place.
    threading.Thread(target=process_webhook_event, args=(data,), daemon=True).start()
    return jsonify({"status": "ok"}), 200

def process_webhook_event(data):
    try:
        value = data["entry"][0]["changes"][0]["value"]

        if "messages" not in value:
            if "statuses" in value:
                logger.debug("Ignoring status update (delivery/read receipt)")
            else:
                logger.debug(f"Ignoring unhandled webhook event: {list(value.keys())}")
            return

        message  = value["messages"][0]
        phone    = message["from"]
        msg_type = message["type"]
        msg_id   = message.get("id")
        logger.info(f"[{phone}] Type: {msg_type} id={msg_id}")

        if is_duplicate_message(msg_id):
            logger.info(f"[{phone}] Duplicate webhook delivery for message {msg_id} — skipping")
            return

        # Always-visible diagnostic for admin number matching (INFO level so it
        # actually shows up in Koyeb logs at the default logging level).
        if ADMIN_WHATSAPP_NUMBER:
            sender_norm = normalize_phone(phone)
            admin_norm  = normalize_phone(ADMIN_WHATSAPP_NUMBER)
            logger.info(f"🔍 Admin match check: sender={sender_norm} admin={admin_norm} match={sender_norm == admin_norm}")

        # ── Admin reply-by-phone (sticky session + media support) ──────────
        # Flow:
        #  1. Admin sends "1234: message" (or ; - , .) -> starts a session with
        #     that parent AND forwards the message. Code no longer needed for
        #     follow-up messages.
        #  2. While a session is active, every message from the admin (text,
        #     image, or document) is forwarded straight to that parent.
        #  3. Admin sends "done" or "release" -> ends the session, returns
        #     control to the bot for that parent.
        if ADMIN_WHATSAPP_NUMBER and normalize_phone(phone) == normalize_phone(ADMIN_WHATSAPP_NUMBER):
            active_session_phone = db.get_active_admin_session()

            # Check for "done"/"release" command to end an active session
            if msg_type == "text":
                admin_text_check = message["text"]["body"].strip().lower()
                if active_session_phone and admin_text_check in ("done", "release", "end", "close"):
                    db.set_admin_takeover(active_session_phone, False)
                    db.clear_active_admin_session()
                    send_whatsapp(ADMIN_WHATSAPP_NUMBER,
                        f"✅ Session ended. {active_session_phone.replace('whatsapp:','')} returned to bot control.")
                    logger.info(f"Admin ended session with {active_session_phone}")
                    return

            # Try to start/continue a session and forward this message
            target_phone = None
            reply_text = None

            if msg_type == "text":
                admin_text = message["text"]["body"].strip()
                m = re.match(r"^(\d{4})\s*[:;\-,.]\s*(.+)$", admin_text, re.DOTALL)
                if m:
                    # New session started via code
                    code, reply_text = m.group(1), m.group(2).strip()
                    target_phone = db.get_phone_by_code(code)
                    if target_phone:
                        db.set_active_admin_session(target_phone)
                        db.set_admin_takeover(target_phone, True)
                elif active_session_phone:
                    # Continuing an existing session, no code needed
                    target_phone = active_session_phone
                    reply_text = admin_text
                else:
                    logger.info(f"Admin sent a message with no active session and no code match: {admin_text!r}")

            elif msg_type in ("image", "document"):
                if not active_session_phone:
                    send_whatsapp(ADMIN_WHATSAPP_NUMBER,
                        "ℹ️ No active conversation to send this to. Start a session first by "
                        "replying to a parent's 4-digit code, or take over a conversation from the dashboard.")
                    return
                # Forward media to the parent currently in session
                target_phone = active_session_phone
                media_block = message.get(msg_type, {})
                media_id = media_block.get("id")
                mime_type = media_block.get("mime_type", "image/jpeg")
                caption = media_block.get("caption")
                if media_id:
                    ok = forward_media(media_id, mime_type, msg_type, target_phone, caption=caption)
                    if ok:
                        log_msg(target_phone, f"[ADMIN] [{msg_type} sent]" + (f" {caption}" if caption else ""),
                                "outbound", sender="admin")
                        db.clear_escalated(target_phone)
                        send_whatsapp(ADMIN_WHATSAPP_NUMBER,
                            f"✅ {msg_type.capitalize()} sent to {target_phone.replace('whatsapp:','')}")
                        logger.info(f"Admin {msg_type} forwarded to {target_phone}")
                    else:
                        send_whatsapp(ADMIN_WHATSAPP_NUMBER, f"⚠️ Failed to forward {msg_type} to parent.")
                        logger.warning(f"Failed to forward admin {msg_type} to {target_phone}")
                return

            if target_phone and reply_text:
                log_msg(target_phone, f"[ADMIN] {reply_text}", "outbound", sender="admin")
                history = db.get_history(target_phone)
                history.append({"role": "assistant", "content": reply_text})
                db.save_history(target_phone, history[-20:])
                send_whatsapp(target_phone, reply_text)
                db.clear_escalated(target_phone)
                session_note = "" if active_session_phone == target_phone else " — session started, reply without a code until you type 'done'"
                send_whatsapp(ADMIN_WHATSAPP_NUMBER,
                    f"✅ Sent to {target_phone.replace('whatsapp:','')}: \"{reply_text}\"{session_note}")
                logger.info(f"Admin message forwarded to {target_phone}")
                return
            elif msg_type == "text" and re.match(r"^\d{4}\s*[:;\-,.]", message["text"]["body"].strip()):
                # Looked like a code attempt but the code wasn't found
                send_whatsapp(ADMIN_WHATSAPP_NUMBER,
                    "⚠️ No pending conversation found for that code. It may have expired or already been handled.")
                return
            elif msg_type == "text":
                # Plain text from the admin's own number with no active
                # session and no code prefix — there's nothing to forward it
                # to. Previously this fell through to the normal
                # parent-message handler "in case admin is also a parent",
                # which produced nonsensical output like the admin getting a
                # message forwarded to themselves. Give clear guidance instead.
                send_whatsapp(ADMIN_WHATSAPP_NUMBER,
                    "ℹ️ No active conversation to reply to. To reply to a parent, "
                    "send their 4-digit code followed by a colon, e.g. \"2407: your reply\", "
                    "or use the dashboard to take over a conversation.")
                return

        name = None
        try:
            contacts = value.get("contacts", [])
            if contacts:
                name = contacts[0].get("profile", {}).get("name")
        except Exception:
            pass
        db.touch_active_user(phone, name)

        if msg_type in ("image", "document"):
            log_msg(phone, f"[{msg_type} received]", "inbound")

            if db.is_admin_takeover(phone) and ADMIN_WHATSAPP_NUMBER:
                # Admin is handling this conversation directly — forward the
                # media straight to them instead of the bot's canned reply.
                media_block = message.get(msg_type, {})
                media_id = media_block.get("id")
                mime_type = media_block.get("mime_type", "image/jpeg")
                caption = media_block.get("caption", "")
                parent_display = phone.replace("whatsapp:", "")
                if media_id:
                    ok = forward_media(media_id, mime_type, msg_type, ADMIN_WHATSAPP_NUMBER,
                                        caption=f"From {parent_display}" + (f": {caption}" if caption else ""))
                    if not ok:
                        send_whatsapp(ADMIN_WHATSAPP_NUMBER,
                            f"⚠️ Parent {parent_display} sent a {msg_type} but it couldn't be forwarded. Ask them to resend.")
                return

            # Not under takeover — bot handles with a standard auto-reply,
            # and if this looks like a payment receipt, escalate so a human
            # double-checks it.
            if msg_type == "image":
                reply = ("Thank you for sending your payment receipt! 📸\n"
                         "Our office will confirm within 24 hours.\n"
                         "For instant confirmation call the school office.")
            else:
                reply = ("Thank you for the document! 📄\n"
                         "Our office will review it and get back to you shortly.")

            if not db.is_bot_paused():
                log_msg(phone, reply, "outbound", sender="bot")
                send_whatsapp(phone, reply)

                # Forward the actual file to admin too so they can review it,
                # and flag the conversation for follow-up.
                if ADMIN_WHATSAPP_NUMBER:
                    media_block = message.get(msg_type, {})
                    media_id = media_block.get("id")
                    mime_type = media_block.get("mime_type", "image/jpeg")
                    parent_display = phone.replace("whatsapp:", "")
                    code = normalize_phone(phone)[-4:]
                    db.save_reply_code(code, phone)
                    if media_id:
                        forward_media(media_id, mime_type, msg_type, ADMIN_WHATSAPP_NUMBER,
                                      caption=f"📎 {msg_type} from {parent_display} — reply with {code}: to respond")
                    db.set_escalated(phone, f"Parent sent a {msg_type} (e.g. payment receipt) needing review")
            return

        if msg_type != "text":
            send_whatsapp(phone, "Sorry, I can only handle text, image, or document messages for now.")
            return

        incoming = message["text"]["body"].strip()
        # Defensive cap — WhatsApp's own client already limits messages to
        # ~4096 chars, but this protects DB storage size and Groq token cost
        # if that ever changes, and stops any single message from dominating
        # the conversation history we send to the AI on every subsequent turn.
        MAX_INCOMING_LEN = 2000
        if len(incoming) > MAX_INCOMING_LEN:
            incoming = incoming[:MAX_INCOMING_LEN] + " […message truncated]"
        log_msg(phone, incoming, "inbound")

        if db.is_admin_takeover(phone):
            logger.info(f"[{phone}] Under admin takeover — forwarding parent reply to admin")
            if ADMIN_WHATSAPP_NUMBER:
                parent_display = phone.replace("whatsapp:", "")
                code = normalize_phone(phone)[-4:]
                db.save_reply_code(code, phone)
                send_whatsapp(ADMIN_WHATSAPP_NUMBER,
                    f"💬 {parent_display}: {incoming}\n(reply with {code}: to respond)")
            return
        if db.is_bot_paused():
            logger.info(f"[{phone}] Bot is paused globally — staying silent")
            return

        # ── Numbered menu handler ─────────────────────────────────────────
        # Check if parent is selecting from the intro menu (1-7) before
        # going to keyword matching or AI — these are instant, no AI cost.
        school_info = get_cached_school_info()
        menu_reply = get_menu_response(incoming, school_info)
        if menu_reply:
            log_msg(phone, menu_reply, "outbound", sender="bot")
            send_whatsapp(phone, menu_reply)
            return

        reply, use_ai = find_keyword_response(incoming)
        if use_ai:
            # Send instant acknowledgement so parent knows message was received
            # while AI processes — eliminates the "is it working?" anxiety
            send_whatsapp(phone, "⏳ Looking that up for you...")
            reply = ask_ai(phone, incoming)
        else:
            history = db.get_history(phone)
            history.append({"role": "user", "content": incoming})
            history.append({"role": "assistant", "content": reply})
            db.save_history(phone, history[-20:])

        # ── Escalation check ──────────────────────────────────────────────
        # Two distinct triggers: (1) the parent's message contains a sensitive
        # keyword (bullying, complaint, emergency...), or (2) the AI's own
        # reply admitted uncertainty. Either way, we replace whatever the bot
        # was about to say with a single first-person message that owns the
        # escalation directly, rather than sending the generic "I don't know"
        # line and then a separate, disconnected "a team member has been
        # notified" notice — that read as two different voices and undersold
        # what the bot was actually doing.
        escalates, keyword_hit = needs_escalation(incoming, reply)
        is_swahili = keyword_hit in ("malalamiko", "dharura", "kashe", "unyanyasaji", "udhalilishaji") if keyword_hit else False

        if escalates and keyword_hit == "receipt":
            # Routine, not urgent — a parent mentioning a receipt in text just
            # needs someone to confirm/verify it, not the "this is serious"
            # framing used for bullying or emergencies.
            reply = ("Thank you — I'll make sure our office checks on this receipt "
                      "and confirms with you shortly.")
            reason = "Parent mentioned a receipt needing verification"
        elif escalates and keyword_hit:
            # Sensitive topic (bullying, emergency, complaint, etc.) — keep it
            # warm and reassuring, not clinical.
            if is_swahili:
                reply = ("Asante kwa kunijulisha — hili ni jambo muhimu, na ninalipeleka "
                          "kwa timu yetu ya shule sasa hivi ili wawasiliane nawe haraka iwezekanavyo.")
            else:
                reply = ("Thank you for letting us know — this is important, and I'm "
                          "flagging it for our school team right away so they can follow "
                          "up with you personally and as soon as possible.")
            reason = f"Sensitive keyword: '{keyword_hit}'"
        elif escalates:
            # Bot didn't know the answer — own it in first person rather than
            # the flat "I don't have that information" stock line. The AI's
            # own reply (already in the parent's language) tells us which
            # language to use here.
            if reply and ("sina taarifa" in reply.lower() or "ofisi ya shule" in reply.lower()):
                reply = ("Hilo ni swali zuri, na ningependa kukupa jibu sahihi badala ya "
                          "kukisia — ninalipeleka kwa ofisi ya shule sasa na watawasiliana nawe hivi karibuni.")
            else:
                reply = ("That's a good question, and I want to make sure you get the "
                          "right answer rather than guess — I'm passing this on to our "
                          "school office now and they'll follow up with you shortly.")
            reason = "Bot was uncertain of the answer"

        log_msg(phone, reply, "outbound", sender="bot")
        send_whatsapp(phone, reply)

        if escalates:
            alert_admin(phone, incoming, reason)
            db.set_escalated(phone, reason)
            logger.info(f"[{phone}] Escalated to admin — {reason}")

    except (KeyError, IndexError) as e:
        logger.warning(f"Webhook parse error: {e}")
    except Exception as e:
        logger.error(f"Unexpected error processing webhook event: {e}")

    return

# ══════════════════════════════════════════════════════════════════════════════
# ADMIN API ROUTES
# These routes are called by the dashboard (which proxies them so the WhatsApp
# token never leaves this service).  They are protected by a shared secret:
# set BOT_API_KEY in Koyeb env vars and the same value in the dashboard's env
# as BOT_API_KEY.  Falls back to ADMIN_PASSWORD if BOT_API_KEY is not set.
# ══════════════════════════════════════════════════════════════════════════════

_BOT_API_KEY = os.getenv("BOT_API_KEY", "") or ADMIN_PASSWORD

def _require_bot_auth():
    """Return None if the request carries a valid API key, else a 401 Response."""
    auth = request.headers.get("Authorization", "")
    token = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
    if not token or not hmac.compare_digest(token, _BOT_API_KEY):
        return jsonify({"error": "Unauthorized"}), 401
    return None

# ── Per-phone outbound rate limiting ─────────────────────────────────────────
# Prevents the admin dashboard (or a misbehaving script) from accidentally
# flooding a parent's WhatsApp with hundreds of messages.  Each phone number
# is allowed at most _RATE_MAX_MSGS messages within a rolling _RATE_WINDOW_S
# second window.  The store is capped so it doesn't grow forever.
_RATE_WINDOW_S  = 60          # rolling window in seconds
_RATE_MAX_MSGS  = 5           # max messages per phone per window
_rate_store: collections.OrderedDict = collections.OrderedDict()  # phone -> [timestamps]
_RATE_STORE_MAX = 2000

def _is_rate_limited(phone: str) -> bool:
    """Return True if this phone has hit the per-window send cap."""
    now = _time_module.time()
    timestamps = _rate_store.get(phone, [])
    # Drop timestamps outside the rolling window
    timestamps = [t for t in timestamps if now - t < _RATE_WINDOW_S]
    if len(timestamps) >= _RATE_MAX_MSGS:
        return True
    timestamps.append(now)
    _rate_store[phone] = timestamps
    # Evict oldest entry if store is too large
    if len(_rate_store) > _RATE_STORE_MAX:
        _rate_store.popitem(last=False)
    return False


@app.route("/admin/bot/status", methods=["GET"])
def bot_status_route():
    """Return current bot pause state and list of phones under admin takeover."""
    denied = _require_bot_auth()
    if denied:
        return denied
    try:
        paused = db.is_bot_paused()
        takeovers = db.get_takeover_phones()
        return jsonify({"paused": paused, "takeovers": takeovers})
    except Exception as e:
        logger.error(f"/admin/bot/status error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/admin/conversations/<path:phone>/send", methods=["POST"])
def admin_send_route(phone):
    """Send a message to a known parent phone on behalf of the admin.
    Logs it, updates history, and clears the escalated flag."""
    denied = _require_bot_auth()
    if denied:
        return denied

    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "message is required"}), 400

    phone_norm = normalize_phone(phone)
    if not phone_norm:
        return jsonify({"error": "invalid phone"}), 400

    if _is_rate_limited(phone_norm):
        logger.warning(f"[{phone_norm}] Admin send rate-limited")
        return jsonify({"error": "rate_limited", "detail": f"Max {_RATE_MAX_MSGS} msgs/{_RATE_WINDOW_S}s per phone"}), 429

    ok = send_whatsapp(phone_norm, message)
    if ok:
        log_msg(phone_norm, f"[ADMIN] {message}", "outbound", sender="admin")
        history = db.get_history(phone_norm)
        history.append({"role": "assistant", "content": message})
        db.save_history(phone_norm, history[-20:])
        db.clear_escalated(phone_norm)
        db.set_admin_takeover(phone_norm, True)
        logger.info(f"[{phone_norm}] Admin sent via dashboard: {message[:80]}")
        return jsonify({"status": "sent"})
    else:
        return jsonify({"error": "WhatsApp send failed"}), 502


@app.route("/admin/send-direct", methods=["POST"])
def admin_send_direct():
    """Send a message to any phone number (not necessarily a known parent).
    Used by the dashboard's 'New Conversation' / direct-send feature."""
    denied = _require_bot_auth()
    if denied:
        return denied

    data = request.get_json(silent=True) or {}
    phone   = (data.get("phone") or "").strip()
    message = (data.get("message") or "").strip()
    if not phone or not message:
        return jsonify({"error": "phone and message are required"}), 400

    phone_norm = normalize_phone(phone)
    if not phone_norm:
        return jsonify({"error": "invalid phone"}), 400

    if _is_rate_limited(phone_norm):
        return jsonify({"error": "rate_limited", "detail": f"Max {_RATE_MAX_MSGS} msgs/{_RATE_WINDOW_S}s per phone"}), 429

    ok = send_whatsapp(phone_norm, message)
    if ok:
        log_msg(phone_norm, f"[DIRECT] {message}", "outbound", sender="admin")
        logger.info(f"[{phone_norm}] Direct admin message sent: {message[:80]}")
        return jsonify({"status": "sent"})
    else:
        return jsonify({"error": "WhatsApp send failed"}), 502


@app.route("/admin/broadcast", methods=["POST"])
def admin_broadcast():
    """Send a message to a list of phones (or all parents if none specified).
    Returns per-phone success/failure so the dashboard can show a summary."""
    denied = _require_bot_auth()
    if denied:
        return denied

    data    = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    phones  = data.get("phones") or []

    if not message:
        return jsonify({"error": "message is required"}), 400

    if not phones:
        try:
            phones = db.get_active_user_phones()
        except Exception as e:
            logger.error(f"/admin/broadcast: failed to fetch phones: {e}")
            return jsonify({"error": f"Could not fetch parent phones: {e}"}), 500

    if not phones:
        return jsonify({"error": "no phones to send to"}), 400

    results = {}
    sent = 0
    failed = 0
    skipped = 0

    for raw_phone in phones:
        phone_norm = normalize_phone(str(raw_phone))
        if not phone_norm:
            skipped += 1
            continue
        if _is_rate_limited(phone_norm):
            results[phone_norm] = "rate_limited"
            skipped += 1
            continue
        ok = send_whatsapp(phone_norm, message)
        if ok:
            log_msg(phone_norm, f"[BROADCAST] {message}", "outbound", sender="admin")
            results[phone_norm] = "sent"
            sent += 1
        else:
            results[phone_norm] = "failed"
            failed += 1

    try:
        db.save_broadcast(message, list(results.keys()), sent, failed)
    except Exception as e:
        logger.warning(f"broadcast: could not save record: {e}")

    logger.info(f"Broadcast: sent={sent} failed={failed} skipped={skipped} msg={message[:60]!r}")
    return jsonify({"sent": sent, "failed": failed, "skipped": skipped, "results": results})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 8000)))
