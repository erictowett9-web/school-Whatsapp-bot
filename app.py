from flask import Flask, request
from flask_sqlalchemy import SQLAlchemy
from twilio.twiml.messaging_response import MessagingResponse
from dotenv import load_dotenv
from google import genai
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

client = genai.Client(api_key=os.getenv('GEMINI_API_KEY'))

SCHOOL_CONTEXT = """
You are a helpful school assistant chatbot for parents.
You help parents with enquiries about:
- School fees and payments (due on 5th of every month, M-Pesa Paybill 123456)
- Attendance and absences (call 0700 000000 before 8am)
- Exam timetables and results (released within 2 weeks after exams)
- School events and parent-teacher meetings (last Friday of every term at 2pm)
- Transport and bus routes (pick-up 6:30am, drop-off 4:30pm)
- Uniform (available at school store 8am-4pm weekdays)
- General school information

Always be friendly, brief and helpful. If you don't know something specific,
tell the parent to call the school office on 0700 000000 or email info@school.com.
Reply in the same language the parent uses.
Keep replies short — maximum 3 sentences.
"""

@app.route("/")
def home():
    return "School WhatsApp Bot is running!"

@app.route("/webhook", methods=["POST"])
def webhook():
    incoming_message = request.form.get("Body", "").strip()

    try:
        response = client.models.generate_content(
            model='gemini-2.0-flash',
            contents=SCHOOL_CONTEXT + "\n\nParent asks: " + incoming_message
        )
        reply = response.text
    except Exception as e:
        reply = "Sorry, I am having trouble right now. Please call the school office on 0700 000000."

    resp = MessagingResponse()
    resp.message(reply)
    return str(resp)

if __name__ == "__main__":
    from waitress import serve
    serve(app, host="0.0.0.0", port=8080)
    @app.route("/webhook", methods=["POST"])
def webhook():
    incoming_message = request.form.get("Body", "").strip()

    try:
        response = client.models.generate_content(
            model='gemini-2.0-flash',
            contents=SCHOOL_CONTEXT + "\n\nParent asks: " + incoming_message
        )
        reply = response.text
    except Exception as e:
        print(f"GEMINI ERROR: {str(e)}")
        reply = f"Error: {str(e)[:100]}"

    resp = MessagingResponse()
    resp.message(reply)
    return str(resp)