"""Save evaluation session metadata and portfolio samples."""

import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path

class SessionRecorder:
    def __init__(
            self, output_dir, *, member, account_label, symbol,
            theory_id, theory_version, sampling_interval_seconds=30):
        if sampling_interval_seconds <= 0:
            raise ValueError("Sampling interval must be positive.")

        self.started_at = time.time()
        self.finished = False

        folder = Path(output_dir)
        folder.mkdir(parents=True, exist_ok=True)

        filename = datetime.now(timezone.utc).strftime("session_%Y%m%dT%H%M%S_%fZ.jsonl")
        self.path = folder / filename

        self._write({
            "event": "session_start",
            "timestamp_unix": self.started_at,
            "member": member,
            "account_label": account_label,
            "symbol": symbol,
            "theory_id": theory_id,
            "theory_version": theory_version,
            "sampling_interval_seconds": sampling_interval_seconds,
        })

    def  _write(self, record):
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, allow_nan=False) + "\n")

    def record_portfolio(self, value_usd):
        if self.finished:
            raise ValueError("This session has already ended.")

        if (
            isinstance(value_usd, bool)
            or not isinstance(value_usd, (int, float))
            or not math.isfinite(value_usd)
            or value_usd <= 0
        ):
            raise ValueError("Portfolio value must be finite and positive.")

        self._write({
            "event": "portfolio_sample",
            "timestamp_unix": time.time(),
            "value_usd": value_usd,
        })

    def finish(self):
        if self.finished:
            return

        ended_at = time.time()
        self._write({
            "event": "session_end",
            "timestamp_unix": ended_at,
            "duration_seconds": ended_at - self.started_at,
        })
        self.finished = True