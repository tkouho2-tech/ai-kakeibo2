import google.generativeai as genai
import os
import streamlit as st

# Function to load secrets independently
def load_secrets():
    try:
        import toml
        secrets = toml.load(".streamlit/secrets.toml")
        os.environ["GEMINI_API_KEY"] = secrets["GEMINI_API_KEY"]
        genai.configure(api_key=os.environ["GEMINI_API_KEY"])
        return True
    except Exception as e:
        print(f"Error loading secrets: {e}")
        return False

if load_secrets():
    print("Secrets loaded.")
    
    models_to_test = [
        "gemini-1.5-flash",
        "gemini-1.5-pro",
        "gemini-pro",
        "gemini-flash-latest"
    ]
    
    print("-" * 20)
    for model_name in models_to_test:
        print(f"Testing {model_name}...")
        try:
            model = genai.GenerativeModel(model_name)
            response = model.generate_content("Hello")
            print(f"Success! Response: {response.text.strip()}")
        except Exception as e:
            print(f"Failed: {e}")
        print("-" * 20)
else:
    print("Could not load secrets.")
