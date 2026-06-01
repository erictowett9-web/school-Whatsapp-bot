from groq import Groq

api_key = "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"
print(f"Using key: {api_key}")

client = Groq(api_key=api_key)
response = client.chat.completions.create(
    messages=[{"role": "user", "content": "hello"}],
    model="llama-3.3-70b-versatile"
)
print(response.choices[0].message.content)