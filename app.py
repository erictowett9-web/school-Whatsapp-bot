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
24th June to 28th June 2026.",
}SCHOOL_CONTEXT = """
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
"""
def find_best_response(message):
    message = message.lower().strip()

    # Only use keywords for very short exact messages
    if len(message.split()) <= 2:
        for keyword, response in responses.items():
            if keyword == message or message == keyword:
                return response, False

    # Everything else goes to Groq AI for smart response
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