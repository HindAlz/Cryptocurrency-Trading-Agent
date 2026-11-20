import json
from client import LedgerClient

with open("config.local.json", encoding="utf-8") as file:
    config = json.load(file)

guardian = LedgerClient(
    config["LEDGER_BASE_URL"],
    config["GUARDIAN_API_KEY"]
)

policy = guardian.get_policy()

print(json.dumps(policy, indent=2))