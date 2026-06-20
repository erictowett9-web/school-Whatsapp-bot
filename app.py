import os
import re
import logging
import hashlib
import hmac
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

# ── Startup diagnostics (always visible at INFO level) ────────────────────────
if ADMIN_WHATSAPP_NUMBER:
    logger.info(f"✅ ADMIN_WHATSAPP_NUMBER configured: {ADMIN_WHATSAPP_NUMBER}")
else:
    logger.warning("⚠️ ADMIN_WHATSAPP_NUMBER is NOT set — admin alerts and reply-by-phone will not work")

# ── School context ─────────────────────────────────────────────────────────────
SCHOOL_CONTEXT = """
You are a friendly and helpful WhatsApp assistant for Sally-Ann School Limited in Litein, Kenya.
Answer questions from parents about the school.

SCHOOL FEES 2026:
- Grade 1 & 2: Ksh 15,500 per term
- ICT Coding & Robotics: Ksh 1,500 per term
- Total: Ksh 17,000 per term
- New admission: Ksh 2,000
- At least 60% paid on Reporting Day. No cash accepted.

PAYMENT:
- M-Pesa Paybill: 777643, Account: ADM number
- KCB: 1135294917 | Equity: 0530291926992
- Equity Paybill: 247247, Account: 926992#ADM number
- Coop Bank: 01148786054900 | Chai Sacco: 1083225

BUS ROUTES (per month):
Kapkatet: Koitabai 2300, Daraja Sita 1950, Factory 1850, Town 1600, Chematich 1850, Kapkatolonyi 1250, Kaptote 1150, DC Jct 950
Litein: Town/St Kizitos 950, Factory Gate 1050, Kwa Soi/Joyland 1150, Imarisha 1150, Kusumek 1600
Tebesonik: Lalagin 1250, Kiptewit Jct 1500, Cheborge 1600, Korongoi 1700, Bokoiyot/Factory 2300
Chemosot: Cheluget 1250, Chelilis/Chesingoro 1600, Kaminjeiwet/Getarwet Jct 1700
Mogogosiek: Murram 2600, Mogogosiek 2500, Boito Kaptien Rd 1850, Boito Shopping 1600, Chemoiben 1400, DC Residence 1050

TRIPS TERM II 2026: Grade 4 Maasai Mara 2500, Grade 5 Nakuru, Grade 6 Naivasha 3500, Grade 7 Nairobi 5000, Grade 8 Mombasa 15000

PARENTAL DAYS: Grade 5 May 16, Grade 4 May 23, Grade 3 May 30, Grade 2 Jun 6, Grade 1 Jun 13, PP1&PP2 Jun 20. Half Term Jun 24-28.

ICT DIGISKOOL: Coding, Robotics & AI for Grade 1-9. Ksh 1,500/term included in fees.

RULES: Reply in same language as parent (English/Swahili). Max 3 sentences. Never make up info. If you don't know the answer or it's outside what's listed above, say exactly: "I don't have that information — the school office will get back to you shortly." (or the Swahili equivalent: "Sina taarifa hiyo — ofisi ya shule itawasiliana nawe hivi karibuni.")
"""

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
    msg_lower = parent_message.lower()
    for kw in ESCALATION_KEYWORDS:
        if kw in msg_lower:
            return True
    if bot_reply:
        reply_lower = bot_reply.lower()
        for phrase in UNCERTAINTY_PHRASES:
            if phrase in reply_lower:
                return True
    return False

KEYWORD_RESPONSES = {
    "hello": "Hello! Welcome to Sally-Ann School. How can I help you today?",
    "hi": "Hi! Welcome to Sally-Ann School. Ask me about fees, bus fares, payments, trips or events.",
    "hujambo": "Habari! Karibu Sally-Ann School. Niulize kuhusu ada, basi au shughuli za shule.",
    "habari": "Nzuri! Karibu Sally-Ann School. Ninaweza kukusaidia na nini leo?",
    "fee": "2026 Fees: Grade 1&2 Ksh 15,500 + ICT Ksh 1,500 = Total Ksh 17,000/term. Min 60% on Reporting Day. No cash.",
    "ada": "Ada 2026: Darasa 1&2 Ksh 15,500 + ICT Ksh 1,500 = Ksh 17,000/muhula. Angalau 60% Siku ya Kuripoti.",
    "pay": "Payment: M-Pesa Paybill 777643 (ADM No), KCB 1135294917, Chai Sacco 1083225, Coop 01148786054900, Equity 0530291926992.",
    "mpesa": "M-Pesa Paybill: 777643. Account: Your child's ADM number. No cash accepted.",
    "bus": "5 bus routes: Kapkatet, Litein, Tebesonik, Chemosot, Mogogosiek. Reply with route name for fares.",
    "basi": "Njia 5 za basi: Kapkatet, Litein, Tebesonik, Chemosot, Mogogosiek. Andika jina la njia yako.",
    "kapkatet": "Kapkatet/month: Koitabai 2300, Daraja Sita 1950, Factory 1850, Town 1600, Chematich 1850, Kapkatolonyi 1250, Kaptote 1150, DC Jct 950.",
    "litein": "Litein/month: Town/St Kizitos 950, Factory Gate 1050, Kwa Soi/Joyland 1150, Imarisha 1150, Kusumek 1600.",
    "tebesonik": "Tebesonik/month: Lalagin 1250, Kiptewit Jct 1500, Cheborge 1600, Korongoi 1700, Bokoiyot/Factory 2300.",
    "chemosot": "Chemosot/month: Cheluget 1250, Chelilis/Chesingoro 1600, Kaminjeiwet/Getarwet Jct 1700.",
    "mogogosiek": "Mogogosiek/month: Murram 2600, Mogogosiek 2500, Boito Kaptien Rd 1850, Boito Shopping 1600, Chemoiben 1400, DC Residence 1050.",
    "trip": "Trips Term II: Grade 4 Maasai Mara 2500, Grade 5 Nakuru, Grade 6 Naivasha 3500, Grade 7 Nairobi 5000, Grade 8 Mombasa 15000.",
    "safari": "Safari Term II: Darasa 4 Maasai Mara 2500, Darasa 5 Nakuru, Darasa 6 Naivasha 3500, Darasa 7 Nairobi 5000, Darasa 8 Mombasa 15000.",
    "meeting": "Parental Days: Grade 5 May 16, Grade 4 May 23, Grade 3 May 30, Grade 2 Jun 6, Grade 1 Jun 13, PP1&PP2 Jun 20.",
    "ict": "ICT Digiskool (Coding, Robotics & AI) Grade 1-9. Ksh 1,500/term — included in fees.",
    "half term": "Half Term: 24th–28th June 2026. School resumes Monday 30th June.",
    "holiday": "Half Term: 24th–28th June 2026. School resumes Monday 30th June.",
    "likizo": "Likizo ya kati: Tarehe 24–28 Juni 2026. Shule inaendelea Jumatatu 30 Juni.",
    "thank": "You're welcome! Feel free to ask anything else. 😊",
    "thanks": "You're welcome! Feel free to ask anything else. 😊",
    "asante": "Karibu sana! Niulize swali lolote. 😊",
}

_last_inbound_time = {}

# ── Webhook deduplication ──────────────────────────────────────────────────────
# Meta retries webhook delivery if it doesn't get a fast enough 200 response,
# which can cause the same message to be processed (and replied to) multiple
# times. Each WhatsApp message has a unique id (WAMID) we can use to detect
# and skip duplicates. Kept small and capped so memory doesn't grow forever.
import collections
_seen_message_ids = collections.OrderedDict()
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
    response = groq_client.chat.completions.create(
        messages=messages, model="llama-3.3-70b-versatile",
        max_tokens=300, temperature=0.4,
    )
    return response.choices[0].message.content.strip()

def ask_gemini(user_message, history):
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}")
    gemini_history = []
    for msg in history:
        role = "user" if msg["role"] == "user" else "model"
        gemini_history.append({"role": role, "parts": [{"text": msg["content"]}]})
    gemini_history.append({"role": "user", "parts": [{"text": user_message}]})
    payload = {
        "system_instruction": {"parts": [{"text": SCHOOL_CONTEXT}]},
        "contents": gemini_history,
        "generationConfig": {"maxOutputTokens": 300, "temperature": 0.4},
    }
    r = requests.post(url, json=payload, timeout=10)
    r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()

def ask_ai(phone, message):
    history = db.get_history(phone)
    messages = [{"role": "system", "content": SCHOOL_CONTEXT}]
    messages.extend(history)
    messages.append({"role": "user", "content": message})
    reply = None
    try:
        reply = ask_groq(messages)
        logger.info(f"[{phone}] Groq OK")
    except Exception as e:
        logger.error(f"[{phone}] Groq error: {e}")
        try:
            reply = ask_gemini(message, history)
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
    import threading
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

        reply, use_ai = find_keyword_response(incoming)
        if use_ai:
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
        escalates = needs_escalation(incoming, reply)
        keyword_hit = next((kw for kw in ESCALATION_KEYWORDS if kw in incoming.lower()), None) if escalates else None
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
# ADMIN AUTH
# ══════════════════════════════════════════════════════════════════════════════
def admin_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated

# In-memory login attempt tracker: ip -> {"count": int, "locked_until": datetime|None}
# Resets on restart, which is acceptable here — the goal is to slow down a
# brute-force burst, not provide perfect protection. A genuinely persistent
# attacker still has to wait out the lockout window on every attempt.
_login_attempts = {}
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_LOCKOUT_MINUTES = 15

@app.route("/admin/login", methods=["POST"])
def admin_login():
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()
    now = datetime.now()
    rec = _login_attempts.get(ip)

    if rec and rec.get("locked_until") and now < rec["locked_until"]:
        wait_min = max(1, int((rec["locked_until"] - now).total_seconds() // 60) + 1)
        return jsonify({"error": f"Too many failed attempts. Try again in {wait_min} minute(s)."}), 429

    data = request.get_json() or {}
    submitted = data.get("password", "")

    if hmac.compare_digest(submitted.encode(), ADMIN_PASSWORD.encode()):
        _login_attempts.pop(ip, None)
        session["admin_logged_in"] = True
        session.permanent = True
        return jsonify({"success": True})

    rec = rec or {"count": 0, "locked_until": None}
    rec["count"] += 1
    if rec["count"] >= _LOGIN_MAX_ATTEMPTS:
        rec["locked_until"] = now + timedelta(minutes=_LOGIN_LOCKOUT_MINUTES)
        rec["count"] = 0
        logger.warning(f"Admin login locked out for IP {ip} after repeated failures")
    _login_attempts[ip] = rec
    return jsonify({"error": "Invalid password"}), 401

@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    session.clear()
    return jsonify({"success": True})

# ══════════════════════════════════════════════════════════════════════════════
# BOT CONTROLS
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/admin/bot/status")
@admin_required
def bot_status():
    return jsonify({"paused": db.is_bot_paused(), "takeovers": db.get_takeover_phones()})

@app.route("/admin/bot/toggle", methods=["POST"])
@admin_required
def toggle_bot():
    new_val = not db.is_bot_paused()
    db.set_bot_paused(new_val)
    return jsonify({"paused": new_val})

# ══════════════════════════════════════════════════════════════════════════════
# METRICS
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/admin/metrics")
@admin_required
def get_metrics():
    now = datetime.now()
    today_str     = now.strftime("%Y-%m-%d")
    yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")

    msgs_today     = db.get_daily_count(today_str, "inbound")
    msgs_yesterday = db.get_daily_count(yesterday_str, "inbound")
    if msgs_yesterday > 0:
        pct_change = round((msgs_today - msgs_yesterday) / msgs_yesterday * 100)
    else:
        pct_change = 100 if msgs_today > 0 else 0

    active_24h = db.count_active_since(24)

    bot_resp_today   = db.get_daily_count(today_str, "outbound_bot")
    admin_resp_today = db.get_daily_count(today_str, "outbound_admin")
    total_resp_today = bot_resp_today + admin_resp_today
    bot_pct = round(bot_resp_today / total_resp_today * 100) if total_resp_today > 0 else 100

    avg_resp = db.get_avg_response_time()

    activity = db.get_activity_items(500)
    pending = sum(1 for a in activity if get_conv_status(a["message"], a["direction"]) == "pending")

    faq_counts = db.get_faq_counts()
    topic_counts = {}
    for label, kws in TOPIC_GROUPS.items():
        topic_counts[label] = sum(faq_counts.get(k, 0) for k in kws)
    total_topic = sum(topic_counts.values())
    total_inbound_all = db.get_total_inbound_all_time()
    other = max(total_inbound_all - total_topic, 0)
    grand_total = total_topic + other
    topics = []
    for label, cnt in topic_counts.items():
        pct = round(cnt / grand_total * 100) if grand_total > 0 else 0
        topics.append([label, pct])
    other_pct = round(other / grand_total * 100) if grand_total > 0 else 0
    topics.append(["Other", other_pct])
    topics.sort(key=lambda x: -x[1])

    weekly = []
    for i in range(6, -1, -1):
        d = now - timedelta(days=i)
        ds = d.strftime("%Y-%m-%d")
        total_day = db.get_daily_total(ds)
        weekly.append([d.strftime("%a"), total_day])

    return jsonify({
        "messages_today":      msgs_today,
        "messages_pct_change": pct_change,
        "active_parents":      active_24h,
        "total_users":         db.count_total_users(),
        "bot_responses":       bot_resp_today,
        "bot_pct":             bot_pct,
        "human_replies":       admin_resp_today,
        "avg_response":        avg_resp,
        "pending_queries":     pending,
        "topics":              topics,
        "weekly":              weekly,
        "bot_paused":          db.is_bot_paused(),
        "admin_takeovers":     db.count_takeovers(),
        "broadcasts_sent":     db.count_broadcasts(),
        "total_messages":      db.total_message_count(),
        "unread":              db.count_unread(),
        "escalated_count":     db.count_escalated(),
    })

@app.route("/admin/escalations")
@admin_required
def get_escalations():
    return jsonify(db.get_escalated_conversations())

@app.route("/admin/escalations/history")
@admin_required
def get_escalation_history_route():
    return jsonify(db.get_escalation_history(200))

@app.route("/admin/activity")
@admin_required
def get_activity():
    items = db.get_activity_items(100)
    result = []
    for it in items:
        status = get_conv_status(it["message"], it["direction"])
        name = it.get("name") or it["phone"].replace("whatsapp:", "")
        result.append({
            "phone": it["phone"], "name": name,
            "message": it["message"], "status": status,
            "timestamp": it["timestamp"],
        })
    return jsonify(result)

# ══════════════════════════════════════════════════════════════════════════════
# MESSAGES / CONVERSATIONS
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/admin/messages")
@admin_required
def get_messages():
    limit = int(request.args.get("limit", 200))
    phone = request.args.get("phone")
    msgs = db.get_messages(limit=limit, phone=phone)
    if not phone:
        return jsonify(msgs)
    return jsonify(list(reversed(msgs)))

@app.route("/admin/conversations")
@admin_required
def get_conversations():
    """Lightweight conversation list — summary data only, no message content.
    Called on a polling timer so must be as cheap as possible. Full message
    history is loaded separately via /admin/conversations/<phone>/messages
    only when the user actually opens a specific conversation."""
    convs = db.get_all_conversations()
    # Only fetch the last 1 message per phone — just enough for the status
    # indicator and last-message preview in the list. Previously fetching
    # 50 messages per conversation and serializing all of them on every poll
    # was the primary cause of the 31KB payload and thread exhaustion.
    messages_by_phone = db.get_messages_for_phones(list(convs.keys()), per_phone_limit=1)
    result = {}
    for phone, c in convs.items():
        last_msgs = messages_by_phone.get(phone, [])
        last = last_msgs[-1] if last_msgs else None
        status = get_conv_status(last["message"], last["direction"]) if last else "pending"
        if c.get("escalated"):
            status = "escalated"
        result[phone] = {
            "phone": phone,
            "name": c.get("name"),
            "last_seen": c.get("last_seen"),
            "last_message": last["message"] if last else None,
            "last_direction": last["direction"] if last else None,
            "unread": db.count_unread_for_phone(phone),
            "admin_takeover": c.get("admin_takeover", False),
            "status": status,
            "escalated": c.get("escalated", False),
            "escalation_reason": c.get("escalation_reason"),
        }
    return jsonify(result)

@app.route("/admin/conversations/<path:phone>/messages")
@admin_required
def get_conversation_messages(phone):
    """Full message history for one conversation — only called when the admin
    actually opens that conversation, not on a polling timer."""
    msgs = db.get_messages(phone=phone)
    return jsonify(msgs)

@app.route("/admin/conversations/<path:phone>/takeover", methods=["POST"])
@admin_required
def takeover(phone):
    db.set_admin_takeover(phone, True)
    db.mark_messages_read(phone)
    db.clear_escalated(phone)
    # Also start the WhatsApp reply-by-phone session, so a plain WhatsApp
    # reply from the admin (no 4-digit code needed) routes to this parent.
    # Without this, dashboard takeover and WhatsApp-reply session were two
    # disconnected systems and admin replies from WhatsApp went nowhere.
    db.set_active_admin_session(phone)
    return jsonify({"success": True})

@app.route("/admin/conversations/<path:phone>/release", methods=["POST"])
@admin_required
def release(phone):
    db.set_admin_takeover(phone, False)
    # Only clear the active session if it's pointing at this same phone,
    # so releasing one conversation doesn't accidentally clear a session
    # the admin started for a different parent.
    if db.get_active_admin_session() == phone:
        db.clear_active_admin_session()
    return jsonify({"success": True})

@app.route("/admin/conversations/<path:phone>/send", methods=["POST"])
@admin_required
def admin_send(phone):
    data = request.get_json()
    message = data.get("message", "").strip()
    if not message:
        return jsonify({"error": "Message required"}), 400
    log_msg(phone, f"[ADMIN] {message}", "outbound", sender="admin")
    history = db.get_history(phone)
    history.append({"role": "assistant", "content": message})
    db.save_history(phone, history[-20:])
    send_whatsapp(phone, message)
    return jsonify({"success": True})

@app.route("/admin/conversations/<path:phone>/read", methods=["POST"])
@admin_required
def mark_read(phone):
    db.mark_messages_read(phone)
    return jsonify({"success": True})

# ══════════════════════════════════════════════════════════════════════════════
# BROADCAST / DIRECT SEND
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/admin/broadcast", methods=["POST"])
@admin_required
def broadcast():
    data    = request.get_json()
    message = data.get("message", "").strip()
    phones  = data.get("phones") or db.get_active_user_phones()
    if not message:
        return jsonify({"error": "Message required"}), 400
    if len(message) > 4096:
        return jsonify({"error": "Message too long (max 4096 characters — WhatsApp's own limit)"}), 400
    sent, failed = 0, 0
    for phone in phones:
        if send_whatsapp(phone, message):
            log_msg(phone, f"[BROADCAST] {message}", "outbound", sender="admin")
            sent += 1
        else:
            failed += 1
    db.save_broadcast(message, phones, sent, failed)
    return jsonify({"success": True, "sent": sent, "failed": failed})

@app.route("/admin/send-direct", methods=["POST"])
@admin_required
def send_direct():
    data    = request.get_json()
    phone   = data.get("phone", "").strip()
    message = data.get("message", "").strip()
    if not phone or not message:
        return jsonify({"error": "Phone and message required"}), 400
    if len(message) > 4096:
        return jsonify({"error": "Message too long (max 4096 characters — WhatsApp's own limit)"}), 400
    if not phone.startswith("whatsapp:") and not phone.startswith("+"):
        phone = "+" + phone
    if not phone.startswith("whatsapp:"):
        phone = "whatsapp:" + phone
    ok = send_whatsapp(phone, message)
    if ok:
        log_msg(phone, f"[DIRECT] {message}", "outbound", sender="admin")
        db.touch_active_user(phone)
    return jsonify({"success": ok, "phone": phone})

@app.route("/admin/broadcast/history")
@admin_required
def broadcast_history():
    return jsonify(db.get_broadcast_history())

@app.route("/admin/export/messages")
@admin_required
def export_messages_csv():
    import csv, io
    phone = request.args.get("phone")
    msgs = db.get_messages(limit=5000, phone=phone)
    if phone:
        msgs = list(reversed(msgs))
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["timestamp", "phone", "direction", "sender", "message"])
    for m in msgs:
        writer.writerow([m.get("timestamp", ""), m.get("phone", ""), m.get("direction", ""),
                          m.get("sender", ""), m.get("message", "")])
    csv_data = output.getvalue()
    filename = f"messages_{phone.replace('whatsapp:','') if phone else 'all'}.csv"
    return csv_data, 200, {
        "Content-Type": "text/csv",
        "Content-Disposition": f"attachment; filename={filename}",
    }

@app.route("/admin/export/escalations")
@admin_required
def export_escalations_csv():
    import csv, io
    history = db.get_escalation_history(2000)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["phone", "name", "reason", "escalated_at", "resolved_at", "resolved_by", "status", "resolution_minutes"])
    for h in history:
        writer.writerow([h.get("phone", ""), h.get("name") or "", h.get("reason", ""),
                          h.get("escalated_at", ""), h.get("resolved_at") or "",
                          h.get("resolved_by") or "", h.get("status", ""), h.get("resolution_minutes") or ""])
    csv_data = output.getvalue()
    return csv_data, 200, {
        "Content-Type": "text/csv",
        "Content-Disposition": "attachment; filename=escalation_history.csv",
    }

# ══════════════════════════════════════════════════════════════════════════════
# DATA RETENTION / DELETION (Data Protection Act compliance)
# ══════════════════════════════════════════════════════════════════════════════
# Nothing here runs automatically. These are manual tools for the school to
# use once they've decided on a retention policy, or to fulfil an individual
# parent's data access/deletion request. See database.py for the underlying
# functions and their docstrings.

@app.route("/admin/retention/preview", methods=["GET"])
@admin_required
def retention_preview():
    """Dry-run — shows what WOULD be deleted for the given inactivity
    threshold, without deleting anything. Call this before /delete-inactive."""
    days = request.args.get("days", type=int)
    if not days or days < 1:
        return jsonify({"error": "Provide a 'days' query parameter (positive integer)"}), 400
    result = db.preview_inactive_data(days)
    return jsonify(result)

@app.route("/admin/retention/delete-inactive", methods=["POST"])
@admin_required
def retention_delete_inactive():
    """Permanently deletes data for all conversations inactive longer than
    the given number of days. Requires explicit confirm:true in the request
    body — this is a destructive, irreversible action."""
    data = request.get_json() or {}
    days = data.get("days")
    confirm = data.get("confirm", False)
    if not days or not isinstance(days, int) or days < 1:
        return jsonify({"error": "Provide an integer 'days' value"}), 400
    if not confirm:
        return jsonify({"error": "Set confirm:true to proceed — this permanently deletes data"}), 400
    result = db.delete_inactive_data(days, confirm=True)
    logger.warning(f"Admin triggered retention deletion: {result}")
    return jsonify(result)

@app.route("/admin/data-request/export/<path:phone>")
@admin_required
def data_request_export(phone):
    """Export everything stored for one parent — for fulfilling a Data
    Protection Act access request, or to keep a record before deletion."""
    data = db.export_parent_data(phone)
    if data is None:
        return jsonify({"error": "Could not export data"}), 500
    return jsonify(data)

@app.route("/admin/data-request/delete/<path:phone>", methods=["POST"])
@admin_required
def data_request_delete(phone):
    """Permanently delete everything stored for one parent, on their
    explicit request. Requires confirm:true in the request body."""
    data = request.get_json() or {}
    if not data.get("confirm"):
        return jsonify({"error": "Set confirm:true to proceed — this permanently deletes data"}), 400
    ok = db.delete_parent_data(phone, confirm=True)
    if ok:
        logger.warning(f"Admin deleted all data for {phone} per data deletion request")
    return jsonify({"success": ok})

# ══════════════════════════════════════════════════════════════════════════════
# QUICK REPLIES / USERS
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/admin/quick-replies", methods=["GET"])
@admin_required
def get_quick_replies():
    return jsonify(db.get_quick_replies())

@app.route("/admin/quick-replies", methods=["POST"])
@admin_required
def add_quick_reply():
    data = request.get_json()
    db.add_quick_reply(data.get("title"), data.get("body"))
    return jsonify({"success": True})

@app.route("/admin/quick-replies/<int:qid>", methods=["DELETE"])
@admin_required
def del_quick_reply(qid):
    db.delete_quick_reply(qid)
    return jsonify({"success": True})

@app.route("/admin/users")
@admin_required
def get_users():
    phones = db.get_active_user_phones()
    return jsonify([{"phone": p} for p in phones])

# ══════════════════════════════════════════════════════════════════════════════
# ADMIN DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/admin")
@app.route("/admin/")
def admin_dashboard():
    return render_template_string(DASHBOARD_HTML)

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Sally-Ann School — Admin</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Source+Serif+4:opsz,wght@8..60,400;8..60,600;8..60,700&family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#FAF6EF;--s1:#FFFFFF;--s2:#F3ECDF;--s3:#EAE0CC;
  --border:#E0D5BC;--border2:#CBBB95;
  --ink:#22281F;--ink2:#5C6354;--ink3:#8C9180;
  --forest:#2D6A4F;--forest-bg:#E3EEE6;--forest-border:#BFDBC9;
  --terracotta:#C4622D;--terra-bg:#F6E4D8;--terra-border:#E8BD9C;
  --gold:#B7872E;--gold-bg:#F5EAD3;--gold-border:#E5CD96;
  --slate:#4A6376;--slate-bg:#E6EDF1;--slate-border:#C2D2DC;
  --font-display:'Source Serif 4',Georgia,serif;
  --font-body:'Inter',system-ui,-apple-system,sans-serif;
  --r:6px;--r2:10px;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--ink);font-family:var(--font-body);min-height:100vh}
::-webkit-scrollbar{width:6px;height:6px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--border2);border-radius:3px}

/* ── LOGIN ── */
#login{display:flex;align-items:center;justify-content:center;min-height:100vh;background:var(--bg)}
.lc{width:380px}
.lc-logo{display:flex;align-items:center;gap:16px;margin-bottom:34px}
.lc-emblem{width:54px;height:54px;background:var(--forest-bg);border:1px solid var(--forest-border);
  border-radius:10px;display:flex;align-items:center;justify-content:center;font-family:var(--font-display);
  font-size:22px;font-weight:700;color:var(--forest)}
.lc-name{font-family:var(--font-display);font-size:19px;font-weight:600}
.lc-sub{font-size:11px;color:var(--ink3);margin-top:3px;letter-spacing:1.5px;text-transform:uppercase}
.lc-label{font-size:11px;color:var(--ink3);margin-bottom:8px;text-transform:uppercase;letter-spacing:.7px;font-weight:600}
.lc-input{width:100%;padding:13px 16px;background:var(--s1);border:1px solid var(--border);
  border-radius:var(--r);color:var(--ink);font-size:15px;outline:none;transition:border .2s;margin-bottom:13px}
.lc-input:focus{border-color:var(--forest)}
.lc-btn{width:100%;padding:13px;background:var(--forest);color:#fff;border:none;
  border-radius:var(--r);font-size:15px;font-weight:600;cursor:pointer}
.lc-btn:hover{background:#255A42}
.lc-err{color:var(--terracotta);font-size:12px;margin-top:10px;min-height:18px}

/* ── SHELL ── */
#app{display:none;flex-direction:column;min-height:100vh}

/* ── HEADER ── */
.header{background:var(--s1);border-bottom:1px solid var(--border);padding:18px 28px}
.header-row{display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:13px}
.hdr-left{display:flex;align-items:center;gap:15px}
.hdr-emblem{width:44px;height:44px;background:var(--forest-bg);border:1px solid var(--forest-border);
  border-radius:9px;display:flex;align-items:center;justify-content:center;font-family:var(--font-display);
  font-size:18px;font-weight:700;color:var(--forest);flex-shrink:0}
.hdr-title{font-family:var(--font-display);font-size:19px;font-weight:600}
.hdr-sub{font-size:11px;color:var(--ink3);margin-top:2px;letter-spacing:.3px}
.hdr-right{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.bot-pill{display:flex;align-items:center;gap:7px;padding:7px 14px;border-radius:18px;
  font-size:12px;font-weight:600;border:1px solid}
.bot-pill.on{background:var(--forest-bg);border-color:var(--forest-border);color:var(--forest)}
.bot-pill.off{background:var(--terra-bg);border-color:var(--terra-border);color:var(--terracotta)}
.bot-pill-dot{width:7px;height:7px;border-radius:50%}
.bot-pill.on .bot-pill-dot{background:var(--forest);animation:blink 1.4s infinite}
.bot-pill.off .bot-pill-dot{background:var(--terracotta)}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}
.pause-btn{padding:7px 16px;border-radius:7px;font-size:12px;font-weight:600;cursor:pointer;
  border:1px solid var(--border2);background:transparent;color:var(--ink2);transition:all .15s}
.pause-btn:hover{border-color:var(--gold);color:var(--gold)}
.pause-btn.resume{background:var(--forest);border-color:var(--forest);color:#fff}
.signout-btn{padding:7px 14px;background:transparent;border:1px solid var(--border2);
  color:var(--ink2);border-radius:7px;cursor:pointer;font-size:12px}
.signout-btn:hover{border-color:var(--terracotta);color:var(--terracotta)}
.unread-pill{background:var(--terracotta);color:#fff;border-radius:18px;padding:5px 11px;
  font-size:11px;font-weight:700;display:none}

/* ── TABS ── */
.tabbar{display:flex;gap:3px;padding:0 28px;background:var(--s1);border-bottom:1px solid var(--border);overflow-x:auto}
.tab{padding:13px 18px;font-size:13px;font-weight:600;color:var(--ink3);cursor:pointer;
  border-bottom:2px solid transparent;transition:all .15s;white-space:nowrap;display:flex;align-items:center;gap:6px}
.tab:hover{color:var(--ink)}
.tab.active{color:var(--forest);border-bottom-color:var(--forest)}
.tab-badge{background:var(--terracotta);color:#fff;border-radius:18px;padding:1px 7px;font-size:10px;font-weight:700;display:none}
.tab-badge.esc-badge{animation:escPulse 1.3s infinite}
@keyframes escPulse{0%,100%{box-shadow:0 0 0 0 rgba(196,98,45,.4)}50%{box-shadow:0 0 0 5px rgba(196,98,45,0)}}

/* ── ESCALATION BANNER ── */
.escalation-banner{display:none;background:var(--terra-bg);border-bottom:1px solid var(--terra-border)}
.escalation-banner.show{display:block}
.esc-banner-inner{padding:10px 28px;display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.esc-banner-icon{font-size:15px;animation:escPulse 1.3s infinite;flex-shrink:0;color:var(--terracotta)}
.esc-banner-text{font-size:12.5px;color:var(--terracotta);font-weight:600;flex:1;min-width:200px}
.esc-banner-items{display:flex;gap:8px;flex-wrap:wrap}
.esc-chip{display:flex;align-items:center;gap:8px;background:var(--s1);border:1px solid var(--terra-border);
  border-radius:18px;padding:5px 8px 5px 12px;font-size:11px;cursor:pointer;transition:all .15s}
.esc-chip:hover{background:var(--s2)}
.esc-chip-name{font-weight:600;color:var(--ink)}
.esc-chip-btn{background:var(--terracotta);color:#fff;border:none;border-radius:13px;padding:3px 10px;
  font-size:10px;font-weight:600;cursor:pointer}
.esc-chip-btn:hover{opacity:.85}

/* ── CONTENT ── */
.content{flex:1;padding:24px 28px}
.pg{display:none}.pg.active{display:block}

/* ── METRICS ── */
.metrics-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(165px,1fr));gap:13px;margin-bottom:20px}
.mc{background:var(--s1);border:1px solid var(--border);border-radius:var(--r2);padding:17px 18px;position:relative}
.mc-top{position:absolute;top:0;left:0;right:0;height:3px;border-radius:var(--r2) var(--r2) 0 0}
.mc-top.forest{background:var(--forest)}
.mc-top.slate{background:var(--slate)}
.mc-top.gold{background:var(--gold)}
.mc-top.terra{background:var(--terracotta)}
.mc-label{font-size:11px;color:var(--ink3);font-weight:600;margin-bottom:8px}
.mc-val{font-family:var(--font-display);font-size:29px;font-weight:600;line-height:1;color:var(--ink)}
.mc-foot{font-size:11px;color:var(--ink3);margin-top:7px;display:flex;align-items:center;gap:5px}
.mc-foot.up{color:var(--forest)}
.mc-foot.down{color:var(--terracotta)}

/* ── CARDS ── */
.card{background:var(--s1);border:1px solid var(--border);border-radius:var(--r2);padding:20px;margin-bottom:16px}
.ch{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;flex-wrap:wrap;gap:8px}
.ct{font-family:var(--font-display);font-size:15px;font-weight:600;color:var(--ink)}
.two-col{display:grid;grid-template-columns:1.1fr .9fr;gap:16px}
@media(max-width:900px){.two-col{grid-template-columns:1fr}}

/* ── TOPIC BARS ── */
.topic-row{margin-bottom:14px}
.topic-head{display:flex;justify-content:space-between;font-size:12.5px;margin-bottom:6px}
.topic-name{font-weight:500;color:var(--ink2)}
.topic-pct{font-weight:700;color:var(--ink)}
.topic-bar-bg{height:7px;background:var(--s2);border-radius:4px;overflow:hidden}
.topic-bar-fill{height:100%;border-radius:4px;transition:width .7s ease}

/* ── WEEKLY CHART ── */
.week-chart{display:flex;align-items:flex-end;gap:10px;height:140px;padding-top:10px}
.week-col{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;gap:8px;height:100%}
.week-bar{width:100%;max-width:36px;background:var(--forest);border-radius:4px 4px 0 0;min-height:4px;transition:height .6s ease}
.week-bar-val{font-size:10px;color:var(--ink3);font-weight:600}
.week-lbl{font-size:11px;color:var(--ink3);font-weight:500}

/* ── ACTIVITY LIST ── */
.activity-list{display:flex;flex-direction:column;gap:2px}
.act-item{display:flex;align-items:flex-start;gap:12px;padding:13px 4px;border-bottom:1px solid var(--border)}
.act-item:last-child{border-bottom:none}
.avatar{width:38px;height:38px;border-radius:50%;display:flex;align-items:center;justify-content:center;
  font-family:var(--font-display);font-size:13px;font-weight:600;flex-shrink:0;color:#fff}
.act-body{flex:1;min-width:0}
.act-name{font-size:13px;font-weight:600;margin-bottom:2px;color:var(--ink)}
.act-msg{font-size:12px;color:var(--ink3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:100%}
.act-right{display:flex;flex-direction:column;align-items:flex-end;gap:5px;flex-shrink:0}
.act-time{font-size:10px;color:var(--ink3)}
.status-badge{font-size:10px;font-weight:700;padding:3px 10px;border-radius:18px;text-transform:capitalize}
.status-pending{background:var(--gold-bg);color:var(--gold)}
.status-resolved{background:var(--forest-bg);color:var(--forest)}
.status-override{background:var(--slate-bg);color:var(--slate)}
.status-escalated{background:var(--terra-bg);color:var(--terracotta)}

/* ── MESSAGES / CONVERSATIONS ── */
.conv-wrap{display:grid;grid-template-columns:290px 1fr;border:1px solid var(--border);
  border-radius:var(--r2);overflow:hidden;height:calc(100vh - 230px);background:var(--s1)}
.conv-left{background:var(--s1);border-right:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden}
.conv-search{padding:10px;border-bottom:1px solid var(--border);display:flex;gap:6px}
.conv-search input{flex:1;padding:8px 12px;background:var(--s2);border:1px solid var(--border);
  border-radius:7px;color:var(--ink);font-size:12px;outline:none}
.conv-search input:focus{border-color:var(--forest)}
.conv-export-btn{padding:8px 10px;background:var(--s2);border:1px solid var(--border);border-radius:7px;
  cursor:pointer;font-size:11px;color:var(--ink2);flex-shrink:0}
.conv-export-btn:hover{border-color:var(--forest);color:var(--forest)}
.conv-scroll{flex:1;overflow-y:auto}
.cv-item{padding:12px 14px;border-bottom:1px solid var(--border);cursor:pointer;transition:background .12s;position:relative}
.cv-item:hover{background:var(--s2)}
.cv-item.active{background:var(--forest-bg);border-left:2px solid var(--forest)}
.cv-item.cv-escalated{background:var(--terra-bg);border-left:2px solid var(--terracotta)}
.cv-item.cv-escalated.active{background:#F0D5C2}
.cv-header{display:flex;justify-content:space-between;margin-bottom:3px}
.cv-name{font-size:12px;font-weight:600;color:var(--ink)}
.cv-time{font-size:9px;color:var(--ink3)}
.cv-preview{font-size:11px;color:var(--ink3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;display:flex;align-items:center;gap:4px}
.cv-tags{display:flex;gap:5px;margin-top:6px;flex-wrap:wrap}
.tag{display:inline-flex;align-items:center;gap:3px;padding:2px 7px;border-radius:18px;font-size:9px;font-weight:700}
.tag-green{background:var(--forest-bg);color:var(--forest)}
.tag-amber{background:var(--gold-bg);color:var(--gold)}
.tag-blue{background:var(--slate-bg);color:var(--slate)}
.tag-escalated{background:var(--terra-bg);color:var(--terracotta)}
.cv-unread{position:absolute;right:12px;top:14px;background:var(--forest);color:#fff;
  width:18px;height:18px;border-radius:50%;font-size:9px;font-weight:700;display:flex;align-items:center;justify-content:center}
.media-dot{width:6px;height:6px;border-radius:50%;background:var(--slate);flex-shrink:0}

.chat-right{display:flex;flex-direction:column;background:var(--bg);overflow:hidden}
.chat-head{padding:12px 16px;background:var(--s1);border-bottom:1px solid var(--border);
  display:flex;justify-content:space-between;align-items:center;flex-shrink:0;flex-wrap:wrap;gap:8px}
.chat-head-info .cname{font-size:14px;font-weight:600;color:var(--ink)}
.chat-head-info .cstatus{font-size:10px;color:var(--ink3);margin-top:2px}
.chat-msgs{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:5px}
.msg-wrap{display:flex;flex-direction:column}
.bubble{max-width:75%;padding:9px 13px;border-radius:11px;font-size:12.5px;line-height:1.55;word-break:break-word}
.bubble.in{background:var(--s1);border:1px solid var(--border);align-self:flex-start;border-bottom-left-radius:3px}
.bubble.out{background:var(--forest-bg);border:1px solid var(--forest-border);align-self:flex-end;border-bottom-right-radius:3px}
.bubble.admin{background:var(--gold-bg);border:1px solid var(--gold-border);align-self:flex-end;border-bottom-right-radius:3px}
.msg-ts{font-size:9px;color:var(--ink3);margin-top:2px}
.msg-ts.r{align-self:flex-end}
.chat-takeover-notice{padding:8px 16px;background:var(--gold-bg);border-top:1px solid var(--gold-border);
  font-size:11px;color:var(--gold);text-align:center;flex-shrink:0}
.chat-escalation-notice{padding:9px 16px;background:var(--terra-bg);border-bottom:1px solid var(--terra-border);
  font-size:11.5px;color:var(--terracotta);font-weight:600;flex-shrink:0}
.chat-foot{padding:10px 14px;background:var(--s1);border-top:1px solid var(--border);flex-shrink:0}
.qr-bar{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px}
.qr-btn{padding:4px 10px;background:var(--s2);border:1px solid var(--border);
  color:var(--ink2);border-radius:6px;font-size:10px;cursor:pointer;transition:all .15s}
.qr-btn:hover{border-color:var(--forest);color:var(--forest)}
.chat-input-row{display:flex;gap:8px;align-items:flex-end}
.chat-ta{flex:1;padding:9px 13px;background:var(--s2);border:1px solid var(--border);
  border-radius:8px;color:var(--ink);font-size:13px;outline:none;resize:none;max-height:100px;line-height:1.45}
.chat-ta:focus{border-color:var(--forest)}
.no-chat{display:flex;flex-direction:column;align-items:center;justify-content:center;flex:1;color:var(--ink3);gap:10px}
.no-chat-icon{font-size:40px;opacity:.3}
.chat-bot-foot{padding:12px 16px;background:var(--s1);border-top:1px solid var(--border);
  display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;flex-shrink:0}
.chat-bot-note{font-size:11px;color:var(--ink3)}

/* ── TABLE ── */
.tbl-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:12px}
th{text-align:left;padding:9px 14px;color:var(--ink3);font-size:10px;text-transform:uppercase;
  letter-spacing:.4px;border-bottom:1px solid var(--border);white-space:nowrap}
td{padding:10px 14px;border-bottom:1px solid var(--border);vertical-align:middle;color:var(--ink2)}
tr:last-child td{border-bottom:none}
tr:hover td{background:var(--s2)}

/* ── BROADCAST ── */
.bc-compose{padding:16px;background:var(--s2);border-radius:var(--r);margin-bottom:14px}
.bc-ta{width:100%;padding:12px 14px;background:var(--s1);border:1px solid var(--border);
  border-radius:8px;color:var(--ink);font-size:13px;outline:none;resize:vertical;min-height:100px;line-height:1.5}
.bc-ta:focus{border-color:var(--forest)}
.bc-meta{display:flex;justify-content:space-between;align-items:center;margin-top:8px;flex-wrap:wrap;gap:6px}
.bc-count{font-size:11px;color:var(--ink3)}
.bc-actions{display:flex;gap:8px;margin-top:12px;align-items:center;flex-wrap:wrap}
.bc-status{font-size:12px;color:var(--ink3)}
.template-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:10px;margin-bottom:14px}
.tpl-card{background:var(--s2);border:1px solid var(--border);border-radius:var(--r);
  padding:14px;cursor:pointer;transition:all .15s;position:relative}
.tpl-card:hover{border-color:var(--forest);background:var(--forest-bg)}
.tpl-title{font-size:12px;font-weight:600;color:var(--forest);margin-bottom:6px}
.tpl-body{font-size:11px;color:var(--ink2);line-height:1.5;max-height:55px;overflow:hidden}
.tpl-hint{font-size:9px;color:var(--forest);margin-top:8px;opacity:.8}
.tpl-del{position:absolute;top:8px;right:8px;background:var(--terra-bg);border:none;
  color:var(--terracotta);width:20px;height:20px;border-radius:50%;cursor:pointer;font-size:11px;
  display:flex;align-items:center;justify-content:center}
.tpl-del:hover{background:var(--terra-border)}
.finput{width:100%;padding:10px 13px;background:var(--s2);border:1px solid var(--border);
  border-radius:8px;color:var(--ink);font-size:13px;outline:none}
.finput:focus{border-color:var(--forest)}

/* ── BOT CONTROLS ── */
.toggle-card{display:flex;justify-content:space-between;align-items:center;padding:22px;
  background:var(--s2);border-radius:var(--r2);border:1px solid var(--border);margin-bottom:16px;flex-wrap:wrap;gap:14px}
.toggle-info .tc-title{font-family:var(--font-display);font-size:15px;font-weight:600;margin-bottom:4px;color:var(--ink)}
.toggle-info .tc-sub{font-size:12px;color:var(--ink2);max-width:420px}
.switch{position:relative;width:54px;height:29px;flex-shrink:0}
.switch input{opacity:0;width:0;height:0}
.slider{position:absolute;cursor:pointer;inset:0;background:var(--s3);border:1px solid var(--border);
  border-radius:30px;transition:.25s}
.slider::before{content:'';position:absolute;height:21px;width:21px;left:3px;bottom:3px;
  background:#fff;border-radius:50%;transition:.25s;box-shadow:0 1px 2px rgba(0,0,0,.15)}
input:checked + .slider{background:var(--forest-bg);border-color:var(--forest)}
input:checked + .slider::before{transform:translateX(23px);background:var(--forest)}
.takeover-list{display:flex;flex-direction:column;gap:8px}
.takeover-item{display:flex;justify-content:space-between;align-items:center;padding:12px 14px;
  background:var(--s2);border:1px solid var(--border);border-radius:8px}
.takeover-phone{font-size:13px;font-weight:600;color:var(--ink)}
.takeover-meta{font-size:11px;color:var(--ink3);margin-top:2px}

/* ── ESCALATION HISTORY ── */
.esc-hist-row td{font-size:11.5px}
.esc-status-open{color:var(--terracotta);font-weight:700}
.esc-status-resolved{color:var(--forest);font-weight:700}

/* ── BUTTONS ── */
.btn{padding:8px 16px;border-radius:8px;font-size:12px;font-weight:600;cursor:pointer;
  border:none;transition:all .15s;display:inline-flex;align-items:center;gap:5px}
.btn:hover{opacity:.88}.btn:active{transform:scale(.97)}
.btn-green{background:var(--forest);color:#fff}
.btn-amber{background:var(--gold);color:#fff}
.btn-red{background:var(--terracotta);color:#fff}
.btn-ghost{background:var(--s2);color:var(--ink);border:1px solid var(--border)}
.btn-ghost:hover{border-color:var(--forest);color:var(--forest)}
.btn-sm{padding:5px 11px;font-size:11px}

/* ── MODAL ── */
.modal-ov{display:none;position:fixed;inset:0;background:rgba(34,40,31,.45);z-index:1000;
  align-items:center;justify-content:center}
.modal-ov.open{display:flex}
.modal{background:var(--s1);border:1px solid var(--border2);border-radius:14px;padding:26px;
  width:460px;max-width:95vw}
.modal-title{font-family:var(--font-display);font-size:16px;font-weight:600;margin-bottom:20px;color:var(--ink)}
.fg{margin-bottom:15px}
.flabel{font-size:10px;color:var(--ink3);text-transform:uppercase;letter-spacing:.6px;margin-bottom:6px;display:block}
.fta{width:100%;padding:10px 13px;background:var(--s2);border:1px solid var(--border);
  border-radius:8px;color:var(--ink);font-size:13px;outline:none;resize:vertical;min-height:80px}
.fta:focus{border-color:var(--forest)}
.modal-acts{display:flex;gap:9px;justify-content:flex-end;margin-top:18px}

/* ── TOAST ── */
.toast{position:fixed;bottom:22px;right:22px;padding:11px 18px;border-radius:9px;
  font-size:12px;font-weight:600;z-index:9999;pointer-events:none;
  transform:translateY(60px);opacity:0;transition:all .28s ease;border:1px solid;max-width:320px}
.toast.show{transform:translateY(0);opacity:1}
.toast.ok{background:var(--forest-bg);border-color:var(--forest-border);color:var(--forest)}
.toast.err{background:var(--terra-bg);border-color:var(--terra-border);color:var(--terracotta)}
.toast.info{background:var(--slate-bg);border-color:var(--slate-border);color:var(--slate)}

.empty-state{text-align:center;padding:40px;color:var(--ink3)}
.empty-state .ei{font-size:32px;margin-bottom:10px;opacity:.35}
.empty-state .et{font-size:12px}
</style>
</head>
<body>

<div id="login">
  <div class="lc">
    <div class="lc-logo">
      <div class="lc-emblem">SA</div>
      <div><div class="lc-name">Sally-Ann School</div><div class="lc-sub">Admin office</div></div>
    </div>
    <div class="lc-label">Admin password</div>
    <input class="lc-input" type="password" id="pw" placeholder="Enter password" onkeydown="if(event.key==='Enter')login()">
    <button class="lc-btn" onclick="login()">Open dashboard</button>
    <div class="lc-err" id="lerr"></div>
  </div>
</div>

<div id="app">
  <div class="header">
    <div class="header-row">
      <div class="hdr-left">
        <div class="hdr-emblem">SA</div>
        <div>
          <div class="hdr-title">Sally-Ann School</div>
          <div class="hdr-sub">WhatsApp bot admin</div>
        </div>
      </div>
      <div class="hdr-right">
        <div class="unread-pill" id="unread-pill">0 unread</div>
        <div class="bot-pill on" id="bot-pill"><div class="bot-pill-dot"></div><span id="bot-pill-text">Bot online</span></div>
        <button class="pause-btn" id="pause-btn" onclick="toggleBot()">Pause bot</button>
        <button class="signout-btn" onclick="logout()">Sign out</button>
      </div>
    </div>
  </div>

  <div class="tabbar">
    <div class="tab active" onclick="showPg('overview',this)">Overview</div>
    <div class="tab" onclick="showPg('messages',this)">Messages <span class="tab-badge" id="tab-badge">0</span></div>
    <div class="tab" onclick="showPg('broadcast',this)">Broadcast</div>
    <div class="tab" onclick="showPg('botcontrols',this)">Bot controls</div>
    <div class="tab" onclick="showPg('escalations',this)">Escalation history</div>
    <div class="tab" onclick="showPg('activity',this)">Activity log</div>
  </div>

  <div class="escalation-banner" id="escalation-banner">
    <div class="esc-banner-inner" id="escalation-banner-inner"></div>
  </div>

  <div class="content">

    <div class="pg active" id="pg-overview">
      <div class="metrics-grid">
        <div class="mc"><div class="mc-top forest"></div><div class="mc-label">Messages today</div><div class="mc-val" id="m-msgs-today">0</div><div class="mc-foot" id="m-msgs-change">— vs yesterday</div></div>
        <div class="mc"><div class="mc-top slate"></div><div class="mc-label">Active parents</div><div class="mc-val" id="m-active">0</div><div class="mc-foot">Last 24 hours</div></div>
        <div class="mc"><div class="mc-top forest"></div><div class="mc-label">Bot responses</div><div class="mc-val" id="m-bot-resp">0</div><div class="mc-foot" id="m-bot-pct">0% handled by bot</div></div>
        <div class="mc"><div class="mc-top slate"></div><div class="mc-label">Human replies</div><div class="mc-val" id="m-human">0</div><div class="mc-foot">Admin overrides</div></div>
        <div class="mc"><div class="mc-top slate"></div><div class="mc-label">Avg. response</div><div class="mc-val" id="m-avgresp">0s</div><div class="mc-foot">Bot response time</div></div>
        <div class="mc"><div class="mc-top gold"></div><div class="mc-label">Pending queries</div><div class="mc-val" id="m-pending">0</div><div class="mc-foot">Need attention</div></div>
      </div>

      <div class="two-col">
        <div class="card">
          <div class="ch"><span class="ct">Top query topics</span></div>
          <div id="topics-list"><div class="empty-state"><div class="ei">—</div><div class="et">No data yet</div></div></div>
        </div>
        <div class="card">
          <div class="ch"><span class="ct">Message volume — last 7 days</span></div>
          <div class="week-chart" id="week-chart"></div>
        </div>
      </div>

      <div class="card">
        <div class="ch"><span class="ct">Recent activity</span><button class="btn btn-ghost btn-sm" onclick="loadOverview()">Refresh</button></div>
        <div class="activity-list" id="recent-activity">
          <div class="empty-state"><div class="ei">—</div><div class="et">No activity yet</div></div>
        </div>
      </div>
    </div>

    <div class="pg" id="pg-messages">
      <div class="conv-wrap">
        <div class="conv-left">
          <div class="conv-search">
            <input placeholder="Search by name or phone..." id="conv-q" oninput="filterConvs(this.value)">
            <button class="conv-export-btn" onclick="exportAllMessages()" title="Export all messages as CSV">Export</button>
          </div>
          <div class="conv-scroll" id="conv-scroll"></div>
        </div>
        <div class="chat-right" id="chat-right">
          <div class="no-chat"><div class="no-chat-icon">—</div><span style="font-size:13px">Select a conversation</span></div>
        </div>
      </div>
    </div>

    <div class="pg" id="pg-broadcast">
      <div class="card">
        <div class="ch"><span class="ct">Send to a specific number</span></div>
        <div class="bc-compose">
          <div style="margin-bottom:10px">
            <div class="flabel" style="margin-bottom:6px">Phone number</div>
            <input id="direct-phone" class="finput" placeholder="e.g. 0712345678 or +254712345678">
          </div>
          <textarea class="bc-ta" id="direct-msg" placeholder="Type your message..." style="min-height:80px"></textarea>
        </div>
        <div class="bc-actions">
          <button class="btn btn-green" onclick="sendDirect()">Send message</button>
          <span class="bc-status" id="direct-status"></span>
        </div>
      </div>

      <div class="card">
        <div class="ch"><span class="ct">Compose broadcast</span></div>
        <div class="bc-compose">
          <textarea class="bc-ta" id="bc-msg" placeholder="Type your message to parents here...

Example:
Dear Parent, please note that school fees for Term III 2026 are now due. Total: Ksh 17,000. Pay via M-Pesa Paybill 777643, Account: ADM number."></textarea>
          <div class="bc-meta">
            <span class="bc-count">Will send to <strong id="bc-count" style="color:var(--ink)">0</strong> parents</span>
            <span style="font-size:10px;color:var(--ink3)">Shift+Enter for new line</span>
          </div>
        </div>
        <div class="bc-actions">
          <button class="btn btn-ghost" onclick="bcPreview()">Preview</button>
          <button class="btn btn-green" onclick="bcSend()">Send to all parents</button>
          <span class="bc-status" id="bc-status"></span>
        </div>
      </div>

      <div class="card">
        <div class="ch"><span class="ct">Quick templates</span><button class="btn btn-ghost btn-sm" onclick="openTplModal()">Add</button></div>
        <div class="template-grid" id="tpl-bc"></div>
      </div>

      <div class="card">
        <div class="ch"><span class="ct">Broadcast history</span></div>
        <div class="tbl-wrap"><table>
          <thead><tr><th>Sent at</th><th>Message</th><th>Recipients</th><th>Delivered</th><th>Failed</th></tr></thead>
          <tbody id="bc-hist-tbody"></tbody>
        </table></div>
      </div>
    </div>

    <div class="pg" id="pg-botcontrols">
      <div class="toggle-card">
        <div class="toggle-info">
          <div class="tc-title" id="bc-toggle-title">Bot is online</div>
          <div class="tc-sub" id="bc-toggle-sub">The bot is automatically replying to all new parent messages. Turn this off to pause all automatic replies — new messages will wait for a human reply.</div>
        </div>
        <label class="switch">
          <input type="checkbox" id="bot-switch" checked onchange="toggleBot()">
          <span class="slider"></span>
        </label>
      </div>

      <div class="card">
        <div class="ch"><span class="ct">Quick reply templates</span><button class="btn btn-ghost btn-sm" onclick="openTplModal()">Add</button></div>
        <div class="template-grid" id="tpl-settings"></div>
      </div>

      <div class="card">
        <div class="ch"><span class="ct">Active admin takeovers</span></div>
        <div class="takeover-list" id="takeover-list">
          <div class="empty-state"><div class="ei">—</div><div class="et">No conversations currently overridden by admin</div></div>
        </div>
      </div>
    </div>

    <div class="pg" id="pg-escalations">
      <div class="card">
        <div class="ch">
          <span class="ct">Escalation history</span>
          <button class="btn btn-ghost btn-sm" onclick="exportEscalations()">Export CSV</button>
        </div>
        <div class="tbl-wrap"><table>
          <thead><tr><th>Parent</th><th>Reason</th><th>Escalated</th><th>Resolved</th><th>Time to resolve</th><th>Status</th></tr></thead>
          <tbody id="esc-hist-tbody"></tbody>
        </table></div>
      </div>
    </div>

    <div class="pg" id="pg-activity">
      <div class="card">
        <div class="ch"><span class="ct">Full activity log</span><button class="btn btn-ghost btn-sm" onclick="loadActivityLog()">Refresh</button></div>
        <div class="activity-list" id="activity-log-list">
          <div class="empty-state"><div class="ei">—</div><div class="et">No activity yet</div></div>
        </div>
      </div>
    </div>

  </div>
</div>

<div class="modal-ov" id="tpl-modal">
  <div class="modal">
    <div class="modal-title">Add template</div>
    <div class="fg"><label class="flabel">Title</label><input class="finput" id="tpl-title" placeholder="e.g. Fee reminder"></div>
    <div class="fg"><label class="flabel">Message body</label><textarea class="fta" id="tpl-body" rows="5" placeholder="Type the message template..."></textarea></div>
    <div class="modal-acts">
      <button class="btn btn-ghost" onclick="closeModal('tpl-modal')">Cancel</button>
      <button class="btn btn-green" onclick="saveTpl()">Save template</button>
    </div>
  </div>
</div>

<div class="modal-ov" id="prev-modal">
  <div class="modal">
    <div class="modal-title">Broadcast preview</div>
    <div style="background:var(--s2);border-radius:8px;padding:15px;font-size:13px;line-height:1.6;
      white-space:pre-wrap;max-height:220px;overflow-y:auto;border:1px solid var(--border);margin-bottom:14px" id="prev-body"></div>
    <div style="font-size:12px;color:var(--ink3);margin-bottom:16px">Will be sent to <strong style="color:var(--ink)" id="prev-count">0</strong> parents</div>
    <div class="modal-acts">
      <button class="btn btn-ghost" onclick="closeModal('prev-modal')">Cancel</button>
      <button class="btn btn-green" onclick="bcConfirm()">Confirm & send</button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
'use strict';
const API='';
let convData={}, selPhone=null, allTpl=[], convTimer=null, overviewTimer=null, escalationTimer=null;
const AVATAR_COLORS=['#2D6A4F','#4A6376','#B7872E','#C4622D','#6B7C56','#7A6A8C'];

async function login(){
  const pw=document.getElementById('pw').value;
  const r=await fetch(API+'/admin/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:pw})});
  if(r.ok){
    document.getElementById('login').style.display='none';
    document.getElementById('app').style.display='flex';
    boot();
  } else {
    document.getElementById('lerr').textContent='Incorrect password. Please try again.';
  }
}
async function logout(){await fetch(API+'/admin/logout',{method:'POST'});location.reload();}

function boot(){
  loadOverview();
  loadTpl();
  loadBotStatus();
  loadEscalations();
  // Polling intervals deliberately conservative — this runs on a 0.1 vCPU
  // instance, and aggressive polling (previously 6-8s) was competing with
  // the health check endpoint and WhatsApp webhook processing for the
  // worker pool's limited threads, causing the instance to fail health
  // checks and crash-loop under Koyeb's auto-recovery.
  overviewTimer=setInterval(loadOverview,20000);
  escalationTimer=setInterval(loadEscalations,15000);
}

function showPg(name,el){
  document.querySelectorAll('.pg').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(n=>n.classList.remove('active'));
  document.getElementById('pg-'+name).classList.add('active');
  el.classList.add('active');
  if(name==='overview')    loadOverview();
  if(name==='messages')    loadConvs();
  if(name==='broadcast')   loadBcPage();
  if(name==='botcontrols') { loadBotStatus(); renderTplSettings(); loadTakeovers(); }
  if(name==='escalations') loadEscalationHistory();
  if(name==='activity')    loadActivityLog();
}

async function loadBotStatus(){
  const r=await fetch(API+'/admin/bot/status');
  if(!r.ok)return;
  const d=await r.json();
  applyBotStatus(d.paused);
}

function applyBotStatus(paused){
  const pill=document.getElementById('bot-pill');
  const pillText=document.getElementById('bot-pill-text');
  const pauseBtn=document.getElementById('pause-btn');
  const sw=document.getElementById('bot-switch');
  const title=document.getElementById('bc-toggle-title');
  const sub=document.getElementById('bc-toggle-sub');

  if(paused){
    pill.className='bot-pill off';
    pillText.textContent='Bot paused';
    pauseBtn.className='pause-btn resume';
    pauseBtn.textContent='Resume bot';
    sw.checked=false;
    title.textContent='Bot is paused';
    sub.textContent='The bot is not replying automatically. All new parent messages are waiting for a human reply. Turn this on to resume automatic replies.';
  } else {
    pill.className='bot-pill on';
    pillText.textContent='Bot online';
    pauseBtn.className='pause-btn';
    pauseBtn.textContent='Pause bot';
    sw.checked=true;
    title.textContent='Bot is online';
    sub.textContent='The bot is automatically replying to all new parent messages. Turn this off to pause all automatic replies — new messages will wait for a human reply.';
  }
}

async function toggleBot(){
  const r=await fetch(API+'/admin/bot/toggle',{method:'POST'});
  const d=await r.json();
  applyBotStatus(d.paused);
  toast(d.paused?'Bot paused — replies now require admin action':'Bot resumed — automatic replies active', d.paused?'info':'ok');
}

let lastEscalationCount=0;
async function loadEscalations(){
  const r=await fetch(API+'/admin/escalations');
  if(!r.ok)return;
  const items=await r.json();
  renderEscalationBanner(items);

  const tb=document.getElementById('tab-badge');
  if(items.length>0){ tb.classList.add('esc-badge'); } else { tb.classList.remove('esc-badge'); }

  if(items.length>lastEscalationCount && lastEscalationCount!==0){
    toast(`New escalation: ${items[0].name||items[0].phone}`,'err');
  } else if(items.length>0 && lastEscalationCount===0){
    toast(`${items.length} conversation${items.length>1?'s':''} need${items.length===1?'s':''} your attention`,'err');
  }
  lastEscalationCount=items.length;
}

function renderEscalationBanner(items){
  const banner=document.getElementById('escalation-banner');
  const inner=document.getElementById('escalation-banner-inner');
  if(!items.length){ banner.classList.remove('show'); inner.innerHTML=''; return; }
  banner.classList.add('show');
  inner.innerHTML=`
    <span class="esc-banner-icon">!</span>
    <span class="esc-banner-text">${items.length} conversation${items.length>1?'s':''} need${items.length===1?'s':''} admin attention</span>
    <div class="esc-banner-items">
      ${items.slice(0,5).map(it=>`
        <div class="esc-chip" onclick="goToEscalation('${it.phone}')">
          <span class="esc-chip-name">${esc(it.name||it.phone.replace('whatsapp:',''))}</span>
          <button class="esc-chip-btn" onclick="event.stopPropagation();takeoverFromBanner('${it.phone}')">Take over</button>
        </div>`).join('')}
    </div>`;
}

function goToEscalation(phone){
  document.querySelectorAll('.pg').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(n=>n.classList.remove('active'));
  document.getElementById('pg-messages').classList.add('active');
  document.querySelectorAll('.tab').forEach(t=>{if(t.textContent.includes('Messages'))t.classList.add('active');});
  loadConvs().then(()=>selConv(phone));
}

async function takeoverFromBanner(phone){
  await fetch(API+'/admin/conversations/'+encodeURIComponent(phone)+'/takeover',{method:'POST'});
  toast('Admin takeover — you are now in control','ok');
  loadEscalations();
  goToEscalation(phone);
}

async function loadOverview(){
  const r=await fetch(API+'/admin/metrics');
  if(!r.ok)return;
  const d=await r.json();

  setText('m-msgs-today',d.messages_today||0);
  const change=d.messages_pct_change||0;
  const changeEl=document.getElementById('m-msgs-change');
  if(change>0){ changeEl.innerHTML='↑ '+change+'% vs yesterday'; changeEl.className='mc-foot up'; }
  else if(change<0){ changeEl.innerHTML='↓ '+Math.abs(change)+'% vs yesterday'; changeEl.className='mc-foot down'; }
  else { changeEl.textContent='No change vs yesterday'; changeEl.className='mc-foot'; }

  setText('m-active',d.active_parents||0);
  setText('m-bot-resp',d.bot_responses||0);
  setText('m-bot-pct',(d.bot_pct||0)+'% handled by bot');
  setText('m-human',d.human_replies||0);
  setText('m-avgresp',(d.avg_response||0)+'s');
  setText('m-pending',d.pending_queries||0);

  const u=d.unread||0;
  const up=document.getElementById('unread-pill');
  up.textContent=u+' unread'; up.style.display=u>0?'inline-flex':'none';
  const tb=document.getElementById('tab-badge');
  tb.textContent=u; tb.style.display=u>0?'inline':'none';

  applyBotStatus(d.bot_paused);

  if(d.topics&&d.topics.length){
    const colors={'School fees & payment':'var(--forest)','Bus routes & fares':'var(--slate)',
      'Educational trips':'var(--gold)','Parental engagement':'var(--terracotta)',
      'ICT programme':'#6B7C56','Other':'var(--ink3)'};
    document.getElementById('topics-list').innerHTML=d.topics.map(([name,pct])=>`
      <div class="topic-row">
        <div class="topic-head"><span class="topic-name">${name}</span><span class="topic-pct">${pct}%</span></div>
        <div class="topic-bar-bg"><div class="topic-bar-fill" style="width:${pct}%;background:${colors[name]||'var(--forest)'}"></div></div>
      </div>`).join('');
  }

  if(d.weekly&&d.weekly.length){
    const mx=Math.max(...d.weekly.map(w=>w[1]),1);
    document.getElementById('week-chart').innerHTML=d.weekly.map(([lbl,val])=>`
      <div class="week-col">
        <div class="week-bar-val">${val}</div>
        <div class="week-bar" style="height:${Math.max((val/mx)*100,4)}px"></div>
        <div class="week-lbl">${lbl}</div>
      </div>`).join('');
  }

  const ar=await fetch(API+'/admin/activity');
  if(ar.ok){
    const items=await ar.json();
    renderActivityList('recent-activity', items.slice(0,8));
  }
}

async function loadActivityLog(){
  const r=await fetch(API+'/admin/activity');
  const items=await r.json();
  renderActivityList('activity-log-list', items);
}

function renderActivityList(elId, items){
  const el=document.getElementById(elId);
  if(!items.length){
    el.innerHTML='<div class="empty-state"><div class="ei">—</div><div class="et">No activity yet</div></div>';
    return;
  }
  el.innerHTML=items.map(it=>{
    const initials=getInitials(it.name);
    const color=avatarColor(it.phone);
    return `<div class="act-item">
      <div class="avatar" style="background:${color}">${initials}</div>
      <div class="act-body">
        <div class="act-name">${esc(it.name)}</div>
        <div class="act-msg">${esc(it.message)}</div>
      </div>
      <div class="act-right">
        <span class="status-badge status-${it.status}">${it.status}</span>
        <span class="act-time">${timeAgo(it.timestamp)}</span>
      </div>
    </div>`;
  }).join('');
}

function getInitials(name){
  if(!name) return '?';
  const parts=name.trim().split(/\s+/);
  if(parts.length>=2) return (parts[0][0]+parts[1][0]).toUpperCase();
  return name.replace(/\D/g,'').slice(-2) || name.substring(0,2).toUpperCase();
}

function avatarColor(phone){
  let hash=0;
  for(let i=0;i<phone.length;i++) hash=(hash*31+phone.charCodeAt(i))&0xffffffff;
  return AVATAR_COLORS[Math.abs(hash)%AVATAR_COLORS.length];
}

function timeAgo(ts){
  const diff=(Date.now()-new Date(ts).getTime())/1000;
  if(diff<60) return 'just now';
  if(diff<3600) return Math.floor(diff/60)+'m ago';
  if(diff<86400) return Math.floor(diff/3600)+'h ago';
  return Math.floor(diff/86400)+'d ago';
}

async function loadConvs(){
  const r=await fetch(API+'/admin/conversations');
  convData=await r.json();
  renderConvList(convData);
  if(selPhone) renderChat(selPhone);
  if(convTimer) clearInterval(convTimer);
  // This is the heaviest dashboard endpoint (full message history for every
  // conversation, re-serialized every poll) — kept on a longer interval than
  // the lighter overview/escalation polls to avoid starving the instance.
  convTimer=setInterval(async()=>{
    const r2=await fetch(API+'/admin/conversations');
    convData=await r2.json();
    renderConvList(convData);
    if(selPhone) renderChat(selPhone);
  },20000);
}

function renderConvList(data){
  const el=document.getElementById('conv-scroll');
  const phones=Object.keys(data);
  if(!phones.length){
    el.innerHTML='<div class="empty-state"><div class="ei">—</div><div class="et">No conversations yet</div></div>';
    return;
  }
  phones.sort((a,b)=>{
    const ae=data[a].escalated?1:0, be=data[b].escalated?1:0;
    if(ae!==be) return be-ae;
    const au=data[a].unread||0,bu=data[b].unread||0;
    if(au!==bu) return bu-au;
    return new Date(data[b].last_seen||0)-new Date(data[a].last_seen||0);
  });
  el.innerHTML=phones.map(ph=>{
    const c=data[ph];
    // Use last_message from the lightweight list endpoint, or fall back to
    // the cached full_log if this conversation has been opened already.
    const lastMsg=c.last_message||(c.full_log&&c.full_log.length?c.full_log[c.full_log.length-1].message:null);
    const preview=lastMsg?esc(lastMsg).substring(0,44)+'…':'No messages';
    const t=c.last_seen?new Date(c.last_seen).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}):'';
    const name=c.name||ph.replace('whatsapp:','');
    const statusTag=c.escalated?'<span class="tag tag-escalated">Needs attention</span>'
      :c.status==='pending'?'<span class="tag tag-amber">Pending</span>'
      :c.status==='override'?'<span class="tag tag-blue">Admin</span>'
      :'<span class="tag tag-green">Resolved</span>';
    return `<div class="cv-item ${selPhone===ph?'active':''} ${c.escalated?'cv-escalated':''}" onclick="selConv('${ph}')">
      <div class="cv-header"><span class="cv-name">${esc(name)}</span><span class="cv-time">${t}</span></div>
      <div class="cv-preview">${preview}</div>
      <div class="cv-tags">${statusTag}</div>
      ${c.unread>0?`<div class="cv-unread">${c.unread}</div>`:''}
    </div>`;
  }).join('');
}

function filterConvs(q){
  const f={};Object.keys(convData).forEach(p=>{
    const name=(convData[p].name||'')+p;
    if(name.toLowerCase().includes(q.toLowerCase()))f[p]=convData[p];
  });
  renderConvList(f);
}

function exportAllMessages(){
  window.open(API+'/admin/export/messages','_blank');
}

function exportEscalations(){
  window.open(API+'/admin/export/escalations','_blank');
}

async function loadEscalationHistory(){
  const r=await fetch(API+'/admin/escalations/history');
  const items=await r.json();
  const tb=document.getElementById('esc-hist-tbody');
  if(!items.length){
    tb.innerHTML='<tr><td colspan="6"><div class="empty-state"><div class="ei">—</div><div class="et">No escalations yet</div></div></td></tr>';
    return;
  }
  tb.innerHTML=items.map(h=>{
    const name=h.name||h.phone.replace('whatsapp:','');
    const escAt=h.escalated_at?new Date(h.escalated_at).toLocaleString():'';
    const resAt=h.resolved_at?new Date(h.resolved_at).toLocaleString():'—';
    const dur=h.resolution_minutes!=null?h.resolution_minutes+' min':'—';
    const statusCls=h.status==='resolved'?'esc-status-resolved':'esc-status-open';
    return `<tr class="esc-hist-row">
      <td>${esc(name)}</td>
      <td>${esc(h.reason||'')}</td>
      <td>${escAt}</td>
      <td>${resAt}</td>
      <td>${dur}</td>
      <td class="${statusCls}">${h.status}</td>
    </tr>`;
  }).join('');
}

async function selConv(ph){
  selPhone=ph;
  renderConvList(convData);
  // Show a loading state immediately so the UI feels responsive
  const panel=document.getElementById('chat-right');
  if(panel) panel.innerHTML='<div style="padding:2rem;color:#888;text-align:center;">Loading messages…</div>';
  // Load full message history on demand — only when opening this specific
  // conversation, not on every poll of the conversation list.
  const r=await fetch(API+'/admin/conversations/'+encodeURIComponent(ph)+'/messages');
  const msgs=await r.json();
  if(convData[ph]) convData[ph].full_log=msgs;
  renderChat(ph);
  await fetch(API+'/admin/conversations/'+encodeURIComponent(ph)+'/read',{method:'POST'});
  if(convData[ph]){ convData[ph].unread=0; }
}

function renderChat(ph){
  const c=convData[ph]; if(!c) return;
  const log=c.full_log||[];
  const isTa=c.admin_takeover;
  const name=c.name||ph.replace('whatsapp:','');
  const panel=document.getElementById('chat-right');

  const msgsHtml=log.length?log.map(m=>{
    const isIn=m.direction==='inbound';
    const isAdm=m.message&&(m.message.startsWith('[ADMIN]')||m.message.startsWith('[DIRECT]')||m.message.startsWith('[BROADCAST]'));
    const cls=isIn?'in':isAdm?'admin':'out';
    return `<div class="msg-wrap">
      <div class="bubble ${cls}">${esc(m.message).replace(/\n/g,'<br>').replace(/\*(.*?)\*/g,'<strong>$1</strong>')}</div>
      <div class="msg-ts ${isIn?'':'r'}">${new Date(m.timestamp).toLocaleTimeString()}</div>
    </div>`;
  }).join(''):'<div class="empty-state"><div class="ei">—</div><div class="et">No messages yet</div></div>';

  const qrBtns=allTpl.slice(0,5).map(t=>`<button class="qr-btn" onclick="useQR('${ph}',${t.id})">${esc(t.title)}</button>`).join('');

  panel.innerHTML=`
    <div class="chat-head">
      <div class="chat-head-info">
        <div class="cname">${esc(name)} ${c.escalated?'<span class="tag tag-escalated" style="margin-left:6px">Escalated</span>':''}</div>
        <div class="cstatus">${ph.replace('whatsapp:','')} · ${isTa?'Admin control':'Bot handling'} · ${c.message_count||0} messages</div>
      </div>
      <div style="display:flex;gap:6px">
        <button class="btn btn-ghost btn-sm" onclick="exportConvMessages('${ph}')">Export</button>
        ${isTa
          ?`<button class="btn btn-green btn-sm" onclick="relConv('${ph}')">Return to bot</button>`
          :`<button class="btn btn-amber btn-sm" onclick="taConv('${ph}')">Take over</button>`}
      </div>
    </div>
    ${c.escalated?`<div class="chat-escalation-notice">Escalation reason: ${esc(c.escalation_reason||'Needs admin attention')}</div>`:''}
    <div class="chat-msgs" id="chat-msgs">${msgsHtml}</div>
    ${isTa?`
      <div class="chat-takeover-notice">You are in control — bot is paused for this conversation</div>
      <div class="chat-foot">
        ${qrBtns?`<div class="qr-bar">${qrBtns}</div>`:''}
        <div class="chat-input-row">
          <textarea class="chat-ta" id="adm-input" rows="2" placeholder="Type your reply... (Enter to send, Shift+Enter for new line)"
            onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();sendAdm('${ph}')}"></textarea>
          <button class="btn btn-green" onclick="sendAdm('${ph}')">Send</button>
        </div>
      </div>`
    :`<div class="chat-bot-foot">
        <span class="chat-bot-note">Bot is handling this conversation</span>
        <button class="btn btn-amber btn-sm" onclick="taConv('${ph}')">Take over to reply</button>
      </div>`}`;

  const msgs=document.getElementById('chat-msgs');
  if(msgs) msgs.scrollTop=msgs.scrollHeight;
}

function exportConvMessages(ph){
  window.open(API+'/admin/export/messages?phone='+encodeURIComponent(ph),'_blank');
}

function useQR(ph,id){
  const t=allTpl.find(q=>q.id===id); if(!t) return;
  const inp=document.getElementById('adm-input'); if(inp) inp.value=t.body;
}

async function taConv(ph){
  await fetch(API+'/admin/conversations/'+encodeURIComponent(ph)+'/takeover',{method:'POST'});
  if(convData[ph]){ convData[ph].admin_takeover=true; convData[ph].escalated=false; }
  renderConvList(convData); renderChat(ph);
  loadEscalations();
  toast('Admin takeover — you are now in control','ok');
}

async function relConv(ph){
  await fetch(API+'/admin/conversations/'+encodeURIComponent(ph)+'/release',{method:'POST'});
  if(convData[ph]) convData[ph].admin_takeover=false;
  renderConvList(convData); renderChat(ph);
  toast('Returned to bot control','info');
}

async function sendAdm(ph){
  const inp=document.getElementById('adm-input');
  const msg=inp.value.trim(); if(!msg) return;
  const r=await fetch(API+'/admin/conversations/'+encodeURIComponent(ph)+'/send',{
    method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:msg})
  });
  if(r.ok){
    inp.value='';
    const r2=await fetch(API+'/admin/conversations');
    convData=await r2.json();
    renderConvList(convData); renderChat(ph);
    toast('Message sent to parent','ok');
  } else toast('Failed to send message','err');
}

async function sendDirect(){
  const phone=document.getElementById('direct-phone').value.trim();
  const msg=document.getElementById('direct-msg').value.trim();
  const st=document.getElementById('direct-status');
  if(!phone){toast('Please enter a phone number','err');return;}
  if(!msg){toast('Please type a message','err');return;}
  st.textContent='Sending...';
  const r=await fetch(API+'/admin/send-direct',{
    method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({phone,message:msg})
  });
  const d=await r.json();
  if(r.ok&&d.success){
    st.textContent='Sent to '+d.phone.replace('whatsapp:','');
    document.getElementById('direct-msg').value='';
    document.getElementById('direct-phone').value='';
    toast('Message sent successfully','ok');
  } else {
    st.textContent='Failed to send';
    toast('Failed — check phone number and WhatsApp token','err');
  }
}

async function loadBcPage(){
  const ur=await fetch(API+'/admin/users');
  const users=await ur.json();
  document.getElementById('bc-count').textContent=users.length;
  renderTplBc();
  const hr=await fetch(API+'/admin/broadcast/history');
  const hist=await hr.json();
  const tb=document.getElementById('bc-hist-tbody');
  if(!hist.length){tb.innerHTML='<tr><td colspan="5"><div class="empty-state"><div class="ei">—</div><div class="et">No broadcasts sent yet</div></div></td></tr>';return;}
  tb.innerHTML=hist.map(b=>`<tr>
    <td style="white-space:nowrap;font-size:11px">${new Date(b.timestamp).toLocaleString()}</td>
    <td style="max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px">${esc(b.message).substring(0,80)}</td>
    <td style="font-size:12px">${b.recipients.length}</td>
    <td style="color:var(--forest);font-weight:700;font-size:12px">${b.sent}</td>
    <td style="color:var(--terracotta);font-size:12px">${b.failed}</td>
  </tr>`).join('');
}

function bcPreview(){
  const msg=document.getElementById('bc-msg').value.trim();
  if(!msg){toast('Please type a message first','err');return;}
  document.getElementById('prev-body').textContent=msg;
  document.getElementById('prev-count').textContent=document.getElementById('bc-count').textContent;
  document.getElementById('prev-modal').classList.add('open');
}

async function bcConfirm(){closeModal('prev-modal');await bcSend();}

async function bcSend(){
  const msg=document.getElementById('bc-msg').value.trim();
  if(!msg){toast('Please type a message first','err');return;}
  const st=document.getElementById('bc-status');
  st.textContent='Sending...';
  const r=await fetch(API+'/admin/broadcast',{
    method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:msg})
  });
  const d=await r.json();
  if(r.ok){
    st.textContent=`Sent to ${d.sent} parents${d.failed>0?' ('+d.failed+' failed)':''}`;
    document.getElementById('bc-msg').value='';
    toast(`Broadcast sent to ${d.sent} parents`,'ok');
    loadBcPage();
  } else {st.textContent='Failed';toast('Broadcast failed','err');}
}

async function loadTpl(){
  const r=await fetch(API+'/admin/quick-replies');
  allTpl=await r.json();
}

function renderTplSettings(){
  const el=document.getElementById('tpl-settings');
  if(!allTpl.length){el.innerHTML='<div class="empty-state"><div class="ei">—</div><div class="et">No templates yet. Add one above.</div></div>';return;}
  el.innerHTML=allTpl.map(t=>`
    <div class="tpl-card">
      <button class="tpl-del" onclick="delTpl(${t.id},event)">×</button>
      <div class="tpl-title">${esc(t.title)}</div>
      <div class="tpl-body">${esc(t.body)}</div>
    </div>`).join('');
}

function renderTplBc(){
  const el=document.getElementById('tpl-bc');
  if(!el) return;
  if(!allTpl.length){el.innerHTML='<div class="empty-state"><div class="ei">—</div><div class="et">No templates yet.</div></div>';return;}
  el.innerHTML=allTpl.map(t=>`
    <div class="tpl-card" data-tpl-id="${t.id}">
      <div class="tpl-title">${esc(t.title)}</div>
      <div class="tpl-body">${esc(t.body)}</div>
      <div class="tpl-hint">Click to use in compose →</div>
    </div>`).join('');
  el.querySelectorAll('.tpl-card').forEach(card=>{
    card.addEventListener('click',()=>{
      const id=parseInt(card.getAttribute('data-tpl-id'),10);
      useTplBc(id);
    });
  });
}

function useTplBc(id){
  const t=allTpl.find(x=>x.id===id);
  if(!t){ toast('Template not found','err'); return; }
  document.getElementById('bc-msg').value=t.body;
  document.getElementById('bc-msg').dispatchEvent(new Event('input'));
  toast('Template loaded — edit as needed before sending','info');
}

function openTplModal(){document.getElementById('tpl-modal').classList.add('open');}

async function saveTpl(){
  const title=document.getElementById('tpl-title').value.trim();
  const body=document.getElementById('tpl-body').value.trim();
  if(!title||!body){toast('Please fill in both fields','err');return;}
  await fetch(API+'/admin/quick-replies',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title,body})});
  await loadTpl();
  renderTplSettings(); renderTplBc();
  closeModal('tpl-modal');
  document.getElementById('tpl-title').value='';
  document.getElementById('tpl-body').value='';
  toast('Template saved','ok');
}

async function delTpl(id,e){
  e.stopPropagation();
  if(!confirm('Delete this template?')) return;
  await fetch(API+'/admin/quick-replies/'+id,{method:'DELETE'});
  await loadTpl();
  renderTplSettings(); renderTplBc();
  toast('Template deleted','info');
}

async function loadTakeovers(){
  const r=await fetch(API+'/admin/bot/status');
  const d=await r.json();
  const el=document.getElementById('takeover-list');
  if(!d.takeovers||!d.takeovers.length){
    el.innerHTML='<div class="empty-state"><div class="ei">—</div><div class="et">No conversations currently overridden by admin</div></div>';
    return;
  }
  el.innerHTML=d.takeovers.map(ph=>`
    <div class="takeover-item">
      <div>
        <div class="takeover-phone">${ph.replace('whatsapp:','')}</div>
        <div class="takeover-meta">Admin is currently handling this conversation</div>
      </div>
      <button class="btn btn-green btn-sm" onclick="releaseFromControls('${ph}')">Return to bot</button>
    </div>`).join('');
}

async function releaseFromControls(ph){
  await fetch(API+'/admin/conversations/'+encodeURIComponent(ph)+'/release',{method:'POST'});
  toast('Returned to bot control','info');
  loadTakeovers();
}

function closeModal(id){document.getElementById(id).classList.remove('open');}

function toast(msg,type='ok'){
  const t=document.getElementById('toast');
  t.textContent=msg;
  t.className='toast '+type;
  t.classList.add('show');
  setTimeout(()=>t.classList.remove('show'),3500);
}

function setText(id,v){const el=document.getElementById(id);if(el)el.textContent=v;}

function esc(s){
  if(!s) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
</script>
</body>
</html>
"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
