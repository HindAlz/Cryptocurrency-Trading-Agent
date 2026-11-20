
import json
from decimal import Decimal, ROUND_FLOOR

from client import LedgerClient, Cooldown, ApiError
from guardian_rules import (
    check_trade,
    check_theory_compliance,
    number
)
from llm_adapter import (
    create_adapter,
    validate_guardian_response
)
from client import UncertainWrite


def prepare_review(transaction_id, model_result, requested_quantity):
    """
    Validate a Guardian decision and prepare a Ledger review.
    Does NOT submit anything.
    """
    if type(transaction_id) is not int or transaction_id <= 0:
        raise ValueError("Invalid transaction ID")

    if hasattr(model_result, "__dataclass_fields__"):
        from dataclasses import asdict
        model_result = asdict(model_result)

    decision = validate_guardian_response(model_result)

    if decision.decision == "MODIFY":
        original = number(requested_quantity, positive=True)
        modified = number(decision.modified_quantity, positive=True)

        if modified >= original:
            raise ValueError(
                "MODIFY may only reduce the original quantity"
            )

        if modified.as_tuple().exponent < -10:
            raise ValueError("Too many decimal places")

    return {
        "transaction_id": transaction_id,
        "decision": decision.decision,
        "reason": decision.reason,
        "risk_score": decision.risk_score,
        "modified_quantity": (
            format(modified.normalize(), "f")
            if decision.decision == "MODIFY" else None
        ),
        "rules_triggered": decision.rules_triggered
    }


def inspect_pending(client, llm=None, state=None, signal_provider=None):
    """
    Inspect real Ledger pending orders with hard security rules.

    READ-ONLY: Does not approve or submit reviews.
    """

    print("\n=== AI GUARDIAN: SECURITY REVIEW ===")

    response = client.get_pending_orders(limit=2)
    pending = response["pending"]

    if not isinstance(pending, list):
        raise ValueError("Invalid pending-orders response")

    print("Pending orders:", len(pending))

    results = []

    for item in pending:
        transaction_id = item.get("transaction_id")

        print(f"\nReviewing transaction: {transaction_id}")

        try:
            # Step 1: Normalize the original proposal
            order = normalize_order(item)
            symbol = order["symbol"]

            # Step 2: Independently fetch latest evidence
            policy = client.get_policy()
            portfolio_response = client.get_portfolio()
            quote = get_current_quote(client, symbol)

            portfolio = normalize_portfolio(
                portfolio_response, symbol
            )

            # Step 3: Retrieve the matching historical theory
            theory = None

            if state is not None:
                theory = state.get_theory_snapshot(
                    order["theory_id"],
                    order["theory_version"]
                )

            
            # Step 4: Check hard security rules
            hard_result = check_trade(
                order=order,
                policy=policy,
                current_price=quote["price"],
                portfolio=portfolio,
                theory_snapshot=theory
            )

            # A hard BLOCK cannot be overridden.
            if hard_result["decision"] == "BLOCK":
                result = hard_result

            # Missing evidence or escalation threshold:
            # do not continue toward approval.
            elif hard_result["decision"] == "ESCALATE":
                result = hard_result

            # Step 5: Verify trading theory
            elif signal_provider is None:
                # No independent market signals are available.
                result = {
                    "decision": "ESCALATE",
                    "passed": False,
                    "rules_triggered": ["MISSING_VERIFIED_SIGNALS"]
                }

            else:
                # The provider must independently calculate and validate
                # the signals, not trust the trader's reasoning.
                signals = signal_provider(order, theory, quote)

                theory_result = check_theory_compliance(
                    order=order,
                    theory=theory,
                    signals=signals,
                    portfolio=portfolio,
                    current_price=quote["price"]
                )

                result = {
                    "decision": theory_result["decision"],
                    "passed": theory_result["decision"] == "PASS",
                    "rules_triggered": (
                        hard_result["rules_triggered"]
                        + theory_result["rules_triggered"]
                    )
                }


            print("Currency:", symbol)
            print("Action:", order["action"])
            print("Current price:", quote["price"])
            print("Hard-rule decision:", result["decision"])
            print("Triggered rules:", result["rules_triggered"])

        except Exception as exc:
            # API cooldowns must be handled by the caller.
            if isinstance(exc, Cooldown):
                raise

            # Never approve when evidence is incomplete.
            result = {
                "decision": "ESCALATE",
                "passed": False,
                "rules_triggered": [
                    "EVIDENCE_UNAVAILABLE"
                ]
            }

            print("Could not safely evaluate this order.")
            print("Error type:", type(exc).__name__)

        results.append({
            "transaction_id": transaction_id,
            **result
        })

    print("\n=== READ-ONLY REVIEW COMPLETE ===")
    return results




def evaluate_order(order, policy, current_price,
                   portfolio, theory_snapshot):
    """
    Run Member 3's security rules against one order.

    This function only evaluates the order.
    It does not submit a review to Ledger.
    """

    result = check_trade(
        order=order,
        policy=policy,
        current_price=current_price,
        portfolio=portfolio,
        theory_snapshot=theory_snapshot
    )

    print("\n=== GUARDIAN SECURITY ASSESSMENT ===")
    print("Currency:", order.get("symbol"))
    print("Action:", order.get("action"))
    print("Quantity:", order.get("quantity"))
    print("Decision:", result["decision"])
    print("Triggered rules:", result["rules_triggered"])

    return result


def normalize_order(pending):
    """Convert a Ledger pending order into our rule format."""
    return {
        "transaction_id": pending.get("transaction_id"),
        "requested_price": pending.get("requested_price"),
        "symbol": pending["symbol"],
        "action": pending["action"],
        "quantity": pending["requested_quantity"],
        "reasoning": pending["reasoning"],
        "theory_id": pending["theory_id"],
        "theory_version": pending["theory_version"]
    }


def get_current_quote(client, symbol):
    """Independently retrieve the price of any currency."""
    response = client.get_prices(symbols=symbol)

    quotes = [
        q for q in response["prices"]
        if q["symbol"] == symbol
    ]

    if len(quotes) != 1:
        raise ValueError("Missing or duplicate market quote")

    return quotes[0]


def normalize_portfolio(portfolio_data, symbol):
    """Extract available cash and holdings of the order's currency."""
    holdings = [
        h for h in portfolio_data["holdings"]
        if h["symbol"] == symbol
    ]

    if len(holdings) > 1:
        raise ValueError("Duplicate holdings entries")

    return {
        "cash_usd": portfolio_data["cash_balance"],
        "symbol_quantity": (
            holdings[0]["quantity"] if holdings else 0
        ),
        "symbol_avg_cost_usd": (
            holdings[0]["avg_cost"] if holdings else 0
        )
    }


def review_mock_order(order, policy, price, portfolio,
                      theory, signals, llm):
    """
    Test the Guardian's hard rules and LLM connection locally.

    No Ledger write requests are made.
    """

    # Hard security checks always run first.
    hard = check_trade(
        order, policy, price, portfolio, theory
    )

    if hard["decision"] != "PASS":
        print("Hard-rule decision:", hard["decision"])
        print("Triggered rules:", hard["rules_triggered"])
        return hard

    # Build the context required by Member 4.
    quantity = Decimal(str(order["quantity"]))
    current_price = Decimal(str(price))
    cash = Decimal(str(portfolio["cash_usd"]))
    holdings = Decimal(str(portfolio["symbol_quantity"]))
    fraction = Decimal(str(theory["position_fraction"]))
    ceiling = Decimal(str(policy["max_transaction_amount"]))

    buy_budget = min(cash * fraction, ceiling)
    buy_budget = buy_budget.quantize(
        Decimal("0.01"), rounding=ROUND_FLOOR
    )
    buy_qty = (buy_budget / current_price).quantize(
        Decimal("1e-10"), rounding=ROUND_FLOOR
    )
    sell_qty = min(
        holdings,
        ceiling / current_price
    ).quantize(Decimal("1e-10"), rounding=ROUND_FLOOR)

    context = {
        "context_version": 1,
        "decision_time_unix": 1795165200,
        "symbol": order["symbol"],
        "news": [],
        "quote": {
            "price_usd": float(current_price),
            "timestamp_unix": 1795165200,
            "model_version": "mock-test"
        },
        "theory": theory,
        "signals": signals,
        "policy": {
            "allowed_symbols": policy["allowed_symbols"],
            "restricted_actions": policy["restricted_actions"],
            "max_transaction_amount_usd":
                policy["max_transaction_amount"],
            "max_trades_per_hour": policy["max_trades_per_hour"],
            "max_trades_per_day": policy["max_trades_per_day"],
            "escalation_threshold_usd":
                policy["escalation_threshold"]
        },
        "trade_counts": {
            "executed_last_hour": policy["trades_last_hour"],
            "executed_last_day": policy["trades_last_day"]
        },
        "portfolio": portfolio,
        "bounds": {
            "max_buy_budget_usd": float(buy_budget),
            "max_buy_quantity": float(buy_qty),
            "max_sell_quantity": float(sell_qty),
            "minimum_settled_amount_usd": 0.01,
            "quantity_decimal_places": 10
        },
        "order": {
            "transaction_id": 42,
            "theory_id": order["theory_id"],
            "theory_version": order["theory_version"],
            "action": order["action"],
            "requested_quantity": float(quantity),
            "requested_price_usd": float(current_price),
            "reasoning": order["reasoning"]
        },
        "assessment": {
            "requested_amount_at_current_quote_usd":
                float(quantity * current_price),
            "theory_snapshot_matches_order": True,
            "hard_rules_passed": True,
            "rules_triggered": [],
            "requires_human_approval": False
        }
    }

    # Member 4's adapter performs context and response validation.
    decision = llm.guardian_decision(context)

    print("LLM recommendation:", decision.decision)
    print("Reason:", decision.reason)

    return {
        "decision": decision.decision,
        "reason": decision.reason,
        "risk_score": decision.risk_score,
        "rules_triggered": decision.rules_triggered
    }


def final_recheck(
    client,
    order,
    review,
    theory_snapshot,
    signal_provider=None
):
    """
    Refresh Ledger evidence before APPROVE or MODIFY.

    READ-ONLY: This function never submits a review.
    Returns PASS, BLOCK, or ESCALATE.
    """

    decision = review.get("decision")

    if decision not in {"APPROVE", "MODIFY"}:
        raise ValueError(
            "Final recheck is only for APPROVE or MODIFY"
        )

    # Preserve the original proposal.
    final_order = order.copy()

    if decision == "MODIFY":
        original = number(order["quantity"], positive=True)
        reduced = number(
            review["modified_quantity"], positive=True
        )

        if reduced >= original:
            return {
                "decision": "BLOCK",
                "passed": False,
                "rules_triggered": ["INVALID_MODIFICATION"]
            }

        if reduced.as_tuple().exponent < -10:
            return {
                "decision": "BLOCK",
                "passed": False,
                "rules_triggered": ["INVALID_MODIFICATION"]
            }

        final_order["quantity"] = format(reduced, "f")

    # Retrieve NEW data rather than using cached evidence.
    policy = client.get_policy()
    quote = get_current_quote(client, order["symbol"])
    portfolio_response = client.get_portfolio()

    portfolio = normalize_portfolio(
        portfolio_response,
        order["symbol"]
    )

    # Repeat the hard security checks with the new price.
    hard_result = check_trade(
        order=final_order,
        policy=policy,
        current_price=quote["price"],
        portfolio=portfolio,
        theory_snapshot=theory_snapshot
    )

    if hard_result["decision"] != "PASS":
        return hard_result

    # Without independently verified fresh signals,
    # the Guardian must not authorize the order.
    if signal_provider is None:
        return {
            "decision": "ESCALATE",
            "passed": False,
            "rules_triggered": ["MISSING_VERIFIED_SIGNALS"]
        }

    try:
        signals = signal_provider(
            final_order, theory_snapshot, quote
        )

        theory_result = check_theory_compliance(
            order=final_order,
            theory=theory_snapshot,
            signals=signals,
            portfolio=portfolio,
            current_price=quote["price"]
        )

    except (ValueError, KeyError, TypeError):
        return {
            "decision": "ESCALATE",
            "passed": False,
            "rules_triggered": ["INVALID_MARKET_EVIDENCE"]
        }

    return {
        "decision": theory_result["decision"],
        "passed": theory_result["decision"] == "PASS",
        "rules_triggered": (
            hard_result["rules_triggered"]
            + theory_result["rules_triggered"]
        )
    }


def submit_review_safely(client, review, recheck_result=None):
    """Compatibility helper for BLOCK/ESCALATE only.

    APPROVE/MODIFY must use guardian_live.recheck_and_submit, which
    refreshes evidence internally instead of trusting a PASS dictionary.
    """
    from guardian_live import submit_non_authorizing
    return submit_non_authorizing(client, review)


def review_pending(client, llm=None, state=None, signal_provider=None,
                   *, submit=False, **options):
    """Read-only by default. Explicit submit=True enables live reviews."""
    if not submit:
        return inspect_pending(client, llm, state, signal_provider)
    from guardian_live import review_pending_live
    return review_pending_live(client, llm, state, signal_provider, **options)


def guardian_decision_dry_run(
    order,
    policy,
    current_price,
    portfolio,
    theory,
    signals,
    llm
):
    """
    Combine security checks, theory checks and LLM judgment.

    LOCAL TESTING ONLY.
    Does not contact Ledger or submit reviews.
    Cannot authorize execution.
    """

    # 1. Hard security rules
    hard = check_trade(
        order,
        policy,
        current_price,
        portfolio,
        theory
    )

    if hard["decision"] == "BLOCK":
        return {
            "decision": "BLOCK",
            "rules_triggered": hard["rules_triggered"],
            "stage": "HARD_RULES"
        }

    if hard["decision"] != "PASS":
        return {
            "decision": "ESCALATE",
            "rules_triggered": hard["rules_triggered"],
            "stage": "HARD_RULES"
        }

    # 2. Theory compliance
    theory_result = check_theory_compliance(
        order,
        theory,
        signals,
        portfolio,
        current_price
    )

    if theory_result["decision"] != "PASS":
        return {
            "decision": theory_result["decision"],
            "rules_triggered": theory_result["rules_triggered"],
            "stage": "THEORY_CHECK"
        }

    # 3. Test the LLM interface
    try:
        result = review_mock_order(
            order,
            policy,
            current_price,
            portfolio,
            theory,
            signals,
            llm
        )

    except Exception:
        return {
            "decision": "ESCALATE",
            "rules_triggered": ["LLM_UNAVAILABLE"],
            "stage": "LLM"
        }

    # This is a dry run with synthetic inputs.
    # Never allow APPROVE or MODIFY to leave this test
    # as an authorization.
    if result["decision"] in {"APPROVE", "MODIFY"}:
        return {
            "decision": "ESCALATE",
            "rules_triggered": [
                "DRY_RUN_CANNOT_AUTHORIZE"
            ],
            "stage": "LLM"
        }

    return {
        "decision": result["decision"],
        "rules_triggered": result["rules_triggered"],
        "stage": "LLM"
    }


if __name__ == "__main__":
    # Load your private Ledger configuration
    with open("config.local.json", encoding="utf-8") as file:
        config = json.load(file)

    guardian_client = LedgerClient(
        config["LEDGER_BASE_URL"],
        config["GUARDIAN_API_KEY"]
    )

    try:
        inspect_pending(guardian_client)

    except Cooldown as error:
        print(f"Ledger cooldown: retry after {error.seconds:.1f}s")

    except ApiError as error:
        print(f"Ledger request failed: HTTP {error.status}")

