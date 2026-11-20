"""Manual read-only check; importing this file never reads keys or calls Ledger."""
import json
from client import LedgerClient


def main():
    with open("config.local.json", encoding="utf-8") as file:
        config = json.load(file)
    guardian = LedgerClient(config["LEDGER_BASE_URL"], config["GUARDIAN_API_KEY"])
    print(json.dumps(guardian.get_policy(), indent=2))


if __name__ == "__main__":
    main()
