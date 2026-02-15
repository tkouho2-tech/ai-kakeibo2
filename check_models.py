import google.generativeai as genai
import re
import os

try:
    api_key = None
    with open(".streamlit/secrets.toml", "r", encoding="utf-8") as f:
        content = f.read()
        match = re.search(r'GEMINI_API_KEY\s*=\s*"([^"]+)"', content)
        if match:
            api_key = match.group(1)
    
    if not api_key:
        print("API Key not found or could not be parsed from .streamlit/secrets.toml")
        exit(1)
    
    genai.configure(api_key=api_key)
    
    print("Listing available models with '1.5' or 'flash' in name...")
    for m in genai.list_models():
        if 'generateContent' in m.supported_generation_methods:
            if '1.5' in m.name or 'flash' in m.name:
                print(f"Model: {m.name}")
            
except Exception as e:
    print(f"Error: {e}")
