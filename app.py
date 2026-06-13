import os
import json
import logging
import hashlib
import hmac
import requests
from datetime import datetime, timedelta
from collections import defaultdict
from flask import Flask, request, jsonify, session, render_template_string
from groq import Groq
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "sallyann-secret-2026")

# ── Env vars ───────────────────────────────────────────────────────────────────
GROQ_API_KEY    = os.getenv("GROQ_API_KEY", "")
WHATSAPP_TOKEN  = os.getenv("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")
VERIFY_TOKEN    = os.getenv("VERIFY_TOKEN", "")
GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY", "")
APP_SECRET      = os.getenv("APP_SECRET", "")
ADMIN_PASSWORD  = os.getenv("ADMIN_PASSWORD", "sallyann2026")

groq_client = Groq(api_key=GROQ_API_KEY)

# ── In-memory stores ───────────────────────────────────────────────────────────
conversation_history = {}   # {phone: [{role, content}]}
message_log          = []   # [{phone, message, direction, timestamp, read}]
active_users         = {}   # {phone: {last_seen, name, grade}}
admin_takeover       = set()
broadcast_log        = []   # [{message, timestamp, recipients, sent_by}]
faq_counter          = defaultdict(int)
quick_replies        = [    # Admin-defined quick replies
    {"id": 1, "title": "Fee reminder",    "body": "Dear Parent, kindly note that school fees for Term II 2026 are due. Total: Ksh 17,000. Pay via M-Pesa Paybill 777643, Account: ADM number. Minimum 60% on Reporting Day."},
    {"id": 2, "title": "Trip reminder",   "body": "Dear Parent, please note that educational trip fees are due. Kindly pay promptly to secure your child's spot."},
    {"id": 3, "title": "Meeting notice",  "body": "Dear Parent, you are invited to the upcoming Parental Engagement Day. Please check the school calendar for your child's grade date."},
    {"id": 4, "title": "Half term",       "body": "Dear Parent, Half Term holiday runs from 24th to 28th June 2026. School resumes on Monday 30th June 2026."},
]

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

RULES: Reply in same language as parent (English/Swahili). Max 3 sentences. Never make up info. If unsure, say call school office.
"""

FAQ_KEYWORDS = ["fee","ada","pay","mpesa","bus","basi","trip","safari","meeting","ict",
                "holiday","likizo","admission","uniform","grade","class","result","exam"]

# ── Helpers ────────────────────────────────────────────────────────────────────
def log_message(phone, message, direction="inbound"):
    message_log.append({
        "phone": phone, "message": message,
        "direction": direction,
        "timestamp": datetime.now().isoformat(),
        "read": direction == "outbound",
    })
    if len(message_log) > 1000:
        message_log.pop(0)
    if direction == "inbound":
        for kw in FAQ_KEYWORDS:
            if kw in message.lower():
                faq_counter[kw] += 1

def get_history(phone):
    return conversation_history.get(phone, [])

def save_history(phone, user_msg, bot_reply):
    if phone not in conversation_history:
        conversation_history[phone] = []
    conversation_history[phone].append({"role": "user",      "content": user_msg})
    conversation_history[phone].append({"role": "assistant", "content": bot_reply})
    if len(conversation_history[phone]) > 20:
        conversation_history[phone] = conversation_history[phone][-20:]

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
    history = get_history(phone)
    messages = [{"role": "system", "content": SCHOOL_CONTEXT}]
    messages.extend(history)
    messages.append({"role": "user", "content": message})
    try:
        reply = ask_groq(messages)
        logger.info(f"[{phone}] Groq OK")
        save_history(phone, message, reply)
        return reply
    except Exception as e:
        logger.error(f"[{phone}] Groq error: {e}")
    try:
        reply = ask_gemini(message, history)
        logger.info(f"[{phone}] Gemini fallback OK")
        save_history(phone, message, reply)
        return reply
    except Exception as e:
        logger.error(f"[{phone}] Gemini error: {e}")
    return "Sorry, I'm having trouble right now. Please call the school office directly."

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

def verify_signature(req):
    if not APP_SECRET:
        return True
    signature = req.headers.get("X-Hub-Signature-256", "")
    if not signature.startswith("sha256="):
        return False
    expected = hmac.new(APP_SECRET.encode(), req.data, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)

def mark_messages_read(phone):
    for msg in message_log:
        if msg["phone"] == phone and msg["direction"] == "inbound":
            msg["read"] = True

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
        message  = data["entry"][0]["changes"][0]["value"]["messages"][0]
        phone    = message["from"]
        msg_type = message["type"]
        logger.info(f"[{phone}] Type: {msg_type}")

        # Update active users
        active_users[phone] = {"last_seen": datetime.now().isoformat(), "phone": phone}

        if msg_type == "image":
            reply = ("Thank you for sending your payment receipt! 📸\n"
                     "Our office will confirm within 24 hours.\n"
                     "For instant confirmation call the school office.")
            log_message(phone, "[Image received]", "inbound")
            log_message(phone, reply, "outbound")
            send_whatsapp(phone, reply)
            return jsonify({"status": "ok"}), 200

        if msg_type != "text":
            send_whatsapp(phone, "Sorry, I can only handle text messages for now.")
            return jsonify({"status": "ok"}), 200

        incoming = message["text"]["body"].strip()
        log_message(phone, incoming, "inbound")

        # Admin takeover — bot silent
        if phone in admin_takeover:
            return jsonify({"status": "ok"}), 200

        reply, use_ai = find_keyword_response(incoming)
        if use_ai:
            reply = ask_ai(phone, incoming)

        log_message(phone, reply, "outbound")
        send_whatsapp(phone, reply)

    except (KeyError, IndexError) as e:
        logger.warning(f"Webhook parse error: {e}")

    return jsonify({"status": "ok"}), 200

# ══════════════════════════════════════════════════════════════════════════════
# ADMIN API
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

@app.route("/admin/metrics")
@admin_required
def get_metrics():
    now = datetime.now()
    active_1h  = sum(1 for u in active_users.values()
                     if (now - datetime.fromisoformat(u["last_seen"])) < timedelta(hours=1))
    active_24h = sum(1 for u in active_users.values()
                     if (now - datetime.fromisoformat(u["last_seen"])) < timedelta(hours=24))
    inbound  = [m for m in message_log if m["direction"] == "inbound"]
    outbound = [m for m in message_log if m["direction"] == "outbound"]
    unread   = sum(1 for m in inbound if not m.get("read"))
    # Messages per hour (last 24h)
    hourly = defaultdict(int)
    for m in inbound:
        try:
            hour = datetime.fromisoformat(m["timestamp"]).strftime("%H:00")
            if (now - datetime.fromisoformat(m["timestamp"])) < timedelta(hours=24):
                hourly[hour] += 1
        except: pass
    top_faqs = sorted(faq_counter.items(), key=lambda x: x[1], reverse=True)[:8]
    return jsonify({
        "total_users":    len(active_users),
        "active_1h":      active_1h,
        "active_24h":     active_24h,
        "total_messages": len(message_log),
        "inbound":        len(inbound),
        "outbound":       len(outbound),
        "unread":         unread,
        "admin_takeovers": len(admin_takeover),
        "broadcasts_sent": len(broadcast_log),
        "top_faqs":       top_faqs,
        "hourly":         dict(sorted(hourly.items())),
    })

@app.route("/admin/messages")
@admin_required
def get_messages():
    limit = int(request.args.get("limit", 200))
    phone = request.args.get("phone")
    if phone:
        msgs = [m for m in message_log if m["phone"] == phone]
    else:
        msgs = message_log[-limit:]
    return jsonify(list(reversed(msgs)))

@app.route("/admin/conversations")
@admin_required
def get_conversations():
    result = {}
    for phone in active_users:
        msgs = [m for m in message_log if m["phone"] == phone]
        unread = sum(1 for m in msgs if m["direction"] == "inbound" and not m.get("read"))
        last_msg = msgs[-1] if msgs else None
        result[phone] = {
            "phone": phone,
            "last_seen": active_users[phone]["last_seen"],
            "messages": conversation_history.get(phone, []),
            "full_log": msgs[-50:],
            "unread": unread,
            "admin_takeover": phone in admin_takeover,
            "message_count": len(msgs),
        }
    return jsonify(result)

@app.route("/admin/conversations/<path:phone>/takeover", methods=["POST"])
@admin_required
def takeover(phone):
    admin_takeover.add(phone)
    mark_messages_read(phone)
    return jsonify({"success": True})

@app.route("/admin/conversations/<path:phone>/release", methods=["POST"])
@admin_required
def release(phone):
    admin_takeover.discard(phone)
    return jsonify({"success": True})

@app.route("/admin/conversations/<path:phone>/send", methods=["POST"])
@admin_required
def admin_send(phone):
    data = request.get_json()
    message = data.get("message", "").strip()
    if not message:
        return jsonify({"error": "Message required"}), 400
    log_message(phone, f"[ADMIN] {message}", "outbound")
    if phone not in conversation_history:
        conversation_history[phone] = []
    conversation_history[phone].append({"role": "assistant", "content": message})
    send_whatsapp(phone, message)
    return jsonify({"success": True})

@app.route("/admin/conversations/<path:phone>/read", methods=["POST"])
@admin_required
def mark_read(phone):
    mark_messages_read(phone)
    return jsonify({"success": True})

@app.route("/admin/broadcast", methods=["POST"])
@admin_required
def broadcast():
    data     = request.get_json()
    message  = data.get("message", "").strip()
    phones   = data.get("phones", list(active_users.keys()))
    if not message:
        return jsonify({"error": "Message required"}), 400
    sent, failed = 0, 0
    for phone in phones:
        if send_whatsapp(phone, message):
            log_message(phone, f"[BROADCAST] {message}", "outbound")
            sent += 1
        else:
            failed += 1
    broadcast_log.append({
        "message": message, "recipients": phones,
        "sent": sent, "failed": failed,
        "timestamp": datetime.now().isoformat(),
    })
    return jsonify({"success": True, "sent": sent, "failed": failed})

@app.route("/admin/broadcast/history")
@admin_required
def broadcast_history():
    return jsonify(list(reversed(broadcast_log)))

@app.route("/admin/quick-replies", methods=["GET"])
@admin_required
def get_quick_replies():
    return jsonify(quick_replies)

@app.route("/admin/quick-replies", methods=["POST"])
@admin_required
def add_quick_reply():
    data = request.get_json()
    new_id = max((q["id"] for q in quick_replies), default=0) + 1
    quick_replies.append({"id": new_id, "title": data.get("title"), "body": data.get("body")})
    return jsonify({"success": True})

@app.route("/admin/quick-replies/<int:qid>", methods=["DELETE"])
@admin_required
def del_quick_reply(qid):
    global quick_replies
    quick_replies = [q for q in quick_replies if q["id"] != qid]
    return jsonify({"success": True})

@app.route("/admin/users")
@admin_required
def get_users():
    return jsonify(list(active_users.values()))

# ══════════════════════════════════════════════════════════════════════════════
# ADMIN DASHBOARD HTML
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
/* ── TOKENS ── */
:root{
  --bg:#060d18;--s1:#0b1628;--s2:#101e35;--s3:#162540;
  --border:#1c2e48;--border2:#243b58;
  --green:#00d4a0;--green2:#00b085;--glow:rgba(0,212,160,.15);
  --amber:#f5a623;--red:#ff4757;--blue:#4facfe;--purple:#a78bfa;--sky:#38bdf8;
  --text:#dce8f5;--text2:#6b8aab;--text3:#3d5a7a;
  --font:'Segoe UI',system-ui,-apple-system,sans-serif;
  --r:10px;--r2:14px;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:var(--font);min-height:100vh;overflow-x:hidden}
::-webkit-scrollbar{width:4px;height:4px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--border2);border-radius:2px}

/* ── LOGIN ── */
#login{display:flex;align-items:center;justify-content:center;min-height:100vh;
  background:radial-gradient(ellipse 60% 50% at 30% 60%,rgba(0,212,160,.07),transparent),var(--bg)}
.lc{width:380px}
.lc-logo{display:flex;align-items:center;gap:14px;margin-bottom:32px}
.lc-emblem{width:52px;height:52px;background:var(--glow);border:1.5px solid var(--green);
  border-radius:14px;display:flex;align-items:center;justify-content:center;font-size:24px}
.lc-name{font-size:18px;font-weight:800;line-height:1.2}
.lc-sub{font-size:11px;color:var(--text2);text-transform:uppercase;letter-spacing:2px;margin-top:2px}
.lc-label{font-size:11px;color:var(--text2);margin-bottom:7px;text-transform:uppercase;letter-spacing:.8px}
.lc-input{width:100%;padding:13px 16px;background:var(--s2);border:1px solid var(--border);
  border-radius:var(--r);color:var(--text);font-size:15px;outline:none;transition:border .2s;margin-bottom:12px}
.lc-input:focus{border-color:var(--green);box-shadow:0 0 0 3px var(--glow)}
.lc-btn{width:100%;padding:13px;background:var(--green);color:#000;border:none;
  border-radius:var(--r);font-size:15px;font-weight:800;cursor:pointer;letter-spacing:.3px;
  transition:background .2s,transform .1s}
.lc-btn:hover{background:var(--green2)}
.lc-btn:active{transform:scale(.98)}
.lc-err{color:var(--red);font-size:12px;margin-top:10px;min-height:18px}

/* ── SHELL ── */
#app{display:none;height:100vh;flex-direction:column}

/* ── TOPBAR ── */
.topbar{height:56px;background:var(--s1);border-bottom:1px solid var(--border);
  display:flex;align-items:center;justify-content:space-between;padding:0 18px;
  position:sticky;top:0;z-index:300;flex-shrink:0}
.tb-l{display:flex;align-items:center;gap:12px}
.tb-emblem{width:32px;height:32px;background:var(--glow);border:1px solid var(--green);
  border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:15px}
.tb-school{font-size:14px;font-weight:800;letter-spacing:.2px}
.tb-tag{font-size:10px;color:var(--text2);text-transform:uppercase;letter-spacing:1.5px}
.tb-r{display:flex;align-items:center;gap:10px}
.live-pill{display:flex;align-items:center;gap:6px;padding:4px 10px;
  background:var(--glow);border:1px solid rgba(0,212,160,.3);border-radius:20px}
.live-dot{width:6px;height:6px;background:var(--green);border-radius:50%;animation:blink 1.4s infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.2}}
.live-txt{font-size:10px;color:var(--green);font-weight:700;letter-spacing:.5px}
.unread-pill{background:var(--red);color:#fff;border-radius:20px;padding:3px 9px;
  font-size:11px;font-weight:800;display:none;animation:pop .3s ease}
@keyframes pop{from{transform:scale(.6)}to{transform:scale(1)}}
.tb-signout{padding:6px 14px;background:transparent;border:1px solid var(--border);
  color:var(--text2);border-radius:7px;cursor:pointer;font-size:12px;transition:all .2s}
.tb-signout:hover{border-color:var(--red);color:var(--red)}

/* ── BODY ── */
.body-layout{display:flex;flex:1;overflow:hidden}

/* ── SIDEBAR ── */
.sidebar{width:200px;background:var(--s1);border-right:1px solid var(--border);
  display:flex;flex-direction:column;padding:12px 0;overflow-y:auto;flex-shrink:0}
.nav-sep{font-size:9px;text-transform:uppercase;letter-spacing:2px;color:var(--text3);
  padding:14px 16px 5px;font-weight:600}
.ni{display:flex;align-items:center;gap:9px;padding:9px 16px;cursor:pointer;
  color:var(--text2);font-size:13px;border-left:2px solid transparent;
  transition:all .15s;position:relative}
.ni:hover{background:var(--s2);color:var(--text)}
.ni.active{background:var(--glow);color:var(--green);border-left-color:var(--green)}
.ni-icon{font-size:15px;width:18px;text-align:center}
.ni-badge{margin-left:auto;background:var(--red);color:#fff;border-radius:20px;
  padding:1px 6px;font-size:10px;font-weight:800;display:none}

/* ── CONTENT ── */
.content{flex:1;overflow-y:auto;background:var(--bg)}
.pg{display:none;padding:22px}.pg.active{display:block}
.ph{margin-bottom:22px}
.ph-title{font-size:19px;font-weight:900;letter-spacing:.2px}
.ph-sub{font-size:12px;color:var(--text2);margin-top:3px}

/* ── METRICS ── */
.metrics-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(155px,1fr));gap:12px;margin-bottom:22px}
.mc{background:var(--s1);border:1px solid var(--border);border-radius:var(--r2);padding:18px;
  position:relative;overflow:hidden;transition:border-color .2s}
.mc:hover{border-color:var(--border2)}
.mc-glow{position:absolute;top:0;left:0;right:0;height:2px}
.mc-glow.teal{background:linear-gradient(90deg,var(--green),var(--green2))}
.mc-glow.amber{background:var(--amber)}
.mc-glow.red{background:var(--red)}
.mc-glow.blue{background:var(--blue)}
.mc-glow.purple{background:var(--purple)}
.mc-glow.sky{background:var(--sky)}
.mc-icon{font-size:20px;margin-bottom:12px}
.mc-val{font-size:32px;font-weight:900;line-height:1;letter-spacing:-1px}
.mc-val.teal{color:var(--green)}
.mc-val.amber{color:var(--amber)}
.mc-val.red{color:var(--red)}
.mc-val.blue{color:var(--blue)}
.mc-val.purple{color:var(--purple)}
.mc-val.sky{color:var(--sky)}
.mc-label{font-size:10px;color:var(--text2);text-transform:uppercase;letter-spacing:.7px;margin-top:5px}

/* ── CARDS ── */
.card{background:var(--s1);border:1px solid var(--border);border-radius:var(--r2);padding:18px;margin-bottom:14px}
.ch{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px}
.ct{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:var(--text2)}
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:14px}

/* ── LIVE FEED ── */
.feed{display:flex;flex-direction:column;gap:6px;max-height:300px;overflow-y:auto}
.fi{display:flex;align-items:flex-start;gap:10px;padding:10px 12px;
  background:var(--s2);border:1px solid var(--border);border-radius:8px;
  animation:slideIn .25s ease}
@keyframes slideIn{from{opacity:0;transform:translateX(-8px)}to{opacity:1;transform:translateX(0)}}
.fi-dot{width:7px;height:7px;border-radius:50%;margin-top:4px;flex-shrink:0}
.fi-dot.in{background:var(--green)}
.fi-dot.out{background:var(--blue)}
.fi-body{flex:1;min-width:0}
.fi-phone{font-size:10px;color:var(--text2);margin-bottom:2px}
.fi-msg{font-size:12px;line-height:1.4;word-break:break-word}
.fi-time{font-size:9px;color:var(--text3);flex-shrink:0;margin-top:2px}

/* ── FAQ ── */
.faq-row{display:flex;justify-content:space-between;align-items:center;font-size:12px;margin-bottom:5px}
.faq-bar-bg{height:4px;background:var(--s3);border-radius:2px;margin-bottom:10px;overflow:hidden}
.faq-bar-fill{height:100%;background:linear-gradient(90deg,var(--green),var(--blue));border-radius:2px;transition:width .7s ease}

/* ── ACTIVITY CHART ── */
.act-chart{display:flex;align-items:flex-end;gap:5px;height:72px}
.act-col{flex:1;display:flex;flex-direction:column;align-items:center;gap:4px}
.act-bar{width:100%;background:var(--glow);border:1px solid rgba(0,212,160,.3);
  border-radius:3px 3px 0 0;min-height:3px;transition:height .5s ease;position:relative}
.act-bar:hover::after{content:attr(data-tip);position:absolute;bottom:calc(100%+4px);left:50%;
  transform:translateX(-50%);background:var(--s3);color:var(--text);
  padding:3px 7px;border-radius:4px;font-size:10px;white-space:nowrap;pointer-events:none}
.act-lbl{font-size:8px;color:var(--text3);text-align:center}

/* ── CONVERSATIONS ── */
.conv-wrap{display:grid;grid-template-columns:270px 1fr;border:1px solid var(--border);
  border-radius:var(--r2);overflow:hidden;height:calc(100vh - 170px)}
.conv-left{background:var(--s1);border-right:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden}
.conv-search{padding:10px;border-bottom:1px solid var(--border)}
.conv-search input{width:100%;padding:8px 12px;background:var(--s2);border:1px solid var(--border);
  border-radius:7px;color:var(--text);font-size:12px;outline:none}
.conv-search input:focus{border-color:var(--green)}
.conv-scroll{flex:1;overflow-y:auto}
.cv-item{padding:12px 14px;border-bottom:1px solid var(--border);cursor:pointer;
  transition:background .12s;position:relative}
.cv-item:hover{background:var(--s2)}
.cv-item.active{background:var(--glow);border-left:2px solid var(--green)}
.cv-header{display:flex;justify-content:space-between;margin-bottom:3px}
.cv-phone{font-size:12px;font-weight:700}
.cv-time{font-size:9px;color:var(--text2)}
.cv-preview{font-size:11px;color:var(--text2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cv-tags{display:flex;gap:5px;margin-top:5px}
.tag{display:inline-flex;align-items:center;gap:3px;padding:2px 7px;border-radius:20px;font-size:9px;font-weight:700}
.tag-green{background:var(--glow);color:var(--green);border:1px solid rgba(0,212,160,.2)}
.tag-amber{background:rgba(245,166,35,.12);color:var(--amber);border:1px solid rgba(245,166,35,.2)}
.tag-blue{background:rgba(79,172,254,.1);color:var(--blue);border:1px solid rgba(79,172,254,.2)}
.tag-red{background:rgba(255,71,87,.1);color:var(--red);border:1px solid rgba(255,71,87,.2)}
.cv-unread{position:absolute;right:12px;top:50%;transform:translateY(-50%);
  background:var(--green);color:#000;width:18px;height:18px;border-radius:50%;
  font-size:9px;font-weight:800;display:flex;align-items:center;justify-content:center}

/* ── CHAT PANEL ── */
.chat-right{display:flex;flex-direction:column;background:var(--bg);overflow:hidden}
.chat-head{padding:12px 16px;background:var(--s1);border-bottom:1px solid var(--border);
  display:flex;justify-content:space-between;align-items:center;flex-shrink:0}
.chat-head-info .cphone{font-size:14px;font-weight:800}
.chat-head-info .cstatus{font-size:10px;color:var(--text2);margin-top:2px}
.chat-head-acts{display:flex;gap:7px}
.chat-msgs{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:5px}
.msg-wrap{display:flex;flex-direction:column}
.bubble{max-width:75%;padding:9px 13px;border-radius:11px;font-size:12px;line-height:1.55;word-break:break-word}
.bubble.in{background:var(--s2);border:1px solid var(--border);align-self:flex-start;border-bottom-left-radius:3px}
.bubble.out{background:rgba(0,212,160,.1);border:1px solid rgba(0,212,160,.18);align-self:flex-end;border-bottom-right-radius:3px}
.bubble.admin{background:rgba(245,166,35,.1);border:1px solid rgba(245,166,35,.2);align-self:flex-end;border-bottom-right-radius:3px}
.msg-ts{font-size:9px;color:var(--text3);margin-top:2px}
.msg-ts.r{align-self:flex-end}
.chat-takeover-notice{padding:8px 16px;background:rgba(245,166,35,.08);border-top:1px solid rgba(245,166,35,.15);
  font-size:11px;color:var(--amber);text-align:center;flex-shrink:0}
.chat-foot{padding:10px 14px;background:var(--s1);border-top:1px solid var(--border);flex-shrink:0}
.qr-bar{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px}
.qr-btn{padding:4px 10px;background:var(--s2);border:1px solid var(--border);
  color:var(--text2);border-radius:6px;font-size:10px;cursor:pointer;transition:all .15s}
.qr-btn:hover{border-color:var(--green);color:var(--green)}
.chat-input-row{display:flex;gap:8px;align-items:flex-end}
.chat-ta{flex:1;padding:9px 13px;background:var(--s2);border:1px solid var(--border);
  border-radius:8px;color:var(--text);font-size:13px;outline:none;resize:none;
  max-height:100px;line-height:1.45}
.chat-ta:focus{border-color:var(--green);box-shadow:0 0 0 2px var(--glow)}
.no-chat{display:flex;flex-direction:column;align-items:center;justify-content:center;
  flex:1;color:var(--text2);gap:10px}
.no-chat-icon{font-size:44px;opacity:.25}
.chat-bot-foot{padding:12px 16px;background:var(--s1);border-top:1px solid var(--border);
  display:flex;align-items:center;justify-content:space-between;flex-shrink:0}
.chat-bot-note{font-size:11px;color:var(--text2)}

/* ── TABLE ── */
.tbl-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:12px}
th{text-align:left;padding:9px 14px;color:var(--text2);font-size:10px;
  text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid var(--border);white-space:nowrap}
td{padding:10px 14px;border-bottom:1px solid var(--border);vertical-align:middle}
tr:last-child td{border-bottom:none}
tr:hover td{background:var(--s2)}

/* ── BROADCAST ── */
.bc-compose{padding:16px;background:var(--s2);border-radius:var(--r);margin-bottom:14px}
.bc-ta{width:100%;padding:12px 14px;background:var(--s1);border:1px solid var(--border);
  border-radius:8px;color:var(--text);font-size:13px;outline:none;resize:vertical;min-height:110px;line-height:1.5}
.bc-ta:focus{border-color:var(--green);box-shadow:0 0 0 2px var(--glow)}
.bc-meta{display:flex;justify-content:space-between;align-items:center;margin-top:8px}
.bc-count{font-size:11px;color:var(--text2)}
.bc-actions{display:flex;gap:8px;margin-top:12px;align-items:center}
.bc-status{font-size:12px;color:var(--text2)}
.template-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:10px;margin-bottom:14px}
.tpl-card{background:var(--s2);border:1px solid var(--border);border-radius:var(--r);
  padding:14px;cursor:pointer;transition:all .15s;position:relative}
.tpl-card:hover{border-color:var(--green);background:var(--glow)}
.tpl-title{font-size:12px;font-weight:700;color:var(--green);margin-bottom:6px}
.tpl-body{font-size:11px;color:var(--text2);line-height:1.5;max-height:55px;overflow:hidden}
.tpl-hint{font-size:9px;color:var(--green);margin-top:8px;opacity:.7}
.tpl-del{position:absolute;top:8px;right:8px;background:rgba(255,71,87,.15);border:none;
  color:var(--red);width:20px;height:20px;border-radius:50%;cursor:pointer;
  font-size:11px;display:flex;align-items:center;justify-content:center;transition:background .2s}
.tpl-del:hover{background:rgba(255,71,87,.3)}

/* ── BUTTONS ── */
.btn{padding:8px 16px;border-radius:8px;font-size:12px;font-weight:700;cursor:pointer;
  border:none;transition:all .15s;display:inline-flex;align-items:center;gap:5px;letter-spacing:.2px}
.btn:hover{opacity:.85}.btn:active{transform:scale(.97)}
.btn-teal{background:var(--green);color:#000}
.btn-amber{background:var(--amber);color:#000}
.btn-red{background:var(--red);color:#fff}
.btn-ghost{background:var(--s2);color:var(--text);border:1px solid var(--border)}
.btn-ghost:hover{border-color:var(--green)}
.btn-sm{padding:5px 11px;font-size:11px}

/* ── MODAL ── */
.modal-ov{display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);
  z-index:1000;align-items:center;justify-content:center;backdrop-filter:blur(2px)}
.modal-ov.open{display:flex}
.modal{background:var(--s1);border:1px solid var(--border2);border-radius:16px;
  padding:26px;width:460px;max-width:95vw;animation:modalIn .2s ease}
@keyframes modalIn{from{opacity:0;transform:scale(.94)}to{opacity:1;transform:scale(1)}}
.modal-title{font-size:16px;font-weight:800;margin-bottom:20px}
.fg{margin-bottom:15px}
.flabel{font-size:10px;color:var(--text2);text-transform:uppercase;letter-spacing:.7px;margin-bottom:6px;display:block}
.finput,.fta{width:100%;padding:10px 13px;background:var(--s2);border:1px solid var(--border);
  border-radius:8px;color:var(--text);font-size:13px;outline:none}
.finput:focus,.fta:focus{border-color:var(--green);box-shadow:0 0 0 2px var(--glow)}
.fta{resize:vertical;min-height:80px}
.modal-acts{display:flex;gap:9px;justify-content:flex-end;margin-top:18px}

/* ── TOAST ── */
.toast{position:fixed;bottom:22px;right:22px;padding:11px 18px;border-radius:9px;
  font-size:12px;font-weight:600;z-index:9999;pointer-events:none;
  transform:translateY(60px);opacity:0;transition:all .28s ease;
  border:1px solid;max-width:320px}
.toast.show{transform:translateY(0);opacity:1}
.toast.ok{background:rgba(0,212,160,.12);border-color:rgba(0,212,160,.4);color:var(--green)}
.toast.err{background:rgba(255,71,87,.12);border-color:rgba(255,71,87,.4);color:var(--red)}
.toast.info{background:rgba(79,172,254,.12);border-color:rgba(79,172,254,.4);color:var(--blue)}

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
      <div><div class="lc-name">Sally-Ann School Limited</div><div class="lc-sub">WhatsApp Admin Console</div></div>
    </div>
    <div class="lc-label">Admin Password</div>
    <input class="lc-input" type="password" id="pw" placeholder="Enter password" onkeydown="if(event.key==='Enter')login()">
    <button class="lc-btn" onclick="login()">Access Dashboard →</button>
    <div class="lc-err" id="lerr"></div>
  </div>
</div>

<!-- ═══ APP ═══ -->
<div id="app">
  <div class="topbar">
    <div class="tb-l">
      <div class="tb-emblem">🏫</div>
      <div><div class="tb-school">Sally-Ann School</div><div class="tb-tag">Admin Dashboard</div></div>
    </div>
    <div class="tb-r">
      <div class="live-pill"><div class="live-dot"></div><span class="live-txt">LIVE</span></div>
      <div class="unread-pill" id="unread-pill">0 unread</div>
      <button class="tb-signout" onclick="logout()">Sign out</button>
    </div>
  </div>

  <div class="body-layout">
    <!-- SIDEBAR -->
    <div class="sidebar">
      <div class="nav-sep">Overview</div>
      <div class="ni active" onclick="showPg('dashboard',this)"><span class="ni-icon">📊</span>Dashboard</div>

      <div class="nav-sep">Inbox</div>
      <div class="ni" onclick="showPg('conversations',this)" id="ni-conv">
        <span class="ni-icon">💬</span>Conversations
        <span class="ni-badge" id="ni-badge">0</span>
      </div>
      <div class="ni" onclick="showPg('messages',this)"><span class="ni-icon">📨</span>Message Log</div>

      <div class="nav-sep">Outbox</div>
      <div class="ni" onclick="showPg('broadcast',this)"><span class="ni-icon">📢</span>Broadcast</div>

      <div class="nav-sep">Tools</div>
      <div class="ni" onclick="showPg('templates',this)"><span class="ni-icon">⚡</span>Templates</div>
    </div>

    <!-- CONTENT -->
    <div class="content">

      <!-- ── DASHBOARD ── -->
      <div class="pg active" id="pg-dashboard">
        <div class="ph"><div class="ph-title">Dashboard</div><div class="ph-sub">Real-time overview — Sally-Ann School WhatsApp bot</div></div>

        <div class="metrics-grid">
          <div class="mc"><div class="mc-glow teal"></div><div class="mc-icon">👨‍👩‍👧</div><div class="mc-val teal" id="m-users">0</div><div class="mc-label">Total Parents</div></div>
          <div class="mc"><div class="mc-glow blue"></div><div class="mc-icon">🟢</div><div class="mc-val blue" id="m-1h">0</div><div class="mc-label">Active (1h)</div></div>
          <div class="mc"><div class="mc-glow purple"></div><div class="mc-icon">📅</div><div class="mc-val purple" id="m-24h">0</div><div class="mc-label">Active (24h)</div></div>
          <div class="mc"><div class="mc-glow teal"></div><div class="mc-icon">💬</div><div class="mc-val teal" id="m-msgs">0</div><div class="mc-label">Total Messages</div></div>
          <div class="mc"><div class="mc-glow amber"></div><div class="mc-icon">📥</div><div class="mc-val amber" id="m-unread">0</div><div class="mc-label">Unread</div></div>
          <div class="mc"><div class="mc-glow red"></div><div class="mc-icon">🎙️</div><div class="mc-val red" id="m-takeover">0</div><div class="mc-label">Admin Active</div></div>
          <div class="mc"><div class="mc-glow sky"></div><div class="mc-icon">📢</div><div class="mc-val sky" id="m-bc">0</div><div class="mc-label">Broadcasts</div></div>
          <div class="mc"><div class="mc-glow blue"></div><div class="mc-icon">📤</div><div class="mc-val blue" id="m-out">0</div><div class="mc-label">Bot Replies</div></div>
        </div>

        <div class="two-col" style="margin-bottom:14px">
          <div class="card">
            <div class="ch"><span class="ct">🔴 Live Feed</span><span style="font-size:9px;color:var(--text3)" id="feed-ts"></span></div>
            <div class="feed" id="feed">
              <div class="empty-state"><div class="ei">📡</div><div class="et">Waiting for messages...</div></div>
            </div>
          </div>
          <div class="card">
            <div class="ch"><span class="ct">🔥 Top Questions</span></div>
            <div id="faq-list"><div class="empty-state"><div class="ei">❓</div><div class="et">No data yet</div></div></div>
          </div>
        </div>

        <div class="card">
          <div class="ch"><span class="ct">📈 Message Activity (24h)</span></div>
          <div class="act-chart" id="act-chart"><div class="empty-state" style="width:100%;padding:20px"><div class="et" style="color:var(--text3)">No activity data yet</div></div></div>
        </div>
      </div>

      <!-- ── CONVERSATIONS ── -->
      <div class="pg" id="pg-conversations">
        <div class="ph"><div class="ph-title">Conversations</div><div class="ph-sub">Read, reply, and take over from the bot</div></div>
        <div class="conv-wrap">
          <div class="conv-left">
            <div class="conv-search"><input placeholder="Search by phone number..." id="conv-q" oninput="filterConvs(this.value)"></div>
            <div class="conv-scroll" id="conv-scroll"></div>
          </div>
          <div class="chat-right" id="chat-right">
            <div class="no-chat"><div class="no-chat-icon">💬</div><span style="font-size:13px">Select a conversation</span></div>
          </div>
        </div>
      </div>

      <!-- ── MESSAGE LOG ── -->
      <div class="pg" id="pg-messages">
        <div class="ph"><div class="ph-title">Message Log</div><div class="ph-sub">Complete history of all inbound and outbound messages</div></div>
        <div class="card">
          <div class="ch"><span class="ct">All Messages</span><button class="btn btn-ghost btn-sm" onclick="loadMsgs()">↻ Refresh</button></div>
          <div class="tbl-wrap"><table>
            <thead><tr><th>Time</th><th>Phone</th><th>Direction</th><th>Message</th></tr></thead>
            <tbody id="msg-tbody"></tbody>
          </table></div>
        </div>
      </div>

      <!-- ── BROADCAST ── -->
      <div class="pg" id="pg-broadcast">
        <div class="ph"><div class="ph-title">Broadcast Message</div><div class="ph-sub">Send messages to all parents or use saved templates</div></div>

        <div class="card">
          <div class="ch"><span class="ct">Compose Message</span></div>
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
            <button class="btn btn-teal" onclick="bcSend()">📢 Send to All Parents</button>
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
            <thead><tr><th>Sent At</th><th>Message Preview</th><th>Recipients</th><th>Delivered</th><th>Failed</th></tr></thead>
            <tbody id="bc-hist-tbody"></tbody>
          </table></div>
        </div>
      </div>

      <!-- ── TEMPLATES ── -->
      <div class="pg" id="pg-templates">
        <div class="ph"><div class="ph-title">Quick Reply Templates</div><div class="ph-sub">Saved messages for fast replies and broadcasts</div></div>
        <div class="card">
          <div class="ch"><span class="ct">Saved Templates</span><button class="btn btn-teal btn-sm" onclick="openTplModal()">+ Add Template</button></div>
          <div class="template-grid" id="tpl-settings"></div>
        </div>
      </div>

    </div><!-- /content -->
  </div><!-- /body-layout -->
</div><!-- /app -->

<!-- ADD TEMPLATE MODAL -->
<div class="modal-ov" id="tpl-modal">
  <div class="modal">
    <div class="modal-title">⚡ Add Template</div>
    <div class="fg"><label class="flabel">Title</label><input class="finput" id="tpl-title" placeholder="e.g. Fee Reminder"></div>
    <div class="fg"><label class="flabel">Message Body</label><textarea class="fta" id="tpl-body" rows="5" placeholder="Type the message template..."></textarea></div>
    <div class="modal-acts">
      <button class="btn btn-ghost" onclick="closeModal('tpl-modal')">Cancel</button>
      <button class="btn btn-teal" onclick="saveTpl()">Save Template</button>
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
      <button class="btn btn-teal" onclick="bcConfirm()">✓ Confirm & Send</button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
'use strict';
const API='';
let convData={}, selPhone=null, allTpl=[], convTimer=null, dashTimer=null;

/* ── AUTH ── */
async function login(){
  const pw=document.getElementById('pw').value;
  const r=await fetch(API+'/admin/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:pw})});
  if(r.ok){
    document.getElementById('login').style.display='none';
    document.getElementById('app').style.display='flex';
    document.getElementById('app').style.flexDirection='column';
    boot();
  } else {
    document.getElementById('lerr').textContent='Incorrect password. Please try again.';
  }
}
async function logout(){await fetch(API+'/admin/logout',{method:'POST'});location.reload();}

/* ── BOOT ── */
function boot(){
  loadDash();
  loadTpl();
  dashTimer=setInterval(loadDash,8000);
}

/* ── NAV ── */
function showPg(name,el){
  document.querySelectorAll('.pg').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.ni').forEach(n=>n.classList.remove('active'));
  document.getElementById('pg-'+name).classList.add('active');
  el.classList.add('active');
  if(name==='conversations') loadConvs();
  if(name==='messages')      loadMsgs();
  if(name==='broadcast')     loadBcPage();
  if(name==='templates')     renderTplSettings();
}

/* ── DASHBOARD ── */
async function loadDash(){
  const r=await fetch(API+'/admin/metrics');
  if(!r.ok)return;
  const d=await r.json();

  setText('m-users',d.total_users||0);
  setText('m-1h',d.active_1h||0);
  setText('m-24h',d.active_24h||0);
  setText('m-msgs',d.total_messages||0);
  setText('m-unread',d.unread||0);
  setText('m-takeover',d.admin_takeovers||0);
  setText('m-bc',d.broadcasts_sent||0);
  setText('m-out',d.outbound||0);

  // Unread pill + badge
  const u=d.unread||0;
  const up=document.getElementById('unread-pill');
  up.textContent=u+' unread'; up.style.display=u>0?'inline-flex':'none';
  const nb=document.getElementById('ni-badge');
  nb.textContent=u; nb.style.display=u>0?'inline':'none';

  // FAQ
  if(d.top_faqs&&d.top_faqs.length){
    const mx=d.top_faqs[0][1]||1;
    document.getElementById('faq-list').innerHTML=d.top_faqs.map(([kw,cnt])=>`
      <div class="faq-row"><span style="text-transform:capitalize;font-size:12px">${kw}</span><span style="color:var(--green);font-weight:700;font-size:12px">${cnt}</span></div>
      <div class="faq-bar-bg"><div class="faq-bar-fill" style="width:${(cnt/mx*100).toFixed(0)}%"></div></div>`).join('');
  }

  // Activity chart
  if(d.hourly&&Object.keys(d.hourly).length){
    const entries=Object.entries(d.hourly);
    const mx2=Math.max(...entries.map(e=>e[1]),1);
    document.getElementById('act-chart').innerHTML=entries.map(([h,v])=>`
      <div class="act-col">
        <div class="act-bar" style="height:${Math.max((v/mx2)*64,3)}px" data-tip="${h}: ${v} msgs"></div>
        <div class="act-lbl">${h}</div>
      </div>`).join('');
  }

  // Live feed
  const mr=await fetch(API+'/admin/messages?limit=15');
  if(mr.ok){
    const msgs=await mr.json();
    if(msgs.length){
      document.getElementById('feed-ts').textContent='Updated '+new Date().toLocaleTimeString();
      document.getElementById('feed').innerHTML=msgs.slice(0,10).map(m=>`
        <div class="fi">
          <div class="fi-dot ${m.direction==='inbound'?'in':'out'}"></div>
          <div class="fi-body">
            <div class="fi-phone">${m.phone.replace('whatsapp:','')}</div>
            <div class="fi-msg">${esc(m.message).substring(0,70)}${m.message.length>70?'…':''}</div>
          </div>
          <div class="fi-time">${new Date(m.timestamp).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})}</div>
        </div>`).join('');
    }
  }
}

/* ── CONVERSATIONS ── */
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
    return `<div class="cv-item ${selPhone===ph?'active':''}" onclick="selConv('${ph}')">
      <div class="cv-header"><span class="cv-phone">${ph.replace('whatsapp:','')}</span><span class="cv-time">${t}</span></div>
      <div class="cv-preview">${preview}</div>
      <div class="cv-tags">
        ${c.admin_takeover?'<span class="tag tag-amber">👤 Admin</span>':'<span class="tag tag-green">🤖 Bot</span>'}
        <span class="tag tag-blue">${c.message_count||0} msgs</span>
      </div>
      ${c.unread>0?`<div class="cv-unread">${c.unread}</div>`:''}
    </div>`;
  }).join('');
}

function filterConvs(q){
  const f={};Object.keys(convData).forEach(p=>{if(p.includes(q))f[p]=convData[p];});
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
  const panel=document.getElementById('chat-right');

  const msgsHtml=log.length?log.map(m=>{
    const isIn=m.direction==='inbound';
    const isAdm=m.message&&m.message.startsWith('[ADMIN]');
    const cls=isIn?'in':isAdm?'admin':'out';
    return `<div class="msg-wrap">
      <div class="bubble ${cls}">${esc(m.message).replace(/\n/g,'<br>').replace(/\*(.*?)\*/g,'<strong>$1</strong>')}</div>
      <div class="msg-ts ${isIn?'':'r'}">${new Date(m.timestamp).toLocaleTimeString()}</div>
    </div>`;
  }).join(''):'<div class="empty-state"><div class="ei">💬</div><div class="et">No messages yet</div></div>';

  const qrBtns=allTpl.slice(0,5).map(t=>`<button class="qr-btn" onclick="useQR('${ph}',${t.id})">⚡ ${t.title}</button>`).join('');

  panel.innerHTML=`
    <div class="chat-head">
      <div class="chat-head-info">
        <div class="cphone">${ph.replace('whatsapp:','')}</div>
        <div class="cstatus">${isTa?'🔴 Admin control':'🤖 Bot handling'} · ${c.message_count||0} messages</div>
      </div>
      <div class="chat-head-acts">
        ${isTa
          ?`<button class="btn btn-teal btn-sm" onclick="relConv('${ph}')">🤖 Return to Bot</button>`
          :`<button class="btn btn-amber btn-sm" onclick="taConv('${ph}')">👤 Take Over</button>`}
      </div>
    </div>
    <div class="chat-msgs" id="chat-msgs">${msgsHtml}</div>
    ${isTa?`
      <div class="chat-takeover-notice">⚡ You are in control — bot is paused for this conversation</div>
      <div class="chat-foot">
        ${qrBtns?`<div class="qr-bar">${qrBtns}</div>`:''}
        <div class="chat-input-row">
          <textarea class="chat-ta" id="adm-input" rows="2" placeholder="Type your reply... (Enter to send, Shift+Enter for new line)"
            onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();sendAdm('${ph}')}"></textarea>
          <button class="btn btn-teal" onclick="sendAdm('${ph}')">Send</button>
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
  if(convData[ph]) convData[ph].admin_takeover=true;
  renderConvList(convData); renderChat(ph);
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

/* ── MESSAGES ── */
async function loadMsgs(){
  const r=await fetch(API+'/admin/messages?limit=300');
  const msgs=await r.json();
  const tb=document.getElementById('msg-tbody');
  if(!msgs.length){tb.innerHTML='<tr><td colspan="4"><div class="empty-state"><div class="ei">📨</div><div class="et">No messages yet</div></div></td></tr>';return;}
  tb.innerHTML=msgs.map(m=>`<tr>
    <td style="white-space:nowrap;color:var(--text2);font-size:11px">${new Date(m.timestamp).toLocaleString()}</td>
    <td style="font-weight:700;font-size:12px">${m.phone.replace('whatsapp:','')}</td>
    <td><span class="tag ${m.direction==='inbound'?'tag-green':'tag-blue'}">${m.direction==='inbound'?'📥 In':'📤 Out'}</span></td>
    <td style="max-width:380px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px">${esc(m.message).substring(0,140)}</td>
  </tr>`).join('');
}

/* ── BROADCAST ── */
async function loadBcPage(){
  // Recipient count
  const ur=await fetch(API+'/admin/users');
  const users=await ur.json();
  document.getElementById('bc-count').textContent=users.length;

  // Templates
  renderTplBc();

  // History
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
  if(!allTpl.length){el.innerHTML='<div class="empty-state"><div class="ei">⚡</div><div class="et">No templates yet. Go to Templates to add some.</div></div>';return;}
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

/* ── UTILS ── */
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