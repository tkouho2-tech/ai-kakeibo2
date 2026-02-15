import google.generativeai as genai
import os
import toml

# Load secrets
try:
    secrets = toml.load(".streamlit/secrets.toml")
    os.environ["GEMINI_API_KEY"] = secrets["GEMINI_API_KEY"]
    genai.configure(api_key=os.environ["GEMINI_API_KEY"])
    print("Secrets loaded.")
except Exception as e:
    print(f"Error loading secrets: {e}")
    exit(1)

models_to_test = [
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
    "gemini-2.0-flash-lite-001",
    "gemini-flash-latest",
    "gemini-pro-latest",
    "gemini-2.0-flash-exp",
    "gemini-2.5-flash"
]

prompt = "手入力の使い方は？"
system_prompt = "あなたはAI家計簿のアシスタントです。"

print("-" * 30)
for model_name in models_to_test:
    print(f"Testing model: {model_name}")
    try:
        model = genai.GenerativeModel(model_name)
        response = model.generate_content([system_prompt, prompt])
        print(f"SUCCESS! Response: {response.text[:50]}...")
    except Exception as e:
        print(f"FAILED: {e}")
    print("-" * 30)
