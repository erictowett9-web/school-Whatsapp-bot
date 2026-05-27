from flask import Flask, request
from flask_sqlalchemy import SQLAlchemy
from twilio.twiml.messaging_response import MessagingResponse
from dotenv import load_dotenv
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
    "hello": "Hello! Welcome to Sally-Ann School Limited. How can I help you today? You can ask about fees, bus fares, payment details, or any school enquiry.",
    "hi": "Hi there! Welcome to Sally-Ann School Limited. Ask me about fees, bus fares, or payment details.",

    # Fees
    "fee": "2026 Fees Structure:\n- Grade 1 & 2: Ksh 15,500 per term\n- ICT (Coding & Robotics): Ksh 1,500 per term\n- TOTAL: Ksh 17,000 per term\n- New admissions: Additional Ksh 2,000 registration fee.\nAt least 60% of fees must be paid on Reporting Day.",
    "grade": "2026 Fees Structure:\n- Grade 1 & 2: Ksh 15,500 per term\n- ICT (Coding & Robotics): Ksh 1,500 per term\n- TOTAL: Ksh 17,000 per term",
    "registration": "New admissions require a registration fee of Ksh 2,000 to be banked together with school fees.",
    "reporting": "At least 60% of school fees must be paid on Reporting Day. No cash will be accepted — bank all payments.",

    # Payment details
    "pay": "Payment options for Sally-Ann School:\n1. M-Pesa Paybill: 777643, A/c: ADM No.\n2. KCB: A/c No. 1135294917\n3. Chai Sacco: A/c No. 1083225 (Litein branch)\n4. Coop Bank: A/c No. 01148786054900\n5. Equity Bank: A/c No. 0530291926992\n6. Equity Paybill: 247247, A/c: 926992#ADM No.\nNote: No cash accepted. Bank fees and bus fare in school account only.",
    "mpesa": "M-Pesa Paybill: 777643\nAccount Number: Your child's ADM No.\nNote: No cash accepted.",
    "kcb": "KCB Account Number: 1135294917\nNote: No cash accepted.",
    "equity": "Equity Bank Account: 0530291926992\nEquity Paybill: 247247, A/c: 926992#ADM No.\nNote: No cash accepted.",
    "coop": "Cooperative Bank Account: 01148786054900\nNote: No cash accepted.",
    "sacco": "Chai Sacco Account: 1083225 (Litein branch)\nNote: No cash accepted.",
    "cash": "No cash will be accepted. All bus fare and school fees must be banked in the school account only.",

    # Bus fares
    "bus": "2026 Bus Fare Rates (per month):\nKAPKATET ROUTE:\n- Koitabai: Ksh 2,300\n- Kapkatet (Daraja Sita): Ksh 1,950\n- Kapkatet Factory: Ksh 1,850\n- Kabianga/Kapkatet Town: Ksh 1,600\n- Chematich: Ksh 1,850\n- Kapkatolonyi: Ksh 1,250\n- Kaptote: Ksh 1,150\n- Koiwa Road/D.C. Junction: Ksh 950\nReply with your route name for more fares.",
    "transport": "We have 4 bus routes: Kapkatet, Litein, Tebesonik and Chemosot/Mogogosiek routes. Reply with your route name for fares.",
    "fare": "Reply with your route name for specific bus fares. Routes available: Kapkatet, Litein, Tebesonik, Chemosot, Mogogosiek.",
    "kapkatet": "Kapkatet Route Bus Fares (per month):\n- Koitabai: Ksh 2,300\n- Kapkatet (Daraja Sita): Ksh 1,950\n- Kapkatet Factory: Ksh 1,850\n- Kabianga/Kapkatet Town: Ksh 1,600\n- Chematich: Ksh 1,850\n- Kapkatolonyi: Ksh 1,250\n- Kaptote: Ksh 1,150\n- Koiwa Road/D.C. Junction: Ksh 950",
    "litein": "Litein Route Bus Fares (per month):\n- Litein Town/St. Kizito's: Ksh 950\n- Factory Gate: Ksh 1,050\n- Kwa Soi & Kwa Chirchir & Joyland: Ksh 1,150\n- Imarisha: Ksh 1,150\n- Kusumek: Ksh 1,600",
    "tebesonik": "Tebesonik Route Bus Fares (per month):\n- Lalagin: Ksh 1,250\n- Kiptewit Junction: Ksh 1,500\n- Cheborge Centre: Ksh 1,600\n- Korongoi: Ksh 1,700\n- Bokoiyot/Siongi/Tebesoni K Factory: Ksh 2,300",
    "chemosot": "Chemosot Route Bus Fares (per month):\n- Cheluget: Ksh 1,250\n- Chelilis/Chesingoro: Ksh 1,600\n- Kaminjeiwet/Getarwet Junction: Ksh 1,700",
    "mogogosiek": "Mogogosiek Route Bus Fares (per month):\n- Murram: Ksh 2,600\n- Mogogosiek: Ksh 2,500\n- Boito (Kaptien Rd): Ksh 1,850\n- Boito (Shopping Center): Ksh 1,600\n- Chemoiben: Ksh 1,400\n- D.C. Residence: Ksh 1,050",

    # General
    "address": "Sally-Ann School Limited\nP.O. Box 401, Litein.",
    "location": "Sally-Ann School Limited is located in Litein. P.O. Box 401, Litein.",
    "contact": "For more enquiries contact Sally-Ann School Limited, P.O. Box 401, Litein.",
}

@app.route("/")
def home():
    return "Sally-Ann School WhatsApp Bot is running!"

@app.route("/webhook", methods=["POST"])
def webhook():
    incoming_message = request.form.get("Body", "").lower().strip()

    reply = next(
        (v for k, v in responses.items() if k in incoming_message),
        "Sorry, I did not understand that. You can ask about fees, bus fares, payment details, or school location. For more help call the school office."
    )

    resp = MessagingResponse()
    resp.message(reply)
    return str(resp)

if __name__ == "__main__":
    from waitress import serve
    serve(app, host="0.0.0.0", port=8080)