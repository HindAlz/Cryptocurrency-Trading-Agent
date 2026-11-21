"""Private Ledger configuration. Model configuration stays in its own file."""
import json
from pathlib import Path
from urllib.parse import urlsplit


def load_ledger_config(path, mode):
    if mode not in {"trader", "guardian", "both", "review", "recover"}:
        raise ValueError("Unknown mode")
    path = Path(path)
    if path.stat().st_size > 32768:
        raise ValueError("Ledger configuration exceeds 32 KiB")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate configuration field")
            result[key] = value
        return result
    data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)
    allowed = {"LEDGER_BASE_URL", "TRADER_API_KEY", "GUARDIAN_API_KEY"}
    if not isinstance(data, dict) or set(data) - allowed:
        raise ValueError("Use only documented Ledger settings; keep model settings separate")
    if any(not isinstance(v, str) or not v.strip() for v in data.values()):
        raise ValueError("Ledger configuration values must be non-empty strings")
    base = data.get("LEDGER_BASE_URL", "")
    url = urlsplit(base)
    if (url.scheme != "https" or not url.hostname or url.username or url.password
            or url.query or url.fragment):
        raise ValueError("Use the canonical HTTPS Ledger API base URL")
    needed = {"trader": ["TRADER_API_KEY"], "guardian": ["GUARDIAN_API_KEY"],
              "review": ["GUARDIAN_API_KEY"], "both": ["TRADER_API_KEY", "GUARDIAN_API_KEY"],
              "recover": []}[mode]
    for name in needed:
        if name not in data or data[name].startswith(("REPLACE", "PASTE", "YOUR_")):
            raise ValueError(f"Configure the complete {name}")
    if mode == "recover" and not any(data.get(k) for k in ("TRADER_API_KEY", "GUARDIAN_API_KEY")):
        raise ValueError("Recovery needs one role's read-capable key")
    return {key: value.strip() for key, value in data.items()}
