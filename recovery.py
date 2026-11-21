"""Read-only Ledger reconciliation plus explicit local binding of a lost proposal.

Never resends proposals or guesses which transaction belongs to an unknown ID.
"""
from decimal import Decimal

STATUSES = {"PENDING", "ESCALATED", "EXECUTED", "BLOCKED", "REJECTED"}


def reconcile_known(client, state):
    unresolved = state.get_unresolved_intents()
    if not unresolved:
        return []
    response = client.get_transactions(limit=200)
    rows = response["transactions"]
    if not isinstance(rows, list):
        raise ValueError("Invalid transaction history")
    by_id = {}
    for row in rows:
        tid = row.get("transaction_id")
        if type(tid) is not int or tid <= 0 or tid in by_id:
            raise ValueError("Invalid/duplicate history ID")
        by_id[tid] = row
    for intent in unresolved:
        tid = intent["transaction_id"]
        if tid is None:
            continue
        record = by_id.get(tid)
        if record is not None:
            if record.get("status") not in STATUSES:
                raise ValueError("Unexpected Ledger history status")
            state.record_outcome(intent["intent_id"], record)
    # Missing history entries or unknown IDs remain unresolved.
    return state.get_unresolved_intents()


def server_unresolved(client):
    """Check both queues via history; reads available to trader and Guardian."""
    for status in ("PENDING", "ESCALATED"):
        response = client.get_transactions(limit=1, status=status)
        rows = response["transactions"]
        if not isinstance(rows, list):
            raise ValueError("Invalid filtered history")
        if any(row.get("status") == status for row in rows):
            return True
    return False


def bind_confirmed_transaction(client, state, intent_id, transaction_id):
    """User-selected recovery only, after checking the dashboard.

    No HTTP writes. Require all stored order fields to match a history entry.
    This intentionally refuses entries absent from the newest 200 records.
    """
    if type(transaction_id) is not int or transaction_id <= 0:
        raise ValueError("Invalid selected transaction ID")
    intent = state.get_intent(intent_id)
    if intent["status"] not in {"SUBMITTING", "UNCERTAIN"} or intent["transaction_id"] is not None:
        raise ValueError("Select an unresolved submitted intent with no transaction ID")
    rows = client.get_transactions(limit=200)["transactions"]
    matching = [r for r in rows if r.get("transaction_id") == transaction_id]
    if len(matching) != 1:
        raise ValueError("Selected transaction not uniquely available in history")
    row = matching[0]
    order = intent["order"]
    for key in ("symbol", "action", "theory_id", "reasoning"):
        if row.get(key) != order[key]:
            raise ValueError("Selected transaction does not match the saved intent")
    if row.get("theory_version") != intent["theory_version"]:
        raise ValueError("Selected theory version differs")
    requested = row.get("requested_quantity")
    if isinstance(requested, bool) or requested is None:
        raise ValueError("Missing original quantity in history")
    quantity = Decimal(str(requested))
    if not quantity.is_finite() or quantity != Decimal(str(order["quantity"])):
        raise ValueError("Original requested quantity differs")
    if row.get("status") not in STATUSES:
        raise ValueError("Invalid selected status")
    state.attach_transaction_id(intent_id, transaction_id)
    state.record_outcome(intent_id, row)
    return {"intent_id": intent_id, "transaction_id": transaction_id, "status": row["status"]}


def unresolved_summary(state):
    return [{k: intent[k] for k in ("intent_id", "transaction_id", "status", "theory_id", "theory_version")}
            for intent in state.get_unresolved_intents()]
