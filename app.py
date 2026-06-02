from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from dotenv import load_dotenv
from groq import Groq
import os

load_dotenv()

app = Flask(__name__)
groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))

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
- Keep replies short and clear — maximum 3 sentences
- Always be friendly and polite
- If a parent asks something you are not sure about, tell them to call the school office
- Never make up information that is not listed above
- If a parent greets you, greet them back warmly and ask how you can help
- Remember the context of the conversation and continue naturally
"""

# In memory conversation storage
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
    "hello": "Hello! Welcome to Sally-Ann School Limited. How can I help you today? You can ask about fees, bus fares, payment details, trips, parental engagement days or ICT programme.",
    "hi": "Hi there! Welcome to Sally-Ann School Limited. Ask me about fees, bus fares, payment details, trips or school events.",
    "fee": "2026 Fees Structure:\n- Grade 1 & 2: Ksh 15,500 per term\n- ICT (Coding & Robotics): Ksh 1,500 per term\n- TOTAL: Ksh 17,000 per term\nNote: At least 60% of fees must be paid on Reporting Day. No cash accepted.",
    "pay": "Payment options:\n1. M-Pesa Paybill: 777643, A/c: ADM No.\n2. KCB: A/c No. 1135294917\n3. Chai Sacco: A/c No. 1083225 (Litein branch)\n4. Coop Bank: A/c No. 01148786054900\n5. Equity Bank: A/c No. 0530291926992\n6. Equity Paybill: 247247, A/c: 926992#ADM No.\nNote: No cash accepted.",
    "mpesa": "M-Pesa Paybill: 777643\nAccount Number: Your child's ADM No.\nNote: No cash accepted.",
    "bus": "We have 4 bus routes: Kapkatet, Litein, Tebesonik and Chemosot/Mogogosiek.\nReply with your route name for specific fares.",
    "kapkatet": "Kapkatet Route Bus Fares (per month):\n- Koitabai: Ksh 2,300\n- Kapkatet (Daraja Sita): Ksh 1,950\n- Kapkatet Factory: Ksh 1,850\n- Kabianga/Kapkatet Town: Ksh 1,600\n- Chematich: Ksh 1,850\n- Kapkatolonyi: Ksh 1,250\n- Kaptote: Ksh 1,150\n- Koiwa Road/D.C. Junction: Ksh 950",
    "litein": "Litein Route Bus Fares (per month):\n- Litein Town/St. Kizito's: Ksh 950\n- Factory Gate: Ksh 1,050\n- Kwa Soi & Kwa Chirchir & Joyland: Ksh 1,150\n- Imarisha: Ksh 1,150\n- Kusumek: Ksh 1,600",
    "tebesonik": "Tebesonik Route Bus Fares (per month):\n- Lalagin: Ksh 1,250\n- Kiptewit Junction: Ksh 1,500\n- Cheborge Centre: Ksh 1,600\n- Korongoi: Ksh 1,700\n- Bokoiyot/Siongi/Tebesoni K Factory: Ksh 2,300",
    "chemosot": "Chemosot Route Bus Fares (per month):\n- Cheluget: Ksh 1,250\n- Chelilis/Chesingoro: Ksh 1,600\n- Kaminjeiwet/Getarwet Junction: Ksh 1,700",
    "mogogosiek": "Mogogosiek Route Bus Fares (per month):\n- Murram: Ksh 2,600\n- Mogogosiek: Ksh 2,500\n- Boito (Kaptien Rd): Ksh 1,850\n- Boito (Shopping Center): Ksh 1,600\n- Chemoiben: Ksh 1,400\n- D.C. Residence: Ksh 1,050",
    "trip": "Term II 2026 Educational Trips:\n- Grade 4: Maasai Mara - Ksh 2,500\n- Grade 5: Nakuru\n- Grade 6: Naivasha - Ksh 3,500\n- Grade 7: Nairobi - Ksh 5,000\n- Grade 8: Mombasa - Ksh 15,000",
    "meeting": "Parental Engagement Days Term II 2026:\n- Grade 5: 16th May\n- Grade 4: 23rd May\n- Grade 3: 30th May\n- Grade 2: 6th June\n- Grade 1: 13th June\n- PP1 & PP2: 20th June\nHalf Term: 24th-28th June 2026.",
    "ict": "ICT Digiskool Programme (Coding, Robotics & AI) for Grade 1-9. Termly fee of Ksh 1,500 included in school fees.",
    "half term": "Half Term holiday is from 24th June to 28th June 2026.",
    "holiday": "Half Term holiday is from 24th June to 28th June 2026.",
    "asante": "Karibu! Uliza swali lolote kuhusu ada, basi au shughuli za shule.",
    "sawa": "Sawa! Kama una swali lingine, niambie. Niko hapa kukusaidia.",
    "thank": "You are welcome! Feel free to ask if you need anything else.",
    "thanks": "You are welcome! Feel free to ask if you need anything else.",
}

def find_best_response(message):
    message_lower = message.lower().strip()
    if len(message_lower.split()) <= 2:
        for keyword, response in responses.items():
            if keyword == message_lower or keyword in message_lower:
                return response, False
    return None, True

@app.route("/")
def home():
    return "Sally-Ann School WhatsApp Bot is running!"

@app.route("/webhook", methods=["POST"])
def webhook():
    incoming_message = request.form.get("Body", "").strip()
    phone_number = request.form.get("From", "")

    reply, use_ai = find_best_response(incoming_message)

    if use_ai:
        try:
            history = get_history(phone_number)
            messages = [{"role": "system", "content": SCHOOL_CONTEXT}]
            messages.extend(history)
            messages.append({"role": "user", "content": incoming_message})
            response = groq_client.chat.completions.create(
                messages=messages,
                model="llama-3.3-70b-versatile",
            )
            reply = response.choices[0].message.content
            save_history(phone_number, incoming_message, reply)
        except Exception as e:
            print(f"GROQ ERROR: {type(e).__name__}: {str(e)}")
            reply = "Sorry, I could not understand that. Please call the school office or ask about fees, bus fares, trips or events."

    resp = MessagingResponse()
    resp.message(reply)
    return str(resp)

if __name__ == "__main__":
    from waitress import serve
    serve(app, host="0.0.0.0", port=8080)