import google.generativeai as genai
import os
import toml

# Load secrets
try:
    secrets = toml.load(".streamlit/secrets.toml")
    os.environ["GEMINI_API_KEY"] = secrets["GEMINI_API_KEY"]
    genai.configure(api_key=os.environ["GEMINI_API_KEY"])
except Exception as e:
    print(f"Error loading secrets: {e}")
    exit(1)

print("Listing available models...")
try:
    for m in genai.list_models():
        if 'generateContent' in m.supported_generation_methods:
            print(m.name)
except Exception as e:
    print(f"Error listing models: {e}")
