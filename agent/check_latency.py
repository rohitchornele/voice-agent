import json, os, time, urllib.request, urllib.error
from dotenv import load_dotenv

load_dotenv()
BASE = "https://generativelanguage.googleapis.com/v1beta/openai"
KEY = os.environ["GEMINI_API_KEY"]
H = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}

def run(model):
    body = {
        "model": model,
        "stream": True,
        "messages": [
            {"role": "system", "content": "You are a phone support agent. Reply in one short sentence."},
            {"role": "user", "content": "Hi, my internet is down."},
        ],
    }
    req = urllib.request.Request(BASE + "/chat/completions",
                                 data=json.dumps(body).encode(), headers=H)
    t0 = time.time()
    first = None
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            for line in r:
                line = line.decode().strip()
                if not line.startswith("data:") or line.endswith("[DONE]"):
                    continue
                d = json.loads(line[5:])["choices"][0]["delta"].get("content")
                if d and first is None:
                    first = (time.time() - t0) * 1000
        return f"first token {first:.0f} ms"
    except urllib.error.HTTPError as e:
        return f"FAILED {e.code}: {e.read().decode()[:150]}"

# Network baseline: a cheap call with no model inference
print("network baseline (GET /models):")
for _ in range(3):
    t = time.time()
    urllib.request.urlopen(urllib.request.Request(BASE + "/models", headers=H), timeout=15).read()
    print(f"   {(time.time()-t)*1000:.0f} ms")

for m in ["gemini-3.1-flash-lite", "gemini-2.5-flash-lite",
          "gemini-3.5-flash-lite", "gemini-flash-lite-latest"]:
    print(m)
    for _ in range(3):
        print("  ", run(m))