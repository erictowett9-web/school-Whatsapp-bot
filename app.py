import os
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
app.secret_key = os.getenv("SECRET_KEY", "sallyann-secret-2026")

# ── Env vars ───────────────────────────────────────────────────────────────────
GROQ_API_KEY        = os.getenv("GROQ_API_KEY", "")
WHATSAPP_TOKEN      = os.getenv("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID     = os.getenv("PHONE_NUMBER_ID", "")
VERIFY_TOKEN        = os.getenv("VERIFY_TOKEN", "")
GEMINI_API_KEY      = os.getenv("GEMINI_API_KEY", "")
APP_SECRET          = os.getenv("APP_SECRET", "")
ADMIN_PASSWORD      = os.getenv("ADMIN_PASSWORD", "sallyann2026")
ADMIN_WHATSAPP_NUMBER = os.getenv("ADMIN_WHATSAPP_NUMBER", "")  # e.g. whatsapp:+254723422407

groq_client = Groq(api_key=GROQ_API_KEY)

# ── Init DB ────────────────────────────────────────────────────────────────────
db.init_db()

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
# Parent messages containing these words always escalate to a human, regardless
# of whether the AI could technically generate a reply.
ESCALATION_KEYWORDS = [
    "complaint", "complain", "refund", "transfer", "lost", "emergency", "urgent",
    "sick", "injury", "injured", "accident", "bully", "bullying", "abuse",
    "harassment", "lawyer", "police", "expel", "expelled", "suspend", "suspended",
    "malalamiko", "dharura", "kashe",  # Swahili: complaint, emergency, harassed
]

# Phrases that suggest the AI itself is uncertain and a human should step in.
UNCERTAINTY_PHRASES = [
    "i'm not sure", "i am not sure", "i don't have that information",
    "i do not have that information", "i'm unable to", "i am unable to",
    "i don't know", "i do not know", "please call the school office",
    "contact the school office", "i can't help with that", "i cannot help with that",
]

def needs_escalation(parent_message, bot_reply=None):
    """Decide whether this exchange should be escalated to a human admin."""
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

# In-memory tracker for response-time calc (per-process; resets on restart, acceptable)
_last_inbound_time = {}

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
    """Strip whatsapp: prefix and leading + so numbers compare reliably,
    since Meta's webhook 'from' field is digits-only (no + and no prefix)."""
    if not p:
        return ""
    return p.replace("whatsapp:", "").replace("+", "").strip()

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

def alert_admin(parent_phone, parent_message, reason):
    """Notify the school admin on WhatsApp that a parent query needs a human reply.
    Includes a short reply code the admin can use to reply directly from their phone."""
    if not ADMIN_WHATSAPP_NUMBER:
        logger.warning("ADMIN_WHATSAPP_NUMBER not set — cannot send admin alert")
        return False
    parent_display = parent_phone.replace("whatsapp:", "")
    code = parent_display[-4:]  # last 4 digits, e.g. "9896"
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

def verify_signature(req):
    if not APP_SECRET:
        return True
    signature = req.headers.get("X-Hub-Signature-256", "")
    if not signature.startswith("sha256="):
        return False
    expected = hmac.new(APP_SECRET.encode(), req.data, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)

def log_msg(phone, message, direction="inbound", sender="bot"):
    """Wraps db.log_message and updates daily counters + faq counts + response times."""
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
    try:
        value = data["entry"][0]["changes"][0]["value"]

        # Meta sends multiple event types on this same webhook: new messages,
        # delivery receipts, read receipts, etc. We only care about "messages".
        # Anything else (e.g. "statuses") is expected and silently ignored.
        if "messages" not in value:
            if "statuses" in value:
                logger.debug("Ignoring status update (delivery/read receipt)")
            else:
                logger.debug(f"Ignoring unhandled webhook event: {list(value.keys())}")
            return jsonify({"status": "ok"}), 200

        message  = value["messages"][0]
        phone    = message["from"]
        msg_type = message["type"]
        logger.info(f"[{phone}] Type: {msg_type}")
        if ADMIN_WHATSAPP_NUMBER:
            logger.debug(f"Comparing sender={normalize_phone(phone)} vs admin={normalize_phone(ADMIN_WHATSAPP_NUMBER)}")

        # ── Admin reply-by-phone shortcut ──────────────────────────────────
        # If this message is FROM the admin's own WhatsApp number and matches
        # the pattern "1234: reply text", forward the reply text to the
        # parent whose number ends in that 4-digit code, instead of treating
        # this as a normal incoming parent message.
        if ADMIN_WHATSAPP_NUMBER and normalize_phone(phone) == normalize_phone(ADMIN_WHATSAPP_NUMBER):
            if msg_type == "text":
                admin_text = message["text"]["body"].strip()
                import re
                m = re.match(r"^(\d{4})\s*[:\-]\s*(.+)$", admin_text, re.DOTALL)
                if m:
                    code, reply_text = m.group(1), m.group(2).strip()
                    target_phone = db.get_phone_by_code(code)
                    if target_phone:
                        log_msg(target_phone, f"[ADMIN] {reply_text}", "outbound", sender="admin")
                        history = db.get_history(target_phone)
                        history.append({"role": "assistant", "content": reply_text})
                        db.save_history(target_phone, history[-20:])
                        send_whatsapp(target_phone, reply_text)
                        db.clear_escalated(target_phone)
                        send_whatsapp(ADMIN_WHATSAPP_NUMBER,
                            f"✅ Sent to {target_phone.replace('whatsapp:','')}: \"{reply_text}\"")
                        logger.info(f"Admin reply-by-code {code} forwarded to {target_phone}")
                    else:
                        send_whatsapp(ADMIN_WHATSAPP_NUMBER,
                            f"⚠️ No pending conversation found for code {code}. It may have expired or already been handled.")
                    return jsonify({"status": "ok"}), 200
            # Any other message from the admin's own number falls through to
            # normal handling below (e.g. if admin is also a parent — rare,
            # but we don't want to silently eat messages that aren't replies).

        name = None
        try:
            contacts = value.get("contacts", [])
            if contacts:
                name = contacts[0].get("profile", {}).get("name")
        except Exception:
            pass
        db.touch_active_user(phone, name)

        if msg_type == "image":
            reply = ("Thank you for sending your payment receipt! 📸\n"
                     "Our office will confirm within 24 hours.\n"
                     "For instant confirmation call the school office.")
            log_msg(phone, "[Image received]", "inbound")
            if not db.is_bot_paused() and not db.is_admin_takeover(phone):
                log_msg(phone, reply, "outbound", sender="bot")
                send_whatsapp(phone, reply)
            return jsonify({"status": "ok"}), 200

        if msg_type != "text":
            send_whatsapp(phone, "Sorry, I can only handle text messages for now.")
            return jsonify({"status": "ok"}), 200

        incoming = message["text"]["body"].strip()
        log_msg(phone, incoming, "inbound")

        if db.is_admin_takeover(phone):
            return jsonify({"status": "ok"}), 200
        if db.is_bot_paused():
            return jsonify({"status": "ok"}), 200

        reply, use_ai = find_keyword_response(incoming)
        if use_ai:
            reply = ask_ai(phone, incoming)
        else:
            history = db.get_history(phone)
            history.append({"role": "user", "content": incoming})
            history.append({"role": "assistant", "content": reply})
            db.save_history(phone, history[-20:])

        log_msg(phone, reply, "outbound", sender="bot")
        send_whatsapp(phone, reply)

        # ── Escalation check ──────────────────────────────────────────────
        # If the parent's message hits a sensitive keyword, or the bot's own
        # reply signals uncertainty, alert the admin AND let the parent know
        # a human will be following up, so they're not left wondering.
        if needs_escalation(incoming, reply):
            keyword_hit = next((kw for kw in ESCALATION_KEYWORDS if kw in incoming.lower()), None)
            reason = f"Sensitive keyword: '{keyword_hit}'" if keyword_hit else "Bot was uncertain of the answer"

            escalation_notice = ("📌 A member of our school team has been notified and will "
                                  "follow up with you shortly. Thank you for your patience.")
            log_msg(phone, escalation_notice, "outbound", sender="bot")
            send_whatsapp(phone, escalation_notice)

            alert_admin(phone, incoming, reason)
            db.set_escalated(phone, reason)
            logger.info(f"[{phone}] Escalated to admin — {reason}")

    except (KeyError, IndexError) as e:
        # A genuine parse failure on a "messages" event we expected to handle
        logger.warning(f"Webhook parse error: {e}")

    return jsonify({"status": "ok"}), 200

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

@app.route("/admin/login", methods=["POST"])
def admin_login():
    data = request.get_json()
    if data.get("password") == ADMIN_PASSWORD:
        session["admin_logged_in"] = True
        return jsonify({"success": True})
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

    # Pending = users whose last message is inbound (derived from activity items)
    activity = db.get_activity_items(500)
    pending = sum(1 for a in activity if get_conv_status(a["message"], a["direction"]) == "pending")

    # Topic breakdown
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

    # 7-day volume
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
        return jsonify(msgs)  # already DESC ordered
    return jsonify(list(reversed(msgs)))

@app.route("/admin/conversations")
@admin_required
def get_conversations():
    convs = db.get_all_conversations()
    result = {}
    for phone, c in convs.items():
        full_log = db.get_messages(limit=1000, phone=phone)
        unread = sum(1 for m in full_log if m["direction"] == "inbound" and not m.get("read_flag"))
        last = full_log[-1] if full_log else None
        status = get_conv_status(last["message"], last["direction"]) if last else "pending"
        if c.get("escalated"):
            status = "escalated"
        result[phone] = {
            "phone": phone,
            "name": c.get("name"),
            "last_seen": c.get("last_seen"),
            "full_log": full_log[-50:],
            "unread": unread,
            "admin_takeover": c.get("admin_takeover", False),
            "message_count": len(full_log),
            "status": status,
            "escalated": c.get("escalated", False),
            "escalation_reason": c.get("escalation_reason"),
        }
    return jsonify(result)

@app.route("/admin/conversations/<path:phone>/takeover", methods=["POST"])
@admin_required
def takeover(phone):
    db.set_admin_takeover(phone, True)
    db.mark_messages_read(phone)
    db.clear_escalated(phone)
    return jsonify({"success": True})

@app.route("/admin/conversations/<path:phone>/release", methods=["POST"])
@admin_required
def release(phone):
    db.set_admin_takeover(phone, False)
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
<style>
:root{
  --bg:#070d18;--s1:#0c1626;--s2:#111f35;--s3:#172a47;
  --border:#1d3050;--border2:#26405f;
  --green:#00d4a0;--green2:#00b085;--glow:rgba(0,212,160,.13);
  --amber:#f5a623;--ramber:rgba(245,166,35,.13);
  --red:#ff4757;--rred:rgba(255,71,87,.13);
  --blue:#4facfe;--rblue:rgba(79,172,254,.13);
  --purple:#a78bfa;--rpurple:rgba(167,139,250,.13);
  --text:#e1ebf7;--text2:#7390b2;--text3:#3e597e;
  --font:'Segoe UI',system-ui,-apple-system,sans-serif;
  --r:10px;--r2:14px;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:var(--font);min-height:100vh}
::-webkit-scrollbar{width:5px;height:5px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--border2);border-radius:3px}

/* ── LOGIN ── */
#login{display:flex;align-items:center;justify-content:center;min-height:100vh;
  background:radial-gradient(ellipse 60% 50% at 30% 60%,rgba(0,212,160,.06),transparent),var(--bg)}
.lc{width:380px}
.lc-logo{display:flex;align-items:center;gap:14px;margin-bottom:32px}
.lc-emblem{width:52px;height:52px;background:var(--glow);border:1.5px solid var(--green);
  border-radius:14px;display:flex;align-items:center;justify-content:center;font-size:24px}
.lc-name{font-size:18px;font-weight:800;line-height:1.25}
.lc-sub{font-size:11px;color:var(--text2);text-transform:uppercase;letter-spacing:2px;margin-top:2px}
.lc-label{font-size:11px;color:var(--text2);margin-bottom:7px;text-transform:uppercase;letter-spacing:.8px}
.lc-input{width:100%;padding:13px 16px;background:var(--s2);border:1px solid var(--border);
  border-radius:var(--r);color:var(--text);font-size:15px;outline:none;transition:border .2s;margin-bottom:12px}
.lc-input:focus{border-color:var(--green);box-shadow:0 0 0 3px var(--glow)}
.lc-btn{width:100%;padding:13px;background:var(--green);color:#000;border:none;
  border-radius:var(--r);font-size:15px;font-weight:800;cursor:pointer;letter-spacing:.3px}
.lc-btn:hover{background:var(--green2)}
.lc-err{color:var(--red);font-size:12px;margin-top:10px;min-height:18px}

/* ── SHELL ── */
#app{display:none;flex-direction:column;min-height:100vh}

/* ── HEADER ── */
.header{background:var(--s1);border-bottom:1px solid var(--border);padding:16px 24px}
.header-row{display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:12px}
.hdr-left{display:flex;align-items:center;gap:14px}
.hdr-emblem{width:42px;height:42px;background:var(--glow);border:1.5px solid var(--green);
  border-radius:11px;display:flex;align-items:center;justify-content:center;font-size:20px;flex-shrink:0}
.hdr-title{font-size:17px;font-weight:900;letter-spacing:.2px}
.hdr-sub{font-size:11px;color:var(--text2);margin-top:2px}
.hdr-right{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.bot-pill{display:flex;align-items:center;gap:7px;padding:6px 14px;border-radius:20px;
  font-size:12px;font-weight:700;border:1px solid}
.bot-pill.on{background:var(--glow);border-color:rgba(0,212,160,.35);color:var(--green)}
.bot-pill.off{background:var(--rred);border-color:rgba(255,71,87,.35);color:var(--red)}
.bot-pill-dot{width:7px;height:7px;border-radius:50%}
.bot-pill.on .bot-pill-dot{background:var(--green);animation:blink 1.4s infinite}
.bot-pill.off .bot-pill-dot{background:var(--red)}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.25}}
.pause-btn{padding:7px 16px;border-radius:8px;font-size:12px;font-weight:700;cursor:pointer;
  border:1px solid;transition:all .15s}
.pause-btn.pause{background:transparent;border-color:var(--border);color:var(--text2)}
.pause-btn.pause:hover{border-color:var(--amber);color:var(--amber)}
.pause-btn.resume{background:var(--green);border-color:var(--green);color:#000}
.signout-btn{padding:7px 14px;background:transparent;border:1px solid var(--border);
  color:var(--text2);border-radius:8px;cursor:pointer;font-size:12px;transition:all .2s}
.signout-btn:hover{border-color:var(--red);color:var(--red)}
.unread-pill{background:var(--red);color:#fff;border-radius:20px;padding:5px 11px;
  font-size:11px;font-weight:800;display:none}

/* ── TABS ── */
.tabbar{display:flex;gap:4px;padding:0 24px;background:var(--s1);border-bottom:1px solid var(--border);
  overflow-x:auto}
.tab{padding:13px 18px;font-size:13px;font-weight:700;color:var(--text2);cursor:pointer;
  border-bottom:2px solid transparent;transition:all .15s;white-space:nowrap;display:flex;
  align-items:center;gap:6px}
.tab:hover{color:var(--text)}
.tab.active{color:var(--green);border-bottom-color:var(--green)}
.tab-badge{background:var(--red);color:#fff;border-radius:20px;padding:1px 7px;font-size:10px;font-weight:800;display:none}
.tab-badge.esc-badge{background:var(--red);animation:escPulse 1.2s infinite}
@keyframes escPulse{0%,100%{box-shadow:0 0 0 0 rgba(255,71,87,.5)}50%{box-shadow:0 0 0 5px rgba(255,71,87,0)}}

/* ── ESCALATION BANNER ── */
.escalation-banner{display:none;background:linear-gradient(90deg,rgba(255,71,87,.16),rgba(255,71,87,.06));
  border-bottom:1px solid rgba(255,71,87,.35);overflow:hidden}
.escalation-banner.show{display:block}
.esc-banner-inner{padding:10px 24px;display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.esc-banner-icon{font-size:16px;animation:escPulse 1.2s infinite;flex-shrink:0}
.esc-banner-text{font-size:12.5px;color:var(--red);font-weight:700;flex:1;min-width:200px}
.esc-banner-items{display:flex;gap:8px;flex-wrap:wrap}
.esc-chip{display:flex;align-items:center;gap:8px;background:var(--s1);border:1px solid rgba(255,71,87,.35);
  border-radius:20px;padding:5px 8px 5px 12px;font-size:11px;cursor:pointer;transition:all .15s}
.esc-chip:hover{background:var(--s2);border-color:var(--red)}
.esc-chip-name{font-weight:700;color:var(--text)}
.esc-chip-btn{background:var(--red);color:#fff;border:none;border-radius:14px;padding:3px 10px;
  font-size:10px;font-weight:700;cursor:pointer}
.esc-chip-btn:hover{opacity:.85}
.status-escalated{background:var(--rred);color:var(--red);animation:escPulse 1.2s infinite}
.tag-escalated{background:var(--rred);color:var(--red);border:1px solid rgba(255,71,87,.3);animation:escPulse 1.2s infinite}

/* ── CONTENT ── */
.content{flex:1;padding:22px 24px}
.pg{display:none}.pg.active{display:block}

/* ── METRICS ── */
.metrics-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(165px,1fr));gap:13px;margin-bottom:20px}
.mc{background:var(--s1);border:1px solid var(--border);border-radius:var(--r2);padding:17px 18px;
  position:relative;overflow:hidden;transition:border-color .2s}
.mc:hover{border-color:var(--border2)}
.mc-top{position:absolute;top:0;left:0;right:0;height:2.5px}
.mc-top.green{background:linear-gradient(90deg,var(--green),var(--green2))}
.mc-top.amber{background:var(--amber)}
.mc-top.red{background:var(--red)}
.mc-top.blue{background:var(--blue)}
.mc-top.purple{background:var(--purple)}
.mc-label{font-size:11px;color:var(--text2);font-weight:600;margin-bottom:8px}
.mc-val{font-size:30px;font-weight:900;line-height:1;letter-spacing:-1px}
.mc-val.green{color:var(--green)}
.mc-val.amber{color:var(--amber)}
.mc-val.red{color:var(--red)}
.mc-val.blue{color:var(--blue)}
.mc-val.purple{color:var(--purple)}
.mc-foot{font-size:11px;color:var(--text2);margin-top:7px;display:flex;align-items:center;gap:5px}
.mc-foot.up{color:var(--green)}
.mc-foot.down{color:var(--red)}

/* ── CARDS ── */
.card{background:var(--s1);border:1px solid var(--border);border-radius:var(--r2);padding:20px;margin-bottom:16px}
.ch{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;flex-wrap:wrap;gap:8px}
.ct{font-size:13px;font-weight:800}
.two-col{display:grid;grid-template-columns:1.1fr .9fr;gap:16px}
@media(max-width:900px){.two-col{grid-template-columns:1fr}}

/* ── TOPIC BARS ── */
.topic-row{margin-bottom:14px}
.topic-head{display:flex;justify-content:space-between;font-size:12.5px;margin-bottom:6px}
.topic-name{font-weight:600}
.topic-pct{font-weight:800;color:var(--text)}
.topic-bar-bg{height:8px;background:var(--s3);border-radius:4px;overflow:hidden}
.topic-bar-fill{height:100%;border-radius:4px;transition:width .7s ease}

/* ── WEEKLY CHART ── */
.week-chart{display:flex;align-items:flex-end;gap:10px;height:140px;padding-top:10px}
.week-col{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;gap:8px;height:100%}
.week-bar{width:100%;max-width:38px;background:linear-gradient(180deg,var(--green),var(--green2));
  border-radius:6px 6px 0 0;min-height:4px;transition:height .6s ease;position:relative}
.week-bar-val{font-size:10px;color:var(--text2);font-weight:700}
.week-lbl{font-size:11px;color:var(--text2);font-weight:600}

/* ── ACTIVITY LIST ── */
.activity-list{display:flex;flex-direction:column;gap:2px}
.act-item{display:flex;align-items:flex-start;gap:12px;padding:13px 4px;border-bottom:1px solid var(--border)}
.act-item:last-child{border-bottom:none}
.avatar{width:38px;height:38px;border-radius:50%;display:flex;align-items:center;justify-content:center;
  font-size:13px;font-weight:800;flex-shrink:0;color:#fff}
.act-body{flex:1;min-width:0}
.act-name{font-size:13px;font-weight:700;margin-bottom:2px}
.act-msg{font-size:12px;color:var(--text2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:100%}
.act-right{display:flex;flex-direction:column;align-items:flex-end;gap:5px;flex-shrink:0}
.act-time{font-size:10px;color:var(--text3)}
.status-badge{font-size:10px;font-weight:800;padding:3px 10px;border-radius:20px;text-transform:capitalize;letter-spacing:.3px}
.status-pending{background:var(--ramber);color:var(--amber)}
.status-resolved{background:var(--glow);color:var(--green)}
.status-override{background:var(--rpurple);color:var(--purple)}

/* ── MESSAGES / CONVERSATIONS ── */
.conv-wrap{display:grid;grid-template-columns:280px 1fr;border:1px solid var(--border);
  border-radius:var(--r2);overflow:hidden;height:calc(100vh - 230px)}
.conv-left{background:var(--s1);border-right:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden}
.conv-search{padding:10px;border-bottom:1px solid var(--border)}
.conv-search input{width:100%;padding:8px 12px;background:var(--s2);border:1px solid var(--border);
  border-radius:7px;color:var(--text);font-size:12px;outline:none}
.conv-search input:focus{border-color:var(--green)}
.conv-scroll{flex:1;overflow-y:auto}
.cv-item{padding:12px 14px;border-bottom:1px solid var(--border);cursor:pointer;transition:background .12s;position:relative}
.cv-item:hover{background:var(--s2)}
.cv-item.active{background:var(--glow);border-left:2px solid var(--green)}
.cv-item.cv-escalated{background:rgba(255,71,87,.06);border-left:2px solid var(--red)}
.cv-item.cv-escalated.active{background:rgba(255,71,87,.12)}
.cv-header{display:flex;justify-content:space-between;margin-bottom:3px}
.cv-name{font-size:12px;font-weight:700}
.cv-time{font-size:9px;color:var(--text2)}
.cv-preview{font-size:11px;color:var(--text2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cv-tags{display:flex;gap:5px;margin-top:6px}
.tag{display:inline-flex;align-items:center;gap:3px;padding:2px 7px;border-radius:20px;font-size:9px;font-weight:700}
.tag-green{background:var(--glow);color:var(--green)}
.tag-amber{background:var(--ramber);color:var(--amber)}
.tag-blue{background:var(--rblue);color:var(--blue)}
.tag-purple{background:var(--rpurple);color:var(--purple)}
.cv-unread{position:absolute;right:12px;top:14px;background:var(--green);color:#000;
  width:18px;height:18px;border-radius:50%;font-size:9px;font-weight:800;display:flex;align-items:center;justify-content:center}

.chat-right{display:flex;flex-direction:column;background:var(--bg);overflow:hidden}
.chat-head{padding:12px 16px;background:var(--s1);border-bottom:1px solid var(--border);
  display:flex;justify-content:space-between;align-items:center;flex-shrink:0;flex-wrap:wrap;gap:8px}
.chat-head-info .cname{font-size:14px;font-weight:800}
.chat-head-info .cstatus{font-size:10px;color:var(--text2);margin-top:2px}
.chat-msgs{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:5px}
.msg-wrap{display:flex;flex-direction:column}
.bubble{max-width:75%;padding:9px 13px;border-radius:11px;font-size:12.5px;line-height:1.55;word-break:break-word}
.bubble.in{background:var(--s2);border:1px solid var(--border);align-self:flex-start;border-bottom-left-radius:3px}
.bubble.out{background:var(--glow);border:1px solid rgba(0,212,160,.18);align-self:flex-end;border-bottom-right-radius:3px}
.bubble.admin{background:var(--ramber);border:1px solid rgba(245,166,35,.2);align-self:flex-end;border-bottom-right-radius:3px}
.msg-ts{font-size:9px;color:var(--text3);margin-top:2px}
.msg-ts.r{align-self:flex-end}
.chat-takeover-notice{padding:8px 16px;background:var(--ramber);border-top:1px solid rgba(245,166,35,.18);
  font-size:11px;color:var(--amber);text-align:center;flex-shrink:0}
.chat-escalation-notice{padding:9px 16px;background:var(--rred);border-bottom:1px solid rgba(255,71,87,.25);
  font-size:11.5px;color:var(--red);font-weight:600;flex-shrink:0}
.chat-foot{padding:10px 14px;background:var(--s1);border-top:1px solid var(--border);flex-shrink:0}
.qr-bar{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px}
.qr-btn{padding:4px 10px;background:var(--s2);border:1px solid var(--border);
  color:var(--text2);border-radius:6px;font-size:10px;cursor:pointer;transition:all .15s}
.qr-btn:hover{border-color:var(--green);color:var(--green)}
.chat-input-row{display:flex;gap:8px;align-items:flex-end}
.chat-ta{flex:1;padding:9px 13px;background:var(--s2);border:1px solid var(--border);
  border-radius:8px;color:var(--text);font-size:13px;outline:none;resize:none;max-height:100px;line-height:1.45}
.chat-ta:focus{border-color:var(--green);box-shadow:0 0 0 2px var(--glow)}
.no-chat{display:flex;flex-direction:column;align-items:center;justify-content:center;flex:1;color:var(--text2);gap:10px}
.no-chat-icon{font-size:44px;opacity:.25}
.chat-bot-foot{padding:12px 16px;background:var(--s1);border-top:1px solid var(--border);
  display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;flex-shrink:0}
.chat-bot-note{font-size:11px;color:var(--text2)}

/* ── TABLE ── */
.tbl-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:12px}
th{text-align:left;padding:9px 14px;color:var(--text2);font-size:10px;text-transform:uppercase;
  letter-spacing:.5px;border-bottom:1px solid var(--border);white-space:nowrap}
td{padding:10px 14px;border-bottom:1px solid var(--border);vertical-align:middle}
tr:last-child td{border-bottom:none}
tr:hover td{background:var(--s2)}

/* ── BROADCAST ── */
.bc-compose{padding:16px;background:var(--s2);border-radius:var(--r);margin-bottom:14px}
.bc-ta{width:100%;padding:12px 14px;background:var(--s1);border:1px solid var(--border);
  border-radius:8px;color:var(--text);font-size:13px;outline:none;resize:vertical;min-height:100px;line-height:1.5}
.bc-ta:focus{border-color:var(--green);box-shadow:0 0 0 2px var(--glow)}
.bc-meta{display:flex;justify-content:space-between;align-items:center;margin-top:8px;flex-wrap:wrap;gap:6px}
.bc-count{font-size:11px;color:var(--text2)}
.bc-actions{display:flex;gap:8px;margin-top:12px;align-items:center;flex-wrap:wrap}
.bc-status{font-size:12px;color:var(--text2)}
.template-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:10px;margin-bottom:14px}
.tpl-card{background:var(--s2);border:1px solid var(--border);border-radius:var(--r);
  padding:14px;cursor:pointer;transition:all .15s;position:relative}
.tpl-card:hover{border-color:var(--green);background:var(--glow)}
.tpl-title{font-size:12px;font-weight:700;color:var(--green);margin-bottom:6px}
.tpl-body{font-size:11px;color:var(--text2);line-height:1.5;max-height:55px;overflow:hidden}
.tpl-hint{font-size:9px;color:var(--green);margin-top:8px;opacity:.7}
.tpl-del{position:absolute;top:8px;right:8px;background:rgba(255,71,87,.15);border:none;
  color:var(--red);width:20px;height:20px;border-radius:50%;cursor:pointer;font-size:11px;
  display:flex;align-items:center;justify-content:center;transition:background .2s}
.tpl-del:hover{background:rgba(255,71,87,.3)}
.finput{width:100%;padding:10px 13px;background:var(--s2);border:1px solid var(--border);
  border-radius:8px;color:var(--text);font-size:13px;outline:none}
.finput:focus{border-color:var(--green);box-shadow:0 0 0 2px var(--glow)}

/* ── BOT CONTROLS ── */
.toggle-card{display:flex;justify-content:space-between;align-items:center;padding:22px;
  background:var(--s2);border-radius:var(--r2);border:1px solid var(--border);margin-bottom:16px;flex-wrap:wrap;gap:14px}
.toggle-info .tc-title{font-size:15px;font-weight:800;margin-bottom:4px}
.toggle-info .tc-sub{font-size:12px;color:var(--text2);max-width:420px}
.switch{position:relative;width:56px;height:30px;flex-shrink:0}
.switch input{opacity:0;width:0;height:0}
.slider{position:absolute;cursor:pointer;inset:0;background:var(--s3);border:1px solid var(--border);
  border-radius:30px;transition:.25s}
.slider::before{content:'';position:absolute;height:22px;width:22px;left:3px;bottom:3px;
  background:var(--text2);border-radius:50%;transition:.25s}
input:checked + .slider{background:var(--glow);border-color:var(--green)}
input:checked + .slider::before{transform:translateX(24px);background:var(--green)}
.takeover-list{display:flex;flex-direction:column;gap:8px}
.takeover-item{display:flex;justify-content:space-between;align-items:center;padding:12px 14px;
  background:var(--s2);border:1px solid var(--border);border-radius:8px}
.takeover-phone{font-size:13px;font-weight:700}
.takeover-meta{font-size:11px;color:var(--text2);margin-top:2px}

/* ── BUTTONS ── */
.btn{padding:8px 16px;border-radius:8px;font-size:12px;font-weight:700;cursor:pointer;
  border:none;transition:all .15s;display:inline-flex;align-items:center;gap:5px;letter-spacing:.2px}
.btn:hover{opacity:.85}.btn:active{transform:scale(.97)}
.btn-green{background:var(--green);color:#000}
.btn-amber{background:var(--amber);color:#000}
.btn-red{background:var(--red);color:#fff}
.btn-ghost{background:var(--s2);color:var(--text);border:1px solid var(--border)}
.btn-ghost:hover{border-color:var(--green)}
.btn-sm{padding:5px 11px;font-size:11px}

/* ── MODAL ── */
.modal-ov{display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:1000;
  align-items:center;justify-content:center;backdrop-filter:blur(2px)}
.modal-ov.open{display:flex}
.modal{background:var(--s1);border:1px solid var(--border2);border-radius:16px;padding:26px;
  width:460px;max-width:95vw;animation:modalIn .2s ease}
@keyframes modalIn{from{opacity:0;transform:scale(.94)}to{opacity:1;transform:scale(1)}}
.modal-title{font-size:16px;font-weight:800;margin-bottom:20px}
.fg{margin-bottom:15px}
.flabel{font-size:10px;color:var(--text2);text-transform:uppercase;letter-spacing:.7px;margin-bottom:6px;display:block}
.fta{width:100%;padding:10px 13px;background:var(--s2);border:1px solid var(--border);
  border-radius:8px;color:var(--text);font-size:13px;outline:none;resize:vertical;min-height:80px}
.fta:focus{border-color:var(--green);box-shadow:0 0 0 2px var(--glow)}
.modal-acts{display:flex;gap:9px;justify-content:flex-end;margin-top:18px}

/* ── TOAST ── */
.toast{position:fixed;bottom:22px;right:22px;padding:11px 18px;border-radius:9px;
  font-size:12px;font-weight:600;z-index:9999;pointer-events:none;
  transform:translateY(60px);opacity:0;transition:all .28s ease;border:1px solid;max-width:320px}
.toast.show{transform:translateY(0);opacity:1}
.toast.ok{background:var(--glow);border-color:rgba(0,212,160,.4);color:var(--green)}
.toast.err{background:var(--rred);border-color:rgba(255,71,87,.4);color:var(--red)}
.toast.info{background:var(--rblue);border-color:rgba(79,172,254,.4);color:var(--blue)}

.empty-state{text-align:center;padding:40px;color:var(--text2)}
.empty-state .ei{font-size:34px;margin-bottom:10px;opacity:.3}
.empty-state .et{font-size:12px}
</style>
</head>
<body>

<!-- ═══ LOGIN ═══ -->
<div id="login">
  <div class="lc">
    <div class="lc-logo">
      <div class="lc-emblem">🏫</div>
      <div><div class="lc-name">Sally-Ann School</div><div class="lc-sub">WhatsApp Bot Admin Dashboard</div></div>
    </div>
    <div class="lc-label">Admin Password</div>
    <input class="lc-input" type="password" id="pw" placeholder="Enter password" onkeydown="if(event.key==='Enter')login()">
    <button class="lc-btn" onclick="login()">Access Dashboard →</button>
    <div class="lc-err" id="lerr"></div>
  </div>
</div>

<!-- ═══ APP ═══ -->
<div id="app">
  <!-- HEADER -->
  <div class="header">
    <div class="header-row">
      <div class="hdr-left">
        <div class="hdr-emblem">🏫</div>
        <div>
          <div class="hdr-title">Sally-Ann School</div>
          <div class="hdr-sub">WhatsApp Bot Admin Dashboard</div>
        </div>
      </div>
      <div class="hdr-right">
        <div class="unread-pill" id="unread-pill">0 unread</div>
        <div class="bot-pill on" id="bot-pill"><div class="bot-pill-dot"></div><span id="bot-pill-text">Bot online</span></div>
        <button class="pause-btn pause" id="pause-btn" onclick="toggleBot()">Pause bot</button>
        <button class="signout-btn" onclick="logout()">Sign out</button>
      </div>
    </div>
  </div>

  <!-- TABS -->
  <div class="tabbar">
    <div class="tab active" onclick="showPg('overview',this)">📊 Overview</div>
    <div class="tab" onclick="showPg('messages',this)">💬 Messages <span class="tab-badge" id="tab-badge">0</span></div>
    <div class="tab" onclick="showPg('broadcast',this)">📢 Broadcast</div>
    <div class="tab" onclick="showPg('botcontrols',this)">🎛️ Bot controls</div>
    <div class="tab" onclick="showPg('activity',this)">📋 Activity log</div>
  </div>

  <!-- ESCALATION ALERT BANNER -->
  <div class="escalation-banner" id="escalation-banner">
    <div class="esc-banner-inner" id="escalation-banner-inner"></div>
  </div>

  <div class="content">

    <!-- ═══ OVERVIEW ═══ -->
    <div class="pg active" id="pg-overview">
      <div class="metrics-grid">
        <div class="mc">
          <div class="mc-top green"></div>
          <div class="mc-label">Messages today</div>
          <div class="mc-val green" id="m-msgs-today">0</div>
          <div class="mc-foot" id="m-msgs-change">— vs yesterday</div>
        </div>
        <div class="mc">
          <div class="mc-top blue"></div>
          <div class="mc-label">Active parents</div>
          <div class="mc-val blue" id="m-active">0</div>
          <div class="mc-foot">Last 24 hours</div>
        </div>
        <div class="mc">
          <div class="mc-top green"></div>
          <div class="mc-label">Bot responses</div>
          <div class="mc-val green" id="m-bot-resp">0</div>
          <div class="mc-foot" id="m-bot-pct">0% handled by bot</div>
        </div>
        <div class="mc">
          <div class="mc-top purple"></div>
          <div class="mc-label">Human replies</div>
          <div class="mc-val purple" id="m-human">0</div>
          <div class="mc-foot">Admin overrides</div>
        </div>
        <div class="mc">
          <div class="mc-top blue"></div>
          <div class="mc-label">Avg. response</div>
          <div class="mc-val blue" id="m-avgresp">0s</div>
          <div class="mc-foot">Bot response time</div>
        </div>
        <div class="mc">
          <div class="mc-top amber"></div>
          <div class="mc-label">Pending queries</div>
          <div class="mc-val amber" id="m-pending">0</div>
          <div class="mc-foot">Need attention</div>
        </div>
      </div>

      <div class="two-col">
        <div class="card">
          <div class="ch"><span class="ct">Top query topics</span></div>
          <div id="topics-list"><div class="empty-state"><div class="ei">❓</div><div class="et">No data yet</div></div></div>
        </div>
        <div class="card">
          <div class="ch"><span class="ct">Message volume — last 7 days</span></div>
          <div class="week-chart" id="week-chart"></div>
        </div>
      </div>

      <div class="card">
        <div class="ch"><span class="ct">Recent activity</span><button class="btn btn-ghost btn-sm" onclick="loadOverview()">↻ Refresh</button></div>
        <div class="activity-list" id="recent-activity">
          <div class="empty-state"><div class="ei">📭</div><div class="et">No activity yet</div></div>
        </div>
      </div>
    </div>

    <!-- ═══ MESSAGES ═══ -->
    <div class="pg" id="pg-messages">
      <div class="conv-wrap">
        <div class="conv-left">
          <div class="conv-search"><input placeholder="Search parent..." id="conv-q" oninput="filterConvs(this.value)"></div>
          <div class="conv-scroll" id="conv-scroll"></div>
        </div>
        <div class="chat-right" id="chat-right">
          <div class="no-chat"><div class="no-chat-icon">💬</div><span style="font-size:13px">Select a conversation</span></div>
        </div>
      </div>
    </div>

    <!-- ═══ BROADCAST ═══ -->
    <div class="pg" id="pg-broadcast">
      <div class="card">
        <div class="ch"><span class="ct">📱 Send to Specific Number</span></div>
        <div class="bc-compose">
          <div style="margin-bottom:10px">
            <div class="flabel" style="margin-bottom:6px">Phone Number</div>
            <input id="direct-phone" class="finput" placeholder="e.g. 0712345678 or +254712345678">
          </div>
          <textarea class="bc-ta" id="direct-msg" placeholder="Type your message..." style="min-height:80px"></textarea>
        </div>
        <div class="bc-actions">
          <button class="btn btn-green" onclick="sendDirect()">📤 Send Message</button>
          <span class="bc-status" id="direct-status"></span>
        </div>
      </div>

      <div class="card">
        <div class="ch"><span class="ct">Compose Broadcast</span></div>
        <div class="bc-compose">
          <textarea class="bc-ta" id="bc-msg" placeholder="Type your message to parents here...

Example:
Dear Parent, please note that school fees for Term III 2026 are now due. Total: Ksh 17,000. Pay via M-Pesa Paybill 777643, Account: ADM number."></textarea>
          <div class="bc-meta">
            <span class="bc-count">📱 Will send to <strong id="bc-count" style="color:var(--text)">0</strong> parents</span>
            <span style="font-size:10px;color:var(--text3)">Shift+Enter for new line</span>
          </div>
        </div>
        <div class="bc-actions">
          <button class="btn btn-ghost" onclick="bcPreview()">👁️ Preview</button>
          <button class="btn btn-green" onclick="bcSend()">📢 Send to All Parents</button>
          <span class="bc-status" id="bc-status"></span>
        </div>
      </div>

      <div class="card">
        <div class="ch"><span class="ct">⚡ Quick Templates</span><button class="btn btn-ghost btn-sm" onclick="openTplModal()">+ Add</button></div>
        <div class="template-grid" id="tpl-bc"></div>
      </div>

      <div class="card">
        <div class="ch"><span class="ct">📋 Broadcast History</span></div>
        <div class="tbl-wrap"><table>
          <thead><tr><th>Sent At</th><th>Message</th><th>Recipients</th><th>Delivered</th><th>Failed</th></tr></thead>
          <tbody id="bc-hist-tbody"></tbody>
        </table></div>
      </div>
    </div>

    <!-- ═══ BOT CONTROLS ═══ -->
    <div class="pg" id="pg-botcontrols">
      <div class="toggle-card">
        <div class="toggle-info">
          <div class="tc-title" id="bc-toggle-title">🤖 Bot is Online</div>
          <div class="tc-sub" id="bc-toggle-sub">The bot is automatically replying to all new parent messages. Turn this off to pause all automatic replies — new messages will wait for a human reply.</div>
        </div>
        <label class="switch">
          <input type="checkbox" id="bot-switch" checked onchange="toggleBot()">
          <span class="slider"></span>
        </label>
      </div>

      <div class="card">
        <div class="ch"><span class="ct">⚡ Quick Reply Templates</span><button class="btn btn-ghost btn-sm" onclick="openTplModal()">+ Add</button></div>
        <div class="template-grid" id="tpl-settings"></div>
      </div>

      <div class="card">
        <div class="ch"><span class="ct">👤 Active Admin Takeovers</span></div>
        <div class="takeover-list" id="takeover-list">
          <div class="empty-state"><div class="ei">👤</div><div class="et">No conversations currently overridden by admin</div></div>
        </div>
      </div>
    </div>

    <!-- ═══ ACTIVITY LOG ═══ -->
    <div class="pg" id="pg-activity">
      <div class="card">
        <div class="ch"><span class="ct">📋 Full Activity Log</span><button class="btn btn-ghost btn-sm" onclick="loadActivityLog()">↻ Refresh</button></div>
        <div class="activity-list" id="activity-log-list">
          <div class="empty-state"><div class="ei">📭</div><div class="et">No activity yet</div></div>
        </div>
      </div>
    </div>

  </div>
</div>

<!-- TEMPLATE MODAL -->
<div class="modal-ov" id="tpl-modal">
  <div class="modal">
    <div class="modal-title">⚡ Add Template</div>
    <div class="fg"><label class="flabel">Title</label><input class="finput" id="tpl-title" placeholder="e.g. Fee Reminder"></div>
    <div class="fg"><label class="flabel">Message Body</label><textarea class="fta" id="tpl-body" rows="5" placeholder="Type the message template..."></textarea></div>
    <div class="modal-acts">
      <button class="btn btn-ghost" onclick="closeModal('tpl-modal')">Cancel</button>
      <button class="btn btn-green" onclick="saveTpl()">Save Template</button>
    </div>
  </div>
</div>

<!-- PREVIEW MODAL -->
<div class="modal-ov" id="prev-modal">
  <div class="modal">
    <div class="modal-title">👁️ Broadcast Preview</div>
    <div style="background:var(--s2);border-radius:8px;padding:15px;font-size:13px;line-height:1.6;
      white-space:pre-wrap;max-height:220px;overflow-y:auto;border:1px solid var(--border);margin-bottom:14px" id="prev-body"></div>
    <div style="font-size:12px;color:var(--text2);margin-bottom:16px">📱 Will be sent to <strong style="color:var(--text)" id="prev-count">0</strong> parents</div>
    <div class="modal-acts">
      <button class="btn btn-ghost" onclick="closeModal('prev-modal')">Cancel</button>
      <button class="btn btn-green" onclick="bcConfirm()">✓ Confirm & Send</button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
'use strict';
const API='';
let convData={}, selPhone=null, allTpl=[], convTimer=null, overviewTimer=null, escalationTimer=null;
const AVATAR_COLORS=['#00d4a0','#4facfe','#a78bfa','#f5a623','#ff6b9d','#38bdf8','#fb923c','#34d399'];

/* ── AUTH ── */
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

/* ── BOOT ── */
function boot(){
  loadOverview();
  loadTpl();
  loadBotStatus();
  loadEscalations();
  overviewTimer=setInterval(loadOverview,8000);
  escalationTimer=setInterval(loadEscalations,6000);
}

/* ── NAV ── */
function showPg(name,el){
  document.querySelectorAll('.pg').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(n=>n.classList.remove('active'));
  document.getElementById('pg-'+name).classList.add('active');
  el.classList.add('active');
  if(name==='overview')    loadOverview();
  if(name==='messages')    loadConvs();
  if(name==='broadcast')   loadBcPage();
  if(name==='botcontrols') { loadBotStatus(); renderTplSettings(); loadTakeovers(); }
  if(name==='activity')    loadActivityLog();
}

/* ── BOT STATUS / PAUSE ── */
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
    title.textContent='⏸️ Bot is Paused';
    sub.textContent='The bot is NOT replying automatically. All new parent messages are waiting for a human reply. Turn this on to resume automatic replies.';
  } else {
    pill.className='bot-pill on';
    pillText.textContent='Bot online';
    pauseBtn.className='pause-btn pause';
    pauseBtn.textContent='Pause bot';
    sw.checked=true;
    title.textContent='🤖 Bot is Online';
    sub.textContent='The bot is automatically replying to all new parent messages. Turn this off to pause all automatic replies — new messages will wait for a human reply.';
  }
}

async function toggleBot(){
  const r=await fetch(API+'/admin/bot/toggle',{method:'POST'});
  const d=await r.json();
  applyBotStatus(d.paused);
  toast(d.paused?'Bot paused — replies now require admin action':'Bot resumed — automatic replies active', d.paused?'info':'ok');
}

/* ── ESCALATIONS ── */
let lastEscalationCount=0;
async function loadEscalations(){
  const r=await fetch(API+'/admin/escalations');
  if(!r.ok)return;
  const items=await r.json();
  renderEscalationBanner(items);

  // Tab badge with pulse if there are escalations
  const tb=document.getElementById('tab-badge');
  const unreadCount=parseInt(tb.textContent)||0;
  if(items.length>0){
    tb.classList.add('esc-badge');
  } else {
    tb.classList.remove('esc-badge');
  }

  // Sound/toast on NEW escalation (count increased)
  if(items.length>lastEscalationCount && lastEscalationCount!==0){
    toast(`🚨 New escalation: ${items[0].name||items[0].phone}`,'err');
  } else if(items.length>0 && lastEscalationCount===0){
    toast(`🚨 ${items.length} conversation${items.length>1?'s':''} need${items.length===1?'s':''} your attention`,'err');
  }
  lastEscalationCount=items.length;
}

function renderEscalationBanner(items){
  const banner=document.getElementById('escalation-banner');
  const inner=document.getElementById('escalation-banner-inner');
  if(!items.length){
    banner.classList.remove('show');
    inner.innerHTML='';
    return;
  }
  banner.classList.add('show');
  inner.innerHTML=`
    <span class="esc-banner-icon">🚨</span>
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
  // Switch to Messages tab and open this conversation
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

/* ── OVERVIEW ── */
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

  // Unread pill + tab badge
  const u=d.unread||0;
  const up=document.getElementById('unread-pill');
  up.textContent=u+' unread'; up.style.display=u>0?'inline-flex':'none';
  const tb=document.getElementById('tab-badge');
  tb.textContent=u; tb.style.display=u>0?'inline':'none';

  // Bot pill (in case toggled elsewhere)
  applyBotStatus(d.bot_paused);

  // Topics
  if(d.topics&&d.topics.length){
    const colors={'School fees & payment':'var(--green)','Bus routes & fares':'var(--blue)',
      'Educational trips':'var(--purple)','Parental engagement':'var(--amber)',
      'ICT programme':'#ff6b9d','Other':'var(--text3)'};
    document.getElementById('topics-list').innerHTML=d.topics.map(([name,pct])=>`
      <div class="topic-row">
        <div class="topic-head"><span class="topic-name">${name}</span><span class="topic-pct">${pct}%</span></div>
        <div class="topic-bar-bg"><div class="topic-bar-fill" style="width:${pct}%;background:${colors[name]||'var(--green)'}"></div></div>
      </div>`).join('');
  }

  // Weekly chart
  if(d.weekly&&d.weekly.length){
    const mx=Math.max(...d.weekly.map(w=>w[1]),1);
    document.getElementById('week-chart').innerHTML=d.weekly.map(([lbl,val])=>`
      <div class="week-col">
        <div class="week-bar-val">${val}</div>
        <div class="week-bar" style="height:${Math.max((val/mx)*100,4)}px"></div>
        <div class="week-lbl">${lbl}</div>
      </div>`).join('');
  }

  // Recent activity (top 8)
  const ar=await fetch(API+'/admin/activity');
  if(ar.ok){
    const items=await ar.json();
    renderActivityList('recent-activity', items.slice(0,8));
  }
}

/* ── ACTIVITY LOG (full) ── */
async function loadActivityLog(){
  const r=await fetch(API+'/admin/activity');
  const items=await r.json();
  renderActivityList('activity-log-list', items);
}

function renderActivityList(elId, items){
  const el=document.getElementById(elId);
  if(!items.length){
    el.innerHTML='<div class="empty-state"><div class="ei">📭</div><div class="et">No activity yet</div></div>';
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

/* ── CONVERSATIONS / MESSAGES ── */
async function loadConvs(){
  const r=await fetch(API+'/admin/conversations');
  convData=await r.json();
  renderConvList(convData);
  if(selPhone) renderChat(selPhone);
  if(convTimer) clearInterval(convTimer);
  convTimer=setInterval(async()=>{
    const r2=await fetch(API+'/admin/conversations');
    convData=await r2.json();
    renderConvList(convData);
    if(selPhone) renderChat(selPhone);
  },8000);
}

function renderConvList(data){
  const el=document.getElementById('conv-scroll');
  const phones=Object.keys(data);
  if(!phones.length){
    el.innerHTML='<div class="empty-state"><div class="ei">💬</div><div class="et">No conversations yet</div></div>';
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
    const log=c.full_log||[];
    const last=log.length?log[log.length-1]:null;
    const preview=last?esc(last.message).substring(0,48)+'…':'No messages';
    const t=c.last_seen?new Date(c.last_seen).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}):'';
    const name=c.name||ph.replace('whatsapp:','');
    const statusTag=c.escalated?'<span class="tag tag-escalated">🚨 Needs attention</span>'
      :c.status==='pending'?'<span class="tag tag-amber">⏳ Pending</span>'
      :c.status==='override'?'<span class="tag tag-purple">👤 Admin</span>'
      :'<span class="tag tag-green">✓ Resolved</span>';
    return `<div class="cv-item ${selPhone===ph?'active':''} ${c.escalated?'cv-escalated':''}" onclick="selConv('${ph}')">
      <div class="cv-header"><span class="cv-name">${esc(name)}</span><span class="cv-time">${t}</span></div>
      <div class="cv-preview">${preview}</div>
      <div class="cv-tags">${statusTag}<span class="tag tag-blue">${c.message_count||0} msgs</span></div>
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

async function selConv(ph){
  selPhone=ph;
  renderConvList(convData);
  renderChat(ph);
  await fetch(API+'/admin/conversations/'+encodeURIComponent(ph)+'/read',{method:'POST'});
  if(convData[ph]) convData[ph].unread=0;
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
  }).join(''):'<div class="empty-state"><div class="ei">💬</div><div class="et">No messages yet</div></div>';

  const qrBtns=allTpl.slice(0,5).map(t=>`<button class="qr-btn" onclick="useQR('${ph}',${t.id})">⚡ ${esc(t.title)}</button>`).join('');

  panel.innerHTML=`
    <div class="chat-head">
      <div class="chat-head-info">
        <div class="cname">${esc(name)} ${c.escalated?'<span class="tag tag-escalated" style="margin-left:6px">🚨 Escalated</span>':''}</div>
        <div class="cstatus">${ph.replace('whatsapp:','')} · ${isTa?'🔴 Admin control':'🤖 Bot handling'} · ${c.message_count||0} messages</div>
      </div>
      <div>
        ${isTa
          ?`<button class="btn btn-green btn-sm" onclick="relConv('${ph}')">🤖 Return to Bot</button>`
          :`<button class="btn btn-amber btn-sm" onclick="taConv('${ph}')">👤 Take Over</button>`}
      </div>
    </div>
    ${c.escalated?`<div class="chat-escalation-notice">🚨 Escalation reason: ${esc(c.escalation_reason||'Needs admin attention')}</div>`:''}
    <div class="chat-msgs" id="chat-msgs">${msgsHtml}</div>
    ${isTa?`
      <div class="chat-takeover-notice">⚡ You are in control — bot is paused for this conversation</div>
      <div class="chat-foot">
        ${qrBtns?`<div class="qr-bar">${qrBtns}</div>`:''}
        <div class="chat-input-row">
          <textarea class="chat-ta" id="adm-input" rows="2" placeholder="Type your reply... (Enter to send, Shift+Enter for new line)"
            onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();sendAdm('${ph}')}"></textarea>
          <button class="btn btn-green" onclick="sendAdm('${ph}')">Send</button>
        </div>
      </div>`
    :`<div class="chat-bot-foot">
        <span class="chat-bot-note">🤖 Bot is handling this conversation</span>
        <button class="btn btn-amber btn-sm" onclick="taConv('${ph}')">👤 Take Over to Reply</button>
      </div>`}`;

  const msgs=document.getElementById('chat-msgs');
  if(msgs) msgs.scrollTop=msgs.scrollHeight;
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

/* ── BROADCAST ── */
async function sendDirect(){
  const phone=document.getElementById('direct-phone').value.trim();
  const msg=document.getElementById('direct-msg').value.trim();
  const st=document.getElementById('direct-status');
  if(!phone){toast('Please enter a phone number','err');return;}
  if(!msg){toast('Please type a message','err');return;}
  st.textContent='⏳ Sending...';
  const r=await fetch(API+'/admin/send-direct',{
    method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({phone,message:msg})
  });
  const d=await r.json();
  if(r.ok&&d.success){
    st.textContent='✅ Sent to '+d.phone.replace('whatsapp:','');
    document.getElementById('direct-msg').value='';
    document.getElementById('direct-phone').value='';
    toast('Message sent successfully','ok');
  } else {
    st.textContent='❌ Failed to send';
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
  if(!hist.length){tb.innerHTML='<tr><td colspan="5"><div class="empty-state"><div class="ei">📢</div><div class="et">No broadcasts sent yet</div></div></td></tr>';return;}
  tb.innerHTML=hist.map(b=>`<tr>
    <td style="white-space:nowrap;color:var(--text2);font-size:11px">${new Date(b.timestamp).toLocaleString()}</td>
    <td style="max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px">${esc(b.message).substring(0,80)}</td>
    <td style="font-size:12px">${b.recipients.length}</td>
    <td style="color:var(--green);font-weight:700;font-size:12px">${b.sent}</td>
    <td style="color:var(--red);font-size:12px">${b.failed}</td>
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
  st.textContent='⏳ Sending...';
  const r=await fetch(API+'/admin/broadcast',{
    method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:msg})
  });
  const d=await r.json();
  if(r.ok){
    st.textContent=`✅ Sent to ${d.sent} parents${d.failed>0?' ('+d.failed+' failed)':''}`;
    document.getElementById('bc-msg').value='';
    toast(`Broadcast sent to ${d.sent} parents`,'ok');
    loadBcPage();
  } else {st.textContent='❌ Failed';toast('Broadcast failed','err');}
}

/* ── TEMPLATES ── */
async function loadTpl(){
  const r=await fetch(API+'/admin/quick-replies');
  allTpl=await r.json();
}

function renderTplSettings(){
  const el=document.getElementById('tpl-settings');
  if(!allTpl.length){el.innerHTML='<div class="empty-state"><div class="ei">⚡</div><div class="et">No templates yet. Add one above.</div></div>';return;}
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
  if(!allTpl.length){el.innerHTML='<div class="empty-state"><div class="ei">⚡</div><div class="et">No templates yet.</div></div>';return;}
  el.innerHTML=allTpl.map(t=>`
    <div class="tpl-card" onclick="useTplBc(${JSON.stringify(t.body)})">
      <div class="tpl-title">⚡ ${esc(t.title)}</div>
      <div class="tpl-body">${esc(t.body)}</div>
      <div class="tpl-hint">Click to use in compose →</div>
    </div>`).join('');
}

function useTplBc(body){document.getElementById('bc-msg').value=body;toast('Template loaded','info');}

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

/* ── BOT CONTROLS: TAKEOVERS ── */
async function loadTakeovers(){
  const r=await fetch(API+'/admin/bot/status');
  const d=await r.json();
  const el=document.getElementById('takeover-list');
  if(!d.takeovers||!d.takeovers.length){
    el.innerHTML='<div class="empty-state"><div class="ei">👤</div><div class="et">No conversations currently overridden by admin</div></div>';
    return;
  }
  el.innerHTML=d.takeovers.map(ph=>`
    <div class="takeover-item">
      <div>
        <div class="takeover-phone">${ph.replace('whatsapp:','')}</div>
        <div class="takeover-meta">Admin is currently handling this conversation</div>
      </div>
      <button class="btn btn-green btn-sm" onclick="releaseFromControls('${ph}')">🤖 Return to Bot</button>
    </div>`).join('');
}

async function releaseFromControls(ph){
  await fetch(API+'/admin/conversations/'+encodeURIComponent(ph)+'/release',{method:'POST'});
  toast('Returned to bot control','info');
  loadTakeovers();
}

/* ── MODALS / UTILS ── */
function closeModal(id){document.getElementById(id).classList.remove('open');}

function toast(msg,type='ok'){
  const t=document.getElementById('toast');
  const icons={ok:'✅',err:'❌',info:'ℹ️'};
  t.textContent=(icons[type]||'•')+' '+msg;
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
</html>"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)