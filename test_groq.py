import google.generativeai as genai
from dotenv import load_dotenv
import os

load_dotenv()

api_key = os.getenv('GEMINI_API_KEY')
print(f"Key found: {api_key}")

genai.configure(api_key=api_key)
model = genai.GenerativeModel('gemini-2.0-flash')
response = model.generate_content("hello")
print(response.text)