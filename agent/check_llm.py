import json, os, time, urllib.request
from dotenv import load_dotenv

load_dotenv()

body = {
    "model": os.environ["LLM_MODEL"],
    "messages": [
        {
            "role": "system",
            "content": "You are a support agent. Reply in one short sentence.",
        },
        {"role": "user", "content": "Hi, my internet is down."},
    ],
}
req = urllib.request.Request(
    "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
    data=json.dumps(body).encode(),
    headers={
        "Authorization": f"Bearer {os.environ['GEMINI_API_KEY']}",
        "Content-Type": "application/json",
    },
)
t = time.time()
try:
    with urllib.request.urlopen(req, timeout=30) as r:
        out = json.load(r)
    print(f"OK in {(time.time()-t)*1000:.0f} ms")
    print(out["choices"][0]["message"]["content"])
except urllib.error.HTTPError as e:
    print("FAILED", e.code, e.read().decode())
