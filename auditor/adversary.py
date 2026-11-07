import os
import json
from openai import OpenAI

client = OpenAI(
    api_key=os.environ["SUBQ_API_KEY"],
    base_url="https://api.subq.ai/v1"
)

def generate_attack():
    response = client.chat.completions.create(
        model="subq-preview",
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a red-team tester for a simulated trading broker. "
                    "Generate one failure scenario as valid JSON only. "
                    "Use keys: match, nth, fault. "
                    "Allowed faults: accept_then_timeout, "
                    "accept_then_500, transient_500, stale_status, "
                    "partial_fill_then_cancel, real_reject."
                    "IMPORTANT: The match field must be a string containing "
                    "the HTTP method and broker endpoint, such as "
                    "'POST /v2/orders'. It must never be an object. "
                    "Return only a JSON object with exactly these keys: "
                    "match, nth, fault."
                )
            },
            {
                "role": "user",
                "content": "Generate a scenario that could expose a bug in trade execution."
            }
        ]
    )

    raw_output = response.choices[0].message.content

    attack = json.loads(raw_output)

    allowed_faults = {
        "accept_then_timeout",
        "accept_then_500",
        "transient_500",
        "stale_status",
        "partial_fill_then_cancel",
        "real_reject",
    }

    if not isinstance(attack, dict):
        raise ValueError("Attack must be a JSON object")

    if attack.get("match") != "POST /v2/orders":
        raise ValueError("Invalid broker endpoint")

    if type(attack.get("nth")) is not int or attack["nth"] < 1:
        raise ValueError("nth must be a positive integer")

    if attack.get("fault") not in allowed_faults:
        raise ValueError("Unsupported broker fault")

    return attack


if __name__ == "__main__":
    attack = generate_attack()
    print(attack)


    