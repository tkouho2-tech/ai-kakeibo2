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

model_name = "gemini-1.5-flash"
prompt = "手入力の使い方は？"
system_prompt = """
あなたは「AI家計簿 Pro」のヘルプアシスタントです。以下のアプリ機能に基づいてユーザーの質問に答えてください。
(Truncated for brevity, assuming the rest matches app.py context)
"""

print(f"Testing model: {model_name}")

try:
    model = genai.GenerativeModel(model_name)
    print("Model initialized.")
    # Replicating the exact call from app.py
    response = model.generate_content([system_prompt, prompt])
    print(f"Success! Response: {response.text}")
except Exception as e:
    print(f"ERROR OCCURRED: {e}")
    import traceback
    traceback.print_exc()
