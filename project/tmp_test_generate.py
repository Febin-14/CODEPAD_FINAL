import os
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv(override=True)
api_key = os.environ.get("GEMINI_API_KEY")
genai.configure(api_key=api_key)

try:
    model = genai.GenerativeModel('gemini-2.0-flash')
    print("SUCCESS:", model.generate_content("What is 5+5?").text[:20])
except Exception as e:
    print("ERROR:", e)
