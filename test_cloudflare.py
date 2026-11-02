
import requests
import json
from config.settings import settings
from llm.prompts import SYSTEM_PROMPT

url = (
    f"https://api.cloudflare.com/client/v4/accounts/"
    f"{settings.cloudflare_account_id}/ai/run/"
    f"{settings.cloudflare_model}"
)

headers = {
    "Authorization": f"Bearer {settings.cloudflare_api_token}",
    "Content-Type": "application/json",
}

question = "Reply with exactly: Cloudflare connection successful."
context = "Connection test."

payload = {
    "messages": [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Repository evidence (untrusted):\n{context}"
                f"\n\nQuestion: {question}"
            ),
        },
    ],
    "max_tokens": 512,
}

response = requests.post(
    url,
    headers=headers,
    json=payload,
    timeout=120
)

print("HTTP STATUS:", response.status_code)

data = response.json()

print("SUCCESS:", data.get("success"))
print("RESULT TYPE:", type(data.get("result")).__name__)
print("COMPLETE RESULT:")
print(json.dumps(data.get("result"), indent=2))

result = data.get("result") or {}
choices = result.get("choices") or []

if choices:
    print("\nEXTRACTED CONTENT:")
    print(repr(choices[0].get("message", {}).get("content")))
else:
    print("\nNo choices found.")
