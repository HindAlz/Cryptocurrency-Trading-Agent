"""Persistent local state for Ledger agents.

Stores:
- Immutable theory snapshots.
- Proposal intent before submission.
- Ledger transaction IDs after submission.
- Uncertain writes and order outcomes.

Does not store credentials or perform Ledger reconciliation.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


FINAL_STATUSES = {"EXECUTED", "BLOCKED", "REJECTED"}

LEDGER_STATUSES = FINAL_STATUSES | {
    "PENDING",
    "ESCALATED"
}

LOCAL_STATUSES = {
    "PREPARED",
    "SUBMITTING",
    "UNCERTAIN",
    "CANCELLED"
}

# ESCALATED remains unresolved until the human decision is confirmed.
RESOLVED_STATUSES = FINAL_STATUSES | {"CANCELLED"}


class StateError(RuntimeError):
    """Invalid state operation or conflicting stored information."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _positive_id(value, name):
    if type(value) is not int or value <= 0:
        raise StateError(f"{name} must be a positive integer.")
    return value


def _json(value):
    """Strict JSON storage with an additional credential-field safeguard."""

    def check(item):
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise StateError("Stored JSON keys must be strings.")

                normalized = key.lower().replace("-", "_")

                if any(
                    marker in normalized
                    for marker in (
                        "api_key",
                        "apikey",
                        "password",
                        "secret",
                        "token"
                    )
                ):
                    raise StateError(
                        "Do not store credential fields in agent state."
                    )

                check(child)

        elif isinstance(item, list):
            for child in item:
                check(child)

    try:
        check(value)

        text = json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False
        )

        if len(text.encode("utf-8")) > 262144:
            raise StateError("State record exceeds 256 KiB.")

        return text

    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise StateError(
            "State data must contain valid JSON values and finite numbers."
        ) from None


class StateStore:
    def __init__(self, path, account_id):
        """account_id is a stable local account label, not an API key."""

        if not isinstance(account_id, str) or not account_id.strip():
            raise StateError("Provide a stable account label.")

        self.account_id = account_id.strip()

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        self.connection = sqlite3.connect(
            path,
            timeout=15,
            isolation_level=None
        )
        self.connection.row_factory = sqlite3.Row

        self.connection.execute("PRAGMA busy_timeout = 15000")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = FULL")

        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS theory_snapshots (
                account_id TEXT NOT NULL,
                theory_id INTEGER NOT NULL,
                theory_version INTEGER NOT NULL,
                snapshot_json TEXT NOT NULL,
                saved_at TEXT NOT NULL,
                PRIMARY KEY (
                    account_id,
                    theory_id,
                    theory_version
                )
            );

            CREATE TABLE IF NOT EXISTS proposal_intents (
                intent_id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                order_json TEXT NOT NULL,
                theory_id INTEGER NOT NULL,
                theory_version INTEGER NOT NULL,
                transaction_id INTEGER,
                status TEXT NOT NULL,
                last_confirmed_status TEXT,
                outcome_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (account_id, transaction_id)
            );

            CREATE INDEX IF NOT EXISTS idx_intents_account
            ON proposal_intents (account_id, created_at);
        """)

    @contextmanager
    def _write(self):
        self.connection.execute("BEGIN IMMEDIATE")

        try:
            yield
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def _get_row(self, intent_id):
        row = self.connection.execute(
            """
            SELECT *
            FROM proposal_intents
            WHERE account_id = ? AND intent_id = ?
            """,
            (self.account_id, intent_id)
        ).fetchone()

        if row is None:
            raise StateError("Intent not found for this account.")

        return row

    @staticmethod
    def _decode_intent(row):
        record = dict(row)
        record["order"] = json.loads(record.pop("order_json"))

        outcome = record.pop("outcome_json")
        record["outcome"] = (
            json.loads(outcome) if outcome is not None else None
        )

        return record

    def save_theory_snapshot(self, theory):
        if not isinstance(theory, dict):
            raise StateError("Theory must be a dictionary.")

        theory_id = _positive_id(theory.get("id"), "theory id")
        version = _positive_id(theory.get("version"), "theory version")
        snapshot = _json(theory)

        with self._write():
            existing = self.connection.execute(
                """
                SELECT snapshot_json
                FROM theory_snapshots
                WHERE account_id = ?
                  AND theory_id = ?
                  AND theory_version = ?
                """,
                (self.account_id, theory_id, version)
            ).fetchone()

            if existing is not None:
                if existing["snapshot_json"] != snapshot:
                    raise StateError(
                        "A different snapshot already exists "
                        "for this theory ID/version."
                    )
                return

            self.connection.execute(
                """
                INSERT INTO theory_snapshots (
                    account_id, theory_id, theory_version,
                    snapshot_json, saved_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    self.account_id,
                    theory_id,
                    version,
                    snapshot,
                    _now()
                )
            )

    def get_theory_snapshot(self, theory_id, theory_version):
        _positive_id(theory_id, "theory id")
        _positive_id(theory_version, "theory version")

        row = self.connection.execute(
            """
            SELECT snapshot_json
            FROM theory_snapshots
            WHERE account_id = ?
              AND theory_id = ?
              AND theory_version = ?
            """,
            (self.account_id, theory_id, theory_version)
        ).fetchone()

        return json.loads(row["snapshot_json"]) if row else None

    def save_proposal_intent(self, order, theory_version):
        """Save a validated BUY/SELL before any proposal request.

        order is the intended API payload:
        symbol, action, quantity, theory_id, reasoning.

        theory_version is stored locally, not added to the API payload.
        """

        if not isinstance(order, dict):
            raise StateError("Order must be a dictionary.")

        required = {
            "symbol", "action", "quantity", "theory_id", "reasoning"
        }

        if set(order) != required:
            raise StateError(
                "Order must contain exactly: symbol, action, quantity, "
                "theory_id, reasoning."
            )

        if order["action"] not in ("BUY", "SELL"):
            raise StateError("Store proposal intents only for BUY/SELL.")

        theory_id = _positive_id(order["theory_id"], "theory id")
        _positive_id(theory_version, "theory version")

        serialized_order = _json(order)
        intent_id = uuid.uuid4().hex
        now = _now()

        with self._write():
            # One unresolved local proposal per account.
            unresolved = self.connection.execute(
                """
                SELECT intent_id
                FROM proposal_intents
                WHERE account_id = ?
                  AND status NOT IN (
                      'EXECUTED', 'BLOCKED', 'REJECTED', 'CANCELLED'
                  )
                LIMIT 1
                """,
                (self.account_id,)
            ).fetchone()

            if unresolved is not None:
                raise StateError(
                    "Resolve the earlier intent before creating another."
                )

            snapshot = self.connection.execute(
                """
                SELECT 1
                FROM theory_snapshots
                WHERE account_id = ?
                  AND theory_id = ?
                  AND theory_version = ?
                """,
                (self.account_id, theory_id, theory_version)
            ).fetchone()

            if snapshot is None:
                raise StateError(
                    "Save the matching theory snapshot first."
                )

            self.connection.execute(
                """
                INSERT INTO proposal_intents (
                    intent_id, account_id, order_json,
                    theory_id, theory_version, status,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, 'PREPARED', ?, ?)
                """,
                (
                    intent_id,
                    self.account_id,
                    serialized_order,
                    theory_id,
                    theory_version,
                    now,
                    now
                )
            )

        return intent_id

    def mark_submitting(self, intent_id):
        """Commit this transition immediately before sending the POST.

        Only the process that successfully makes this transition may send.
        """

        with self._write():
            row = self._get_row(intent_id)

            if row["status"] != "PREPARED":
                raise StateError(
                    "Only a PREPARED intent may begin submission."
                )

            self.connection.execute(
                """
                UPDATE proposal_intents
                SET status = 'SUBMITTING', updated_at = ?
                WHERE account_id = ? AND intent_id = ?
                """,
                (_now(), self.account_id, intent_id)
            )

    def attach_transaction_id(self, intent_id, transaction_id):
        """Attach a confirmed Ledger ID.

        Can also be used after successful history reconciliation.
        Does not itself establish the order's final outcome.
        """

        _positive_id(transaction_id, "transaction id")

        with self._write():
            row = self._get_row(intent_id)

            existing = row["transaction_id"]

            if existing is not None:
                if existing != transaction_id:
                    raise StateError(
                        "Intent already has a different transaction ID."
                    )
                return

            if row["status"] in RESOLVED_STATUSES:
                raise StateError(
                    "Cannot attach an ID to a resolved intent."
                )

            other = self.connection.execute(
                """
                SELECT intent_id
                FROM proposal_intents
                WHERE account_id = ? AND transaction_id = ?
                """,
                (self.account_id, transaction_id)
            ).fetchone()

            if other is not None:
                raise StateError(
                    "Transaction is already linked to another intent."
                )

            self.connection.execute(
                """
                UPDATE proposal_intents
                SET transaction_id = ?, status = 'PENDING',
                    updated_at = ?
                WHERE account_id = ? AND intent_id = ?
                """,
                (
                    transaction_id,
                    _now(),
                    self.account_id,
                    intent_id
                )
            )

    def mark_uncertain(self, intent_id):
        with self._write():
            row = self._get_row(intent_id)

            if row["status"] in RESOLVED_STATUSES:
                raise StateError(
                    "Cannot mark a resolved intent uncertain."
                )

            self.connection.execute(
                """
                UPDATE proposal_intents
                SET status = 'UNCERTAIN', updated_at = ?
                WHERE account_id = ? AND intent_id = ?
                """,
                (_now(), self.account_id, intent_id)
            )

    def record_outcome(self, intent_id, outcome):
        """Store a confirmed Ledger outcome/history record.

        Requires transaction_id and status.
        The caller must verify that the evidence belongs to this order.
        """

        if not isinstance(outcome, dict):
            raise StateError("Outcome must be a dictionary.")

        transaction_id = _positive_id(
            outcome.get("transaction_id"),
            "transaction id"
        )
        status = outcome.get("status")

        if not isinstance(status, str) or status not in LEDGER_STATUSES:
            raise StateError("Unknown Ledger order status.")

        serialized_outcome = _json(outcome)

        with self._write():
            row = self._get_row(intent_id)

            if row["transaction_id"] != transaction_id:
                raise StateError(
                    "Attach the matching transaction ID first."
                )

            if row["status"] == "CANCELLED":
                raise StateError("Cannot update a cancelled local intent.")

            confirmed = row["last_confirmed_status"]

            if confirmed in FINAL_STATUSES and confirmed != status:
                raise StateError(
                    "Cannot change a confirmed final outcome."
                )

            if confirmed == "ESCALATED" and status == "PENDING":
                raise StateError(
                    "Cannot move an escalated order back to pending."
                )

            self.connection.execute(
                """
                UPDATE proposal_intents
                SET status = ?, last_confirmed_status = ?,
                    outcome_json = ?, updated_at = ?
                WHERE account_id = ? AND intent_id = ?
                """,
                (
                    status,
                    status,
                    serialized_outcome,
                    _now(),
                    self.account_id,
                    intent_id
                )
            )

    def cancel_prepared_intent(self, intent_id):
        """Cancel only an intent that has never begun submission."""

        with self._write():
            row = self._get_row(intent_id)

            if (
                row["status"] != "PREPARED"
                or row["transaction_id"] is not None
            ):
                raise StateError(
                    "Only an unsent PREPARED intent can be cancelled."
                )

            self.connection.execute(
                """
                UPDATE proposal_intents
                SET status = 'CANCELLED', updated_at = ?
                WHERE account_id = ? AND intent_id = ?
                """,
                (_now(), self.account_id, intent_id)
            )

    def get_intent(self, intent_id):
        return self._decode_intent(self._get_row(intent_id))

    def get_intent_by_transaction(self, transaction_id):
        _positive_id(transaction_id, "transaction id")

        row = self.connection.execute(
            """
            SELECT *
            FROM proposal_intents
            WHERE account_id = ? AND transaction_id = ?
            """,
            (self.account_id, transaction_id)
        ).fetchone()

        return self._decode_intent(row) if row else None

    def get_unresolved_intents(self):
        rows = self.connection.execute(
            """
            SELECT *
            FROM proposal_intents
            WHERE account_id = ?
              AND status NOT IN (
                  'EXECUTED', 'BLOCKED', 'REJECTED', 'CANCELLED'
              )
            ORDER BY created_at, intent_id
            """,
            (self.account_id,)
        ).fetchall()

        return [self._decode_intent(row) for row in rows]