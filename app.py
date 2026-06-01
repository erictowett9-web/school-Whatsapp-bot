from flask import Flask, request
from flask_sqlalchemy import SQLAlchemy
from twilio.twiml.messaging_response import MessagingResponse
from dotenv import load_dotenv
from fuzzywuzzy import fuzz
from groq import Groq
import os

load_dotenv()

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///conversations.db'
db = SQLAlchemy(app)

with app.app_context():
    db.create_all()

class Conversation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_message = db.Column(db.String(500))
    bot_response = db.Column(db.String(500))
    timestamp = db.Column(db.DateTime, default=db.func.current_timestamp())

# Groq client
groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))

# School context
SCHOOL_CONTEXT = """
You are a helpful school assistant chatbot for Sally-Ann School Limited in Litein, Kenya.
You help parents with enquiries about:
- School fees: Grade 1 & 2 Ksh 15,500 per term, ICT Ksh 1,500, Total Ksh 17,000
- Payment: M-Pesa Paybill 777643 ADM No, KCB 1135294917, Equity 0530291926992, Coop 01148786054900, Chai Sacco 1083225 Litein
- No cash accepted — bank all payments
- At least 60% fees on Reporting Day
- New admission registration fee Ksh 2,000
- Bus routes: Kapkatet, Litein, Tebesonik, Chemosot, Mogogosiek
- Educational trips Term II: Grade 4 Maasai Mara Ksh 2500, Grade 5 Nakuru, Grade 6 Naivasha Ksh 3500, Grade 7 Nairobi Ksh 5000, Grade 8 Mombasa Ksh 15000
- Parental engagement days Term II: Grade 5 16th May, Grade 4 23rd May, Grade 3 30th May, Grade 2 6th June, Grade 1 13th June, PP1&PP2 20th June
- Half term: 24th June to 28th June 2026
- ICT Digiskool Programme Coding Robotics AI Grade 1-9 Ksh 1,500 per term
- School address: P.O. Box 401, Litein

Always be friendly, helpful and brief. Reply in the same language the parent uses.
Keep replies short — maximum 3 sentences.
If you do not know something specific, tell the parent to call the school office.
"""

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
}

def find_best_response(message):
    message = message.lower().strip()

    # First try exact keyword match
    for keyword, response in responses.items():
        if keyword in message:
            return response, False

    # Then try fuzzy matching
    best_match = None
    best_score = 0
    for keyword in responses:
        score = fuzz.partial_ratio(keyword, message)
        if score > best_score:
            best_score = score
            best_match = keyword

    if best_score >= 75:
        return responses[best_match], False

    # If no match found use Groq AI
    return None, True

@app.route("/")
def home():
    return "Sally-Ann School WhatsApp Bot is running!"

@app.route("/webhook", methods=["POST"])
def webhook():
    incoming_message = request.form.get("Body", "").strip()

    reply, use_ai = find_best_response(incoming_message)

    if use_ai:
        try:
            response = groq_client.chat.completions.create(
                messages=[
                    {"role": "system", "content": SCHOOL_CONTEXT},
                    {"role": "user", "content": incoming_message}
                ],
                model="llama-3.3-70b-versatile",
            )
            reply = response.choices[0].message.content
        except Exception as e:
            print(f"GROQ ERROR: {str(e)}")
            reply = "Sorry, I could not understand that. Please call the school office or ask about fees, bus fares, trips or events."

    resp = MessagingResponse()
    resp.message(reply)
    return str(resp)

if __name__ == "__main__":
    from waitress import serve
    serve(app, host="0.0.0.0", port=8080)