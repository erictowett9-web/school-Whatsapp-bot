from flask import Flask, request, jsonify
from dotenv import load_dotenv
from groq import Groq
import os
import requests
import threading
import time

load_dotenv()

app = Flask(__name__)
groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
APP_URL = os.getenv("APP_URL", "https://correct-libbie-ericktowett-d139d96e.koyeb.app/")

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
Kapkatet Route: Koitabai Ksh 2300, Kapkatet Daraja Sita Ksh 1950, Kapkatet Factory Ksh 1850, Kabianga/Kapkatet Town Ksh 1600, Chematich Ksh 1850, Kapkatolonyi Ksh 1250, Kaptote Ksh 1150, Koiwa Road/DC Junction Ksh 950
Litein Route: Litein Town/St Kizitos Ksh 950, Factory Gate Ksh 1050, Kwa Soi/Kwa Chirchir/Joyland Ksh 1150, Imarisha Ksh 1150, Kusumek Ksh 1600
Tebesonik Route: Lalagin Ksh 1250, Kiptewit Junction Ksh 1500, Cheborge Centre Ksh 1600, Korongoi Ksh 1700, Bokoiyot/Siongi/Tebesoni K Factory Ksh 2300
Chemosot Route: Cheluget Ksh 1250, Chelilis/Chesingoro Ksh 1600, Kaminjeiwet/Getarwet Junction Ksh 1700
Mogogosiek Route: Murram Ksh 2600, Mogogosiek Ksh 2500, Boito Kaptien Rd Ksh 1850, Boito Shopping Center Ksh 1600, Chemoiben Ksh 1400, DC Residence Ksh 1050

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
- Always reply in the same language the parent uses
- Keep replies short and clear - maximum 3 sentences
- Always be friendly and polite
- If a parent asks something you are not sure about, tell them to call the school office
- Never make up information that is not listed above
- If a parent greets you, greet them back warmly and ask how you can help
- Remember the context of the conversation and continue naturally
"""

conversation_history = {}

def get_history(phone_number):
    return conversation_history.get(phone_number, [])

def save_history(phone_number, user_message, bot_response):
    if phone_number not in conversation_history:
        conversation_history[phone_number] = []
    conversation_history[phone_number].append({"role": "user", "content": user_message})
    conversation_history[phone_number].append({"role": "assistant", "content": bot_response})
    if len(conversation_history[phone_number]) > 20:
        conversation_history[phone_number] = conversation_history[phone_number][-20:]

responses = {
    "hello": "Hello! Welcome to Sally-Ann School Limited. How can I help you today?",
    "hi": "Hi there! Welcome to Sally-Ann School Limited. Ask me about fees, bus fares, payment details, trips or school events.",
    "fee": "2026 Fees: Grade 1 & 2 Ksh 15,500 + ICT Ksh 1,500 = Total Ksh 17,000 per term. At least 60% must be paid on Reporting Day. No cash accepted.",
    "pay": "Payment options: M-Pesa Paybill 777643 ADM No, KCB 1135294917, Chai Sacco 1083225, Coop Bank 01148786054900, Equity 0530291926992. No cash accepted.",
    "mpesa": "M-Pesa Paybill: 777643. Account Number: Your child's ADM No. No cash accepted.",
    "bus": "We have 4 bus routes: Kapkatet, Litein, Tebesonik and Chemosot/Mogogosiek. Reply with your route name for specific fares.",
    "kapkatet": "Kapkatet Route fares per month: Koitabai Ksh 2300, Kapkatet Daraja Sita Ksh 1950, Kapkatet Factory Ksh 1850, Kabianga/Kapkatet Town Ksh 1600, Chematich Ksh 1850, Kapkatolonyi Ksh 1250, Kaptote Ksh 1150, Koiwa Road/DC Junction Ksh 950.",
    "litein": "Litein Route fares per month: Litein Town/St Kizitos Ksh 950, Factory Gate Ksh 1050, Kwa Soi/Kwa Chirchir/Joyland Ksh 1150, Imarisha Ksh 1150, Kusumek Ksh 1600.",
    "tebesonik": "Tebesonik Route fares per month: Lalagin Ksh 1250, Kiptewit Junction Ksh 1500, Cheborge Centre Ksh 1600, Korongoi Ksh 1700, Bokoiyot/Siongi/Tebesoni K Factory Ksh 2300.",
    "chemosot": "Chemosot Route fares per month: Cheluget Ksh 1250, Chelilis/Chesingoro Ksh 1600, Kaminjeiwet/Getarwet Junction Ksh 1700.",
    "mogogosiek": "Mogogosiek Route fares per month: Murram Ksh 2600, Mogogosiek Ksh 2500, Boito Kaptien Rd Ksh 1850, Boito Shopping Center Ksh 1600, Chemoiben Ksh 1400, DC Residence Ksh 1050.",
    "trip": "Term II 2026 Trips: Grade 4 Maasai Mara Ksh 2500, Grade 5 Nakuru, Grade 6 Naivasha Ksh 3500, Grade 7 Nairobi Ksh 5000, Grade 8 Mombasa Ksh 15000.",
    "meeting": "Parental Engagement Days: Grade 5 16th May, Grade 4 23rd May, Grade 3 30th May, Grade 2 6th June, Grade 1 13th June, PP1&PP2 20th June. Half Term 24th-28th June 2026.",
    "ict": "ICT Digiskool Programme (Coding, Robotics & AI) for Grade 1-9. Termly fee of Ksh 1,500 included in school fees.",
    "half term": "Half Term holiday is from 24th June to 28th June 2026.",
    "holiday": "Half Term holiday is from 24th June to 28th June 2026.",
    "thank": "You are welcome! Feel free to ask if you need anything else.",
    "thanks": "You are welcome! Feel free to ask if you need anything else.",
    "asante": "Karibu! Uliza swali lolote kuhusu ada, basi au shughuli za shule.",
}

def find_best_response(message):
    message_lower = message.lower().strip()
    if len(message_lower.split()) <= 2:
        for keyword, response in responses.items():
            if keyword == message_lower or keyword in message_lower:
                return response, False
    return None, True

def ask_groq(messages):
    response = groq_client.chat.completions.create(
        messages=messages,
        model="llama-3.3-70b-versatile",
    )
    return response.choices[0].message.content

def ask_gemini(user_message, history):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    gemini_history = []
    for msg in history:
        role = "user" if msg["role"] == "user" else "model"
        gemini_history.append({
            "role": role,
            "parts": [{"text": msg["content"]}]
        })
    gemini_history.append({
        "role": "user",
        "parts": [{"text": user_message}]
    })
    payload = {
        "system_instruction": {
            "parts": [{"text": SCHOOL_CONTEXT}]
        },
        "contents": gemini_history
    }
    r = requests.post(url, json=payload)
    result = r.json()
    return result["candidates"][0]["content"]["parts"][0]["text"]

def ask_ai(phone_number, incoming_message):
    history = get_history(phone_number)
    messages = [{"role": "system", "content": SCHOOL_CONTEXT}]
    messages.extend(history)
    messages.append({"role": "user", "content": incoming_message})

    # Try Groq first
    try:
        reply = ask_groq(messages)
        print("Responded via Groq")
        save_history(phone_number, incoming_message, reply)
        return reply
    except Exception as e:
        print(f"GROQ ERROR: {type(e).__name__}: {str(e)}")

    # Fallback to Gemini
    try:
        reply = ask_gemini(incoming_message, history)
        print("Responded via Gemini fallback")
        save_history(phone_number, incoming_message, reply)
        return reply
    except Exception as e:
        print(f"GEMINI ERROR: {type(e).__name__}: {str(e)}")

    return "Sorry, I'm having trouble right now. Please call the school office."

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
    r = requests.post(url, headers=headers, json=payload)
    if not r.ok:
        print(f"META SEND ERROR: {r.status_code} {r.text}")

def keep_alive():
    while True:
        time.sleep(600)  # ping every 10 minutes
        try:
            requests.get(APP_URL)
            print("Keep-alive ping sent")
        except Exception as e:
            print(f"Keep-alive failed: {str(e)}")

@app.route("/")
def home():
    return "Sally-Ann School WhatsApp Bot is running!"

@app.route("/webhook", methods=["GET"])
def verify():
    mode      = request.args.get("hub.mode")
    token     = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        print("Webhook verified!")
        return challenge, 200
    return "Forbidden", 403

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    try:
        message      = data["entry"][0]["changes"][0]["value"]["messages"][0]
        phone_number = message["from"]
        msg_type     = message["type"]

        if msg_type == "image":
            send_message(phone_number,
                "Thank you for sending your payment receipt! 📸\n"
                "Our office will confirm your payment within 24 hours.\n"
                "For instant confirmation please call the school office.")
            return jsonify({"status": "ok"}), 200

        elif msg_type != "text":
            send_message(phone_number, "Sorry, I can only handle text messages for now.")
            return jsonify({"status": "ok"}), 200

        incoming_message = message["text"]["body"].strip()
        reply, use_ai = find_best_response(incoming_message)

        if use_ai:
            reply = ask_ai(phone_number, incoming_message)

        send_message(phone_number, reply)

    except (KeyError, IndexError):
        pass

    return jsonify({"status": "ok"}), 200

# Start keep-alive thread
threading.Thread(target=keep_alive, daemon=True).start()

if __name__ == "__main__":
    from waitress import serve
    serve(app, host="0.0.0.0", port=8080)