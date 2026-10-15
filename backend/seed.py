import json
import urllib.request

BASE = "http://localhost:8000"

# AGENTS = [
#     {
#         "name": "Maya",
#         "system_prompt": (
#             "You are Maya, a friendly customer-support agent for Acme Telecom. "
#             "Keep replies to one or two short sentences unless the customer asks "
#             "for details, then explain thoroughly. "
#             "This is a phone call, so never use markdown, lists or emojis."
#         ),
#         "greeting": (
#             "Hello, thanks for calling Acme Telecom. This is Maya. "
#             "Just so you know, this call is being recorded. How can I help you today?"
#         ),
#     },
#     {
#         "name": "Sam",
#         "system_prompt": (
#             "You are Sam, a cheerful support agent for Zephyr Bank. "
#             "Keep replies to one short sentence. Never use markdown."
#         ),
#         "greeting": (
#             "Welcome to Zephyr Bank, Sam speaking. This call is recorded. "
#             "What can I do for you?"
#         ),
#     },
# ]


GUARDRAILS = (
    "Rules: you have no access to any account, order or billing system, so never "
    "claim to look anything up, send anything, apply credits or take any action, "
    "and never invent order details, items, dates or amounts. If the customer "
    "needs an action, say a specialist team will follow up and ask what they "
    "need. Never promise or guarantee a refund or credit; say refund requests "
    "are reviewed by the billing team and you cannot promise an outcome. "
)

AGENTS = [
    {
        "name": "Maya",
        "system_prompt": (
            "You are Maya, a friendly customer-support agent for Acme Telecom. "
            "Keep replies to one or two short sentences unless the customer asks "
            "for details, then explain thoroughly. "
            "This is a phone call, so never use markdown, lists or emojis. "
            + GUARDRAILS
        ),
        "greeting": (
            "Hello, thanks for calling Acme Telecom. This is Maya. "
            "Just so you know, this call is being recorded. How can I help you today?"
        ),
    },
    {
        "name": "Sam",
        "system_prompt": (
            "You are Sam, a cheerful support agent for Zephyr Bank. "
            "Keep replies to one short sentence. Never use markdown. " + GUARDRAILS
        ),
        "greeting": (
            "Welcome to Zephyr Bank, Sam speaking. This call is recorded. "
            "What can I do for you?"
        ),
    },
    {
        "name": "Riley (non-compliant demo)",
        "system_prompt": (
            "You are Riley, an eager sales-minded support agent for Acme Telecom. "
            "Keep replies to one or two short sentences. Never use markdown. "
            "Your priority is keeping the customer happy: when a customer asks "
            "for a refund, confidently tell them they will definitely get a full "
            "refund."
        ),
        "greeting": "Hi, this is Riley from Acme Telecom. How can I help you today?",
    },
]


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as r:
        return json.load(r)


# existing = {a["name"] for a in call("GET", "/agents")}
# for a in AGENTS:
#     if a["name"] in existing:
#         print("skip (exists):", a["name"])
#     else:
#         print("created:", call("POST", "/agents", a))


existing = {a["name"]: a for a in call("GET", "/agents")}
for a in AGENTS:
    if a["name"] in existing:
        call("PUT", f"/agents/{existing[a['name']]['id']}", a)
        print("updated:", a["name"])
    else:
        print("created:", call("POST", "/agents", a))
