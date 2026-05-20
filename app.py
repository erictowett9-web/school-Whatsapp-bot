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
    

with app.app_context():
    db.create_all()

class Conversation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_message = db.Column(db.String(200))
    bot_response = db.Column(db.String(200))
    timestamp = db.Column(db.DateTime, default=db.func.current_timestamp())

# Parent responses
responses = {
    "fee": "School fees are due on the 5th of every month. Pay via M-Pesa Paybill 123456, Account: admission number.",
    "pay": "You can pay fees via M-Pesa Paybill 123456. Use your child's admission number as the account.",
    "balance": "For fee balance enquiries, contact the bursar at bursar@school.com or call 0700 000000.",
    "absent": "To report an absence, call 0700 000000 before 8am or email attendance@school.com.",
    "late": "Late arrivals must be signed in at the front office. Please bring a written note.",
    "sick": "If your child is sick, call 0700 000000 before 8am to inform the class teacher.",
    "exam": "The next exam timetable will be shared via WhatsApp group and school portal.",
    "result": "Results are released within 2 weeks after exams via the parent portal.",
    "grade": "For grade enquiries, contact your child's class teacher directly.",
    "event": "Upcoming school events are posted on our WhatsApp group every Monday.",
    "meeting": "Parent-teacher meetings are held every last Friday of the term at 2pm.",
    "transport": "For bus route and pick-up time enquiries, call the transport office at 0700 000001.",
    "bus": "Bus pick-up is at 6:30am. Drop-off after school is at 4:30pm.",
    "uniform": "School uniforms are available at the school store every weekday 8am to 4pm.",
    "hello": "Hello! Welcome to the school enquiry bot. How can I help you today?",
    "hi": "Hi there! I am the school assistant. Ask me about fees, exams, attendance or events.",
}

@app.route("/")
def home():
    return "School WhatsApp Bot is running!"

@app.route("/webhook", methods=["POST"])
def webhook():
    incoming_message = request.form.get("Body", "").lower().strip()
    sender = request.form.get("From", "")

    # Find matching response
    reply = next(
        (v for k, v in responses.items() if k in incoming_message),
        "Sorry, I did not understand that. You can ask about fees, attendance, exams, events, transport or uniform."
    )

    # Save to database
    conv = Conversation(user_message=incoming_message, bot_response=reply)
    db.session.add(conv)
    db.session.commit()

    # Send reply via Twilio
    resp = MessagingResponse()
    resp.message(reply)
    return str(resp)

if __name__ == "__main__":
    from waitress import serve
    serve(app, host="0.0.0.0", port=8080)
