import json, os, urllib.request
from dotenv import load_dotenv

load_dotenv()


def get(url, headers):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


# Gemini: list models through the OpenAI-compatible endpoint
try:
    data = get(
        "https://generativelanguage.googleapis.com/v1beta/openai/models",
        {"Authorization": f"Bearer {os.environ['GEMINI_API_KEY']}"},
    )
    ids = [m["id"] for m in data["data"]]
    print("Gemini key OK. Models containing '3.1' or 'flash-lite':")
    for i in ids:
        if "3.1" in i or "flash-lite" in i:
            print("  ", i)
except Exception as e:
    print("Gemini FAILED:", e)

# Deepgram: list projects (valid key returns 200)
try:
    get(
        "https://api.deepgram.com/v1/projects",
        {"Authorization": f"Token {os.environ['DEEPGRAM_API_KEY']}"},
    )
    print("Deepgram key OK")
except Exception as e:
    print("Deepgram FAILED:", e)
