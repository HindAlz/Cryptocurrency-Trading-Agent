"""Append-only evaluation records. Never include keys or private configuration."""
import json
import math
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


class SessionRecorder:
    def __init__(self, output_dir, *, member, account_label, symbol, theory_id,
                 theory_version, sampling_interval_seconds=30, metadata=None):
        if (isinstance(sampling_interval_seconds, bool)
                or not isinstance(sampling_interval_seconds, (int, float))
                or not math.isfinite(sampling_interval_seconds)
                or sampling_interval_seconds <= 0):
            raise ValueError("Sampling interval must be finite and positive")
        for value in (member, account_label, symbol):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("Member, account label and currency are required")
        self.started_at = time.time()
        self.finished = False
        self.sample_count = 0
        self.last_sample_at = None
        folder = Path(output_dir)
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
        self.path = folder / f"session_{stamp}_{uuid.uuid4().hex[:8]}.jsonl"
        # Exclusive creation prevents mixing records from two sessions.
        self.path.touch(exist_ok=False)
        self._write({"event": "session_start", "timestamp_unix": self.started_at,
                     "member": member, "account_label": account_label, "symbol": symbol,
                     "theory_id": theory_id, "theory_version": theory_version,
                     "sampling_interval_seconds": sampling_interval_seconds,
                     "metadata": metadata or {}})

    def _write(self, record):
        encoded = json.dumps(record, allow_nan=False)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(encoded + "\n")
            handle.flush()

    def event(self, name, **fields):
        if self.finished:
            raise ValueError("Session has ended")
        self._write({"event": name, "timestamp_unix": time.time(), **fields})

    def record_portfolio(self, value_usd, *, timestamp_unix=None, **fields):
        if self.finished:
            raise ValueError("Session has ended")
        if (isinstance(value_usd, bool) or not isinstance(value_usd, (int, float))
                or not math.isfinite(value_usd) or value_usd <= 0):
            raise ValueError("Portfolio value must be finite and positive")
        stamp = time.time() if timestamp_unix is None else timestamp_unix
        if (isinstance(stamp, bool) or not isinstance(stamp, (int, float))
                or not math.isfinite(stamp)):
            raise ValueError("Invalid sample timestamp")
        if self.last_sample_at is not None and stamp < self.last_sample_at:
            raise ValueError("Sample timestamps moved backwards")
        self._write({"event": "portfolio_sample", "timestamp_unix": stamp,
                     "value_usd": value_usd, **fields})
        self.last_sample_at = stamp
        self.sample_count += 1

    def finish(self, *, status="complete", error_type=None):
        if self.finished:
            return
        if status not in {"complete", "incomplete", "interrupted", "failed"}:
            raise ValueError("Invalid recording status")
        ended = time.time()
        self._write({"event": "session_end", "timestamp_unix": ended,
                     "duration_seconds": ended - self.started_at, "status": status,
                     "sample_count": self.sample_count,
                     "last_successful_sample_unix": self.last_sample_at,
                     "error_type": error_type})
        self.finished = True
