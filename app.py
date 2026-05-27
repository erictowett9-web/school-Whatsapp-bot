from flask import Flask, request
from flask_sqlalchemy import SQLAlchemy
from twilio.twiml.messaging_response import MessagingResponse
from dotenv import load_dotenv
from fuzzywuzzy import fuzz
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

responses = {
    # Greetings
    "hello": "Hello! Welcome to Sally-Ann School Limited. How can I help you today? You can ask about fees, bus fares, payment details, trips, parental engagement days or ICT programme.",
    "hi": "Hi there! Welcome to Sally-Ann School Limited. Ask me about fees, bus fares, payment details, trips or school events.",

    # Fees
    "fee": "2026 Fees Structure:\n- Grade 1 & 2: Ksh 15,500 per term\n- ICT (Coding & Robotics): Ksh 1,500 per term\n- TOTAL: Ksh 17,000 per term\nNote: At least 60% of fees must be paid on Reporting Day. No cash accepted.",
    "grade": "2026 Fees Structure:\n- Grade 1 & 2: Ksh 15,500 per term\n- ICT (Coding & Robotics): Ksh 1,500 per term\n- TOTAL: Ksh 17,000 per term",
    "registration": "New admissions require a registration fee of Ksh 2,000 to be banked together with school fees.",
    "reporting": "At least 60% of school fees must be paid on Reporting Day. No cash will be accepted — bank all payments.",
    "admission": "New admissions require a registration fee of Ksh 2,000 to be banked together with school fees.",
    "how much": "2026 Fees Structure:\n- Grade 1 & 2: Ksh 15,500 per term\n- ICT (Coding & Robotics): Ksh 1,500 per term\n- TOTAL: Ksh 17,000 per term\nNote: At least 60% of fees must be paid on Reporting Day.",
    "cost": "2026 Fees Structure:\n- Grade 1 & 2: Ksh 15,500 per term\n- ICT (Coding & Robotics): Ksh 1,500 per term\n- TOTAL: Ksh 17,000 per term",
    "amount": "2026 Fees Structure:\n- Grade 1 & 2: Ksh 15,500 per term\n- ICT (Coding & Robotics): Ksh 1,500 per term\n- TOTAL: Ksh 17,000 per term",
    "balance": "For fee balance enquiries, please contact the school bursar directly.",
    "school fees": "2026 Fees Structure:\n- Grade 1 & 2: Ksh 15,500 per term\n- ICT (Coding & Robotics): Ksh 1,500 per term\n- TOTAL: Ksh 17,000 per term\nNote: At least 60% of fees must be paid on Reporting Day.",

    # Payment details
    "pay": "Payment options for Sally-Ann School:\n1. M-Pesa Paybill: 777643, A/c: ADM No.\n2. KCB: A/c No. 1135294917\n3. Chai Sacco: A/c No. 1083225 (Litein branch)\n4. Coop Bank: A/c No. 01148786054900\n5. Equity Bank: A/c No. 0530291926992\n6. Equity Paybill: 247247, A/c: 926992#ADM No.\nNote: No cash accepted. Bank fees and bus fare in school account only.",
    "payment": "Payment options for Sally-Ann School:\n1. M-Pesa Paybill: 777643, A/c: ADM No.\n2. KCB: A/c No. 1135294917\n3. Chai Sacco: A/c No. 1083225 (Litein branch)\n4. Coop Bank: A/c No. 01148786054900\n5. Equity Bank: A/c No. 0530291926992\n6. Equity Paybill: 247247, A/c: 926992#ADM No.",
    "mpesa": "M-Pesa Paybill: 777643\nAccount Number: Your child's ADM No.\nNote: No cash accepted.",
    "kcb": "KCB Account Number: 1135294917\nNote: No cash accepted.",
    "equity": "Equity Bank Account: 0530291926992\nEquity Paybill: 247247, A/c: 926992#ADM No.\nNote: No cash accepted.",
    "coop": "Cooperative Bank Account: 01148786054900\nNote: No cash accepted.",
    "sacco": "Chai Sacco Account: 1083225 (Litein branch)\nNote: No cash accepted.",
    "cash": "No cash will be accepted. All bus fare and school fees must be banked in the school account only.",
    "bank": "Payment options:\n1. M-Pesa Paybill: 777643, A/c: ADM No.\n2. KCB: A/c No. 1135294917\n3. Chai Sacco: A/c No. 1083225 (Litein branch)\n4. Coop Bank: A/c No. 01148786054900\n5. Equity Bank: A/c No. 0530291926992\n6. Equity Paybill: 247247, A/c: 926992#ADM No.",
    "send money": "Payment options:\n1. M-Pesa Paybill: 777643, A/c: ADM No.\n2. KCB: A/c No. 1135294917\n3. Equity Paybill: 247247, A/c: 926992#ADM No.\nNote: No cash accepted.",
    "paybill": "M-Pesa Paybill: 777643\nAccount Number: Your child's ADM No.\nNote: No cash accepted.",
    "account": "Payment accounts:\n1. M-Pesa Paybill: 777643, A/c: ADM No.\n2. KCB: A/c No. 1135294917\n3. Equity Paybill: 247247, A/c: 926992#ADM No.",

    # Bus fares
    "bus": "2026 Bus Fare Rates (per month):\nWe have 4 routes: Kapkatet, Litein, Tebesonik and Chemosot/Mogogosiek.\nReply with your route name for specific fares.",
    "transport": "We have 4 bus routes: Kapkatet, Litein, Tebesonik and Chemosot/Mogogosiek routes. Reply with your route name for fares.",
    "fare": "Reply with your route name for specific bus fares. Routes: Kapkatet, Litein, Tebesonik, Chemosot, Mogogosiek.",
    "route": "Our bus routes are: Kapkatet, Litein, Tebesonik, Chemosot and Mogogosiek. Reply with your route name for fares.",
    "matatu": "We have school buses on 4 routes: Kapkatet, Litein, Tebesonik and Chemosot/Mogogosiek. Reply with your route name for fares.",
    "vehicle": "We have school buses on 4 routes: Kapkatet, Litein, Tebesonik and Chemosot/Mogogosiek. Reply with your route name for fares.",
    "kapkatet": "Kapkatet Route Bus Fares (per month):\n- Koitabai: Ksh 2,300\n- Kapkatet (Daraja Sita): Ksh 1,950\n- Kapkatet Factory: Ksh 1,850\n- Kabianga/Kapkatet Town: Ksh 1,600\n- Chematich: Ksh 1,850\n- Kapkatolonyi: Ksh 1,250\n- Kaptote: Ksh 1,150\n- Koiwa Road/D.C. Junction: Ksh 950",
    "litein": "Litein Route Bus Fares (per month):\n- Litein Town/St. Kizito's: Ksh 950\n- Factory Gate: Ksh 1,050\n- Kwa Soi & Kwa Chirchir & Joyland: Ksh 1,150\n- Imarisha: Ksh 1,150\n- Kusumek: Ksh 1,600",
    "tebesonik": "Tebesonik Route Bus Fares (per month):\n- Lalagin: Ksh 1,250\n- Kiptewit Junction: Ksh 1,500\n- Cheborge Centre: Ksh 1,600\n- Korongoi: Ksh 1,700\n- Bokoiyot/Siongi/Tebesoni K Factory: Ksh 2,300",
    "chemosot": "Chemosot Route Bus Fares (per month):\n- Cheluget: Ksh 1,250\n- Chelilis/Chesingoro: Ksh 1,600\n- Kaminjeiwet/Getarwet Junction: Ksh 1,700",
    "mogogosiek": "Mogogosiek Route Bus Fares (per month):\n- Murram: Ksh 2,600\n- Mogogosiek: Ksh 2,500\n- Boito (Kaptien Rd): Ksh 1,850\n- Boito (Shopping Center): Ksh 1,600\n- Chemoiben: Ksh 1,400\n- D.C. Residence: Ksh 1,050",

    # Term II Activities
    "trip": "Term II 2026 Educational Trips:\n- Grade 4: Maasai Mara - Ksh 2,500\n- Grade 5: Nakuru - 1st April 2026\n- Grade 6: Naivasha - Ksh 3,500\n- Grade 7: Nairobi - Ksh 5,000\n- Grade 8: Mombasa - Ksh 15,000",
    "excursion": "Term II 2026 Educational Trips:\n- Grade 4: Maasai Mara - Ksh 2,500\n- Grade 5: Nakuru - 1st April 2026\n- Grade 6: Naivasha - Ksh 3,500\n- Grade 7: Nairobi - Ksh 5,000\n- Grade 8: Mombasa - Ksh 15,000",
    "educational": "Term II 2026 Educational Trips:\n- Grade 4: Maasai Mara - Ksh 2,500\n- Grade 5: Nakuru - 1st April 2026\n- Grade 6: Naivasha - Ksh 3,500\n- Grade 7: Nairobi - Ksh 5,000\n- Grade 8: Mombasa - Ksh 15,000",
    "nairobi": "Grade 7 will visit Nairobi for their educational trip. Cost: Ksh 5,000.",
    "mombasa": "Grade 8 will visit Mombasa for their educational trip. Cost: Ksh 15,000.",
    "naivasha": "Grade 6 will visit Naivasha for their educational trip. Cost: Ksh 3,500.",
    "nakuru": "Grade 5 will visit Nakuru for their educational trip on 1st April 2026.",
    "maasai": "Grade 4 will visit Maasai Mara for their educational trip. Cost: Ksh 2,500.",

    # Parental engagement
    "meeting": "Parental Engagement Days Term II 2026:\n- Grade 5: 16th May 2026\n- Grade 4: 23rd May 2026\n- Grade 3: 30th May 2026\n- Grade 2: 6th June 2026\n- Grade 1: 13th June 2026\n- PP1 & PP2: 20th June 2026\nHalf Term: 24th June to 28th June 2026.",
    "parent": "Parental Engagement Days Term II 2026:\n- Grade 5: 16th May 2026\n- Grade 4: 23rd May 2026\n- Grade 3: 30th May 2026\n- Grade 2: 6th June 2026\n- Grade 1: 13th June 2026\n- PP1 & PP2: 20th June 2026",
    "engagement": "Parental Engagement Days Term II 2026:\n- Grade 5: 16th May 2026\n- Grade 4: 23rd May 2026\n- Grade 3: 30th May 2026\n- Grade 2: 6th June 2026\n- Grade 1: 13th June 2026\n- PP1 & PP2: 20th June 2026",
    "half term": "Half Term holiday is from 24th June to 28th June 2026.",
    "holiday": "Half Term holiday is from 24th June to 28th June 2026.",
    "when": "Please specify what you would like to know the date for. You can ask about parental engagement days, half term, trips or opening day.",
    "date": "Please specify what date you need. You can ask about parental engagement days, half term, trips or opening day.",

    # ICT Programme
    "ict": "ICT Digiskool Programme (Coding, Robotics & AI) for Grade 1-9 is fully implemented from Term II 2026. Termly fee of Ksh 1,500 is included in school fees.",
    "coding": "ICT Digiskool Programme (Coding, Robotics & AI) for Grade 1-9 is fully implemented from Term II 2026. Termly fee of Ksh 1,500 included in school fees.",
    "robotics": "ICT Digiskool Programme (Coding, Robotics & AI) for Grade 1-9. Termly fee of Ksh 1,500 included in school fees.",
    "digiskool": "Digiskool Programme (Coding, Robotics & AI) for Grade 1-9 fully implemented from Term II 2026. Termly fee of Ksh 1,500 included in school fees.",
    "computer": "ICT Digiskool Programme (Coding, Robotics & AI) for Grade 1-9 is fully implemented from Term II 2026. Termly fee of Ksh 1,500 included in school fees.",
    "technology": "ICT Digiskool Programme (Coding, Robotics & AI) for Grade 1-9 is fully implemented from Term II 2026. Termly fee of Ksh 1,500 included in school fees.",

    # Opening day
    "opening": "School opened officially on Wednesday 29th April 2026 at 7:30am for day scholars. Boarders reported on Tuesday 28th April 2026 from 2:00pm.",
    "open": "School opened officially on Wednesday 29th April 2026 at 7:30am for day scholars.",
    "boarder": "Boarders reported on Tuesday 28th April 2026 from 2:00pm.",
    "reopen": "For Term III opening dates please contact the school office directly.",
    "term": "For term dates please contact the school office directly.",

    # General
    "address": "Sally-Ann School Limited\nP.O. Box 401, Litein.",
    "location": "Sally-Ann School Limited is located in Litein. P.O. Box 401, Litein.",
    "contact": "For more enquiries contact Sally-Ann School Limited, P.O. Box 401, Litein.",
    "where": "Sally-Ann School Limited is located in Litein. P.O. Box 401, Litein.",
    "thank": "You are welcome! Feel free to ask if you need anything else.",
    "thanks": "You are welcome! Feel free to ask if you need anything else.",
    "asante": "Karibu! Uliza swali lolote kuhusu ada, basi au shughuli za shule.",
}

def find_best_response(message):
    message = message.lower().strip()

    # First try exact keyword match
    for keyword, response in responses.items():
        if keyword in message:
            return response

    # Then try fuzzy matching
    best_match = None
    best_score = 0
    for keyword in responses:
        score = fuzz.partial_ratio(keyword, message)
        if score > best_score:
            best_score = score
            best_match = keyword

    if best_score >= 70:
        return responses[best_match]

    return "Sorry, I did not understand that. You can ask about fees, bus fares, payment details, trips, parental engagement days or ICT programme. For more help contact Sally-Ann School Limited, P.O. Box 401, Litein."

@app.route("/")
def home():
    return "Sally-Ann School WhatsApp Bot is running!"

@app.route("/webhook", methods=["POST"])
def webhook():
    incoming_message = request.form.get("Body", "").lower().strip()
    reply = find_best_response(incoming_message)
    resp = MessagingResponse()
    resp.message(reply)
    return str(resp)

if __name__ == "__main__":
    from waitress import serve
    serve(app, host="0.0.0.0", port=8080)