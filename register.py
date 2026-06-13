import os
import logging
import hashlib
import hmac
import requests
from flask import Flask, request, jsonify
from groq import Groq
from dotenv import load_dotenv

load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ── Env vars ──────────────────────────────────────────────────────────────────
GROQ_API_KEY    = os.getenv("GROQ_API_KEY", "")
WHATSAPP_TOKEN  = os.getenv("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")
VERIFY_TOKEN    = os.getenv("VERIFY_TOKEN", "")
GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY", "")
APP_SECRET      = os.getenv("APP_SECRET", "")

groq_client = Groq(api_key=GROQ_API_KEY)

# ── In-memory conversation history ────────────────────────────────────────────
conversation_history = {}

def get_history(phone):
    return conversation_history.get(phone, [])

def save_history(phone, user_message, bot_response):
    if phone not in conversation_history:
        conversation_history[phone] = []
    conversation_history[phone].append({"role": "user",      "content": user_message})
    conversation_history[phone].append({"role": "assistant", "content": bot_response})
    if len(conversation_history[phone]) > 20:
        conversation_history[phone] = conversation_history[phone][-20:]

# ── School context ─────────────────────────────────────────────────────────────
SCHOOL_CONTEXT = """
You are a friendly and helpful WhatsApp assistant for Sally-Ann School Limited in Litein, Kenya.
Your job is to answer questions from parents about the school.

SCHOOL FEES 2026:
- Grade 1 & 2: Ksh 15,500 per term
- ICT Coding & Robotics: Ksh 1,500 per term
- Total fees: Ksh 17,000 per term
- New admission registration fee: Ksh 2,000
- At least 60% of fees must be paid on Reporting Day
- No cash accepted — all payments must be banked

PAYMENT OPTIONS:
- M-Pesa Paybill: 777643, Account: child's ADM number
- KCB Bank Account: 1135294917
- Equity Bank Account: 0530291926992
- Equity Paybill: 247247, Account: 926992#ADM number
- Cooperative Bank Account: 01148786054900
- Chai Sacco Account: 1083225 Litein branch

BUS ROUTES AND FARES PER MONTH:
Kapkatet Route: Koitabai Ksh 2300, Kapkatet Daraja Sita Ksh 1950, Kapkatet Factory Ksh 1850,
  Kabianga/Kapkatet Town Ksh 1600, Chematich Ksh 1850, Kapkatolonyi Ksh 1250,
  Kaptote Ksh 1150, Koiwa Road/DC Junction Ksh 950
Litein Route: Litein Town/St Kizitos Ksh 950, Factory Gate Ksh 1050,
  Kwa Soi/Kwa Chirchir/Joyland Ksh 1150, Imarisha Ksh 1150, Kusumek Ksh 1600
Tebesonik Route: Lalagin Ksh 1250, Kiptewit Junction Ksh 1500, Cheborge Centre Ksh 1600,
  Korongoi Ksh 1700, Bokoiyot/Siongi/Tebesoni K Factory Ksh 2300
Chemosot Route: Cheluget Ksh 1250, Chelilis/Chesingoro Ksh 1600,
  Kaminjeiwet/Getarwet Junction Ksh 1700
Mogogosiek Route: Murram Ksh 2600, Mogogosiek Ksh 2500, Boito Kaptien Rd Ksh 1850,
  Boito Shopping Center Ksh 1600, Chemoiben Ksh 1400, DC Residence Ksh 1050

TERM II 2026 EDUCATIONAL TRIPS:
- Grade 4: Maasai Mara - Ksh 2,500
- Grade 5: Nakuru - 1st April 2026
- Grade 6: Naivasha - Ksh 3,500
- Grade 7: Nairobi - Ksh 5,000
- Grade 8: Mombasa - Ksh 15,000

PARENTAL ENGAGEMENT DAYS TERM II 2026:
- Grade 5: 16th May 2026
- Grade 4: 23rd May 2026
- Grade 3: 30th May 2026
- Grade 2: 6th June 2026
- Grade 1: 13th June 2026
- PP1 & PP2: 20th June 2026
- Half Term: 24th June to 28th June 2026

ICT DIGISKOOL PROGRAMME:
Coding, Robotics and AI for Grade 1-9. Termly fee of Ksh 1,500 included in school fees.

SCHOOL CONTACTS:
- Address: P.O. Box 401, Litein
- For anything not covered above, tell the parent to call the school office directly

IMPORTANT RULES:
- Always reply in the same language the parent uses (English or Swahili)
- Keep replies short and clear — maximum 3 sentences
- Always be friendly and polite
- If unsure, tell the parent to call the school office
- Never make up information not listed above
- Greet parents warmly and ask how you can help
- Remember the conversation context and continue naturally
"""

# ── Keyword responses ─────────────────────────────────────────────────────────
KEYWORD_RESPONSES = {
    "hello":      "Hello! Welcome to Sally-Ann School Limited. How can I help you today?",
    "hi":         "Hi there! Welcome to Sally-Ann School. Ask me about fees, bus fares, payments, trips or events.",
    "hujambo":    "Habari! Karibu Sally-Ann School. Niulize kuhusu ada, basi, malipo au shughuli za shule.",
    "habari":     "Nzuri! Karibu Sally-Ann School. Ninaweza kukusaidia na nini leo?",
    "fee":        "2026 Fees: Grade 1 & 2 Ksh 15,500 + ICT Ksh 1,500 = Total Ksh 17,000/term. At least 60% on Reporting Day. No cash.",
    "ada":        "Ada 2026: Darasa 1 & 2 Ksh 15,500 + ICT Ksh 1,500 = Jumla Ksh 17,000/muhula. Angalau 60% Siku ya Kuripoti. Pesa taslimu hazipokeleki.",
    "pay":        "Payment: M-Pesa Paybill 777643 (ADM No), KCB 1135294917, Chai Sacco 1083225, Coop Bank 01148786054900, Equity 0530291926992.",
    "mpesa":      "M-Pesa Paybill: 777643. Account: Your child's ADM number. No cash accepted.",
    "bus":        "We have 5 bus routes: Kapkatet, Litein, Tebesonik, Chemosot and Mogogosiek. Reply with your route name for fares.",
    "basi":       "Tuna njia 5 za basi: Kapkatet, Litein, Tebesonik, Chemosot na Mogogosiek. Andika jina la njia yako.",
    "kapkatet":   "Kapkatet (per month): Koitabai 2300, Daraja Sita 1950, Factory 1850, Kabianga/Town 1600, Chematich 1850, Kapkatolonyi 1250, Kaptote 1150, DC Jct 950.",
    "litein":     "Litein (per month): Town/St Kizitos 950, Factory Gate 1050, Kwa Soi/Joyland 1150, Imarisha 1150, Kusumek 1600.",
    "tebesonik":  "Tebesonik (per month): Lalagin 1250, Kiptewit Jct 1500, Cheborge 1600, Korongoi 1700, Bokoiyot/Factory 2300.",
    "chemosot":   "Chemosot (per month): Cheluget 1250, Chelilis/Chesingoro 1600, Kaminjeiwet/Getarwet Jct 1700.",
    "mogogosiek": "Mogogosiek (per month): Murram 2600, Mogogosiek 2500, Boito Kaptien Rd 1850, Boito Shopping 1600, Chemoiben 1400, DC Residence 1050.",
    "trip":       "Term II Trips: Grade 4 Maasai Mara 2500, Grade 5 Nakuru, Grade 6 Naivasha 3500, Grade 7 Nairobi 5000, Grade 8 Mombasa 15000.",
    "safari":     "Safari Term II: Darasa 4 Maasai Mara 2500, Darasa 5 Nakuru, Darasa 6 Naivasha 3500, Darasa 7 Nairobi 5000, Darasa 8 Mombasa 15000.",
    "meeting":    "Parental Engagement: Grade 5 May 16, Grade 4 May 23, Grade 3 May 30, Grade 2 Jun 6, Grade 1 Jun 13, PP1&PP2 Jun 20. Half Term Jun 24-28.",
    "ict":        "ICT Digiskool (Coding, Robotics & AI) for Grade 1-9. Ksh 1,500/term — included in school fees.",
    "half term":  "Half Term: 24th June to 28th June 2026.",
    "holiday":    "Half Term holiday: 24th June to 28th June 2026.",
    "likizo":     "Likizo ya kati: Tarehe 24 hadi 28 Juni 2026.",
    "thank":      "You are welcome! Feel free to ask if you need anything else. 😊",
    "thanks":     "You are welcome! Feel free to ask if you need anything else. 😊",
    "asante":     "Karibu sana! Niulize swali lolote ukihitaji msaada zaidi. 😊",
}

def find_keyword_response(message):
    """
    FIX: Works for messages of any length, not just 2 words.
    Returns (reply, use_ai).
    """
    msg_lower = message.lower().strip()
    # Exact match first
    if msg_lower in KEYWORD_RESPONSES:
        return KEYWORD_RESPONSES[msg_lower], False
    # Partial match — keyword appears anywhere in the message
    for keyword, response in KEYWORD_RESPONSES.items():
        if keyword in msg_lower:
            return response, False
    return None, True

# ── AI helpers ─────────────────────────────────────────────────────────────────
def ask_groq(messages):
    response = groq_client.chat.completions.create(
        messages=messages,
        model="llama-3.3-70b-versatile",
        max_tokens=300,
        temperature=0.4,
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
    # FIX: Added timeout=10 to prevent hanging
    r = requests.post(url, json=payload, timeout=10)
    r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()

def ask_ai(phone, message):
    history = get_history(phone)
    messages = [{"role": "system", "content": SCHOOL_CONTEXT}]
    messages.extend(history)
    messages.append({"role": "user", "content": message})
    # Try Groq first
    try:
        reply = ask_groq(messages)
        logger.info(f"[{phone}] Groq OK")
        save_history(phone, message, reply)
        return reply
    except Exception as e:
        logger.error(f"[{phone}] Groq error: {e}")
    # Fallback to Gemini
    try:
        reply = ask_gemini(message, history)
        logger.info(f"[{phone}] Gemini fallback OK")
        save_history(phone, message, reply)
        return reply
    except Exception as e:
        logger.error(f"[{phone}] Gemini error: {e}")
    return "Sorry, I'm having trouble right now. Please call the school office directly."

# ── WhatsApp sender ────────────────────────────────────────────────────────────
def send_message(to, body):
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body},
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=10)
        if not r.ok:
            logger.error(f"Meta send error: {r.status_code} {r.text}")
        return r.ok
    except Exception as e:
        logger.error(f"send_message error: {e}")
        return False

# ── FIX: Webhook signature verification ───────────────────────────────────────
def verify_signature(req):
    """Verify the POST is genuinely from Meta using HMAC-SHA256."""
    if not APP_SECRET:
        return True  # Skip verification if APP_SECRET not configured
    signature = req.headers.get("X-Hub-Signature-256", "")
    if not signature.startswith("sha256="):
        return False
    expected = hmac.new(
        APP_SECRET.encode("utf-8"),
        req.data,
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)

# ── Routes ─────────────────────────────────────────────────────────────────────
@app.route("/")
def home():
    return "✅ Sally-Ann School WhatsApp Bot is running!"

@app.route("/webhook", methods=["GET"])
def verify():
    mode      = request.args.get("hub.mode")
    token     = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        logger.info("Webhook verified ✅")
        return challenge, 200
    logger.warning("Webhook verification failed ❌")
    return "Forbidden", 403

@app.route("/webhook", methods=["POST"])
def webhook():
    # FIX: Verify request is from Meta
    if not verify_signature(request):
        logger.warning("Invalid webhook signature — rejected")
        return "Unauthorized", 401

    data = request.get_json()
    try:
        message  = data["entry"][0]["changes"][0]["value"]["messages"][0]
        phone    = message["from"]
        msg_type = message["type"]
        logger.info(f"[{phone}] Type: {msg_type}")

        if msg_type == "image":
            send_message(phone,
                "Thank you for sending your payment receipt! 📸\n"
                "Our office will confirm your payment within 24 hours.\n"
                "For instant confirmation please call the school office.")
            return jsonify({"status": "ok"}), 200

        if msg_type != "text":
            send_message(phone, "Sorry, I can only handle text messages for now.")
            return jsonify({"status": "ok"}), 200

        incoming = message["text"]["body"].strip()
        logger.info(f"[{phone}] Message: {incoming}")

        reply, use_ai = find_keyword_response(incoming)
        if use_ai:
            reply = ask_ai(phone, incoming)

        send_message(phone, reply)

    except (KeyError, IndexError) as e:
        logger.warning(f"Webhook parse error: {e}")

    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
