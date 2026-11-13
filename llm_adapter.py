from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any


class LLMError(Exception):
    """Base error caught by the trader and Guardian."""


class LLMValidationError(LLMError):
    """Invalid context or model response."""


class LLMProviderError(LLMError):
    """Provider failure, refusal, or incomplete response."""


TRADER_ACTIONS = {"BUY", "SELL", "HOLD"}
GUARDIAN_DECISIONS = {"APPROVE", "MODIFY", "BLOCK", "ESCALATE"}

TRADER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "symbol": {"type": "string"},
        "action": {
            "type": "string",
            "enum": ["BUY", "SELL", "HOLD"]
        },
        "quantity": {"type": "number"},
        "reasoning": {"type": "string"}
    },
    "required": ["symbol", "action", "quantity", "reasoning"]
}

GUARDIAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "decision": {
            "type": "string",
            "enum": ["APPROVE", "MODIFY", "BLOCK", "ESCALATE"]
        },
        "reason": {"type": "string"},
        "risk_score": {"type": "number"},
        "modified_quantity": {"type": ["number", "null"]},
        "rules_triggered": {
            "type": "array",
            "items": {"type": "string"}
        }
    },
    "required": [
        "decision",
        "reason",
        "risk_score",
        "modified_quantity",
        "rules_triggered"
    ]
}


@dataclass(frozen=True)
class TraderDecision:
    symbol: str
    action: str
    quantity: float
    reasoning: str


@dataclass(frozen=True)
class GuardianDecision:
    decision: str
    reason: str
    risk_score: float
    modified_quantity: float | None
    rules_triggered: list[str]


@dataclass
class ModelConfig:
    provider: str = "gemini"
    model: str = ""
    base_url: str = (
        "https://generativelanguage.googleapis.com/v1beta/openai/"
    )
    timeout_seconds: float = 30.0
    max_retries: int = 0
    api_key_env: str = "GEMINI_API_KEY"

    @classmethod
    def from_file(cls, path):
        path = Path(path)

        if not path.exists():
            raise FileNotFoundError(
                f"Configuration not found: {path}. "
                "Copy config.llm.example.json to config.llm.local.json."
            )

        if path.stat().st_size > 32768:
            raise ValueError("Model configuration exceeds 32 KiB.")

        data = json.loads(path.read_text(encoding="utf-8"))

        if not isinstance(data, dict):
            raise ValueError("Model configuration must be a JSON object.")

        allowed = {
            "provider",
            "model",
            "base_url",
            "timeout_seconds",
            "max_retries",
            "api_key_env"
        }

        if set(data) - allowed:
            raise ValueError("Unknown model configuration fields.")

        return cls(**data)

    def api_key(self):
        key = os.environ.get(self.api_key_env, "").strip()

        if not key:
            raise LLMProviderError(
                f"Missing environment variable: {self.api_key_env}."
            )

        return key


def create_adapter(config_path=None, provider=None):
    if provider not in {None, "mock", "gemini"}:
        raise ValueError("Provider must be gemini or mock.")

    if config_path is None:
        if provider == "mock":
            config = ModelConfig(provider="mock")
        else:
            path = Path(__file__).resolve().parent / "config.llm.local.json"
            config = ModelConfig.from_file(path)
    else:
        config = ModelConfig.from_file(config_path)

    if provider is not None:
        config.provider = provider

    return ModelAdapter(config)


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False

    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def _exact_fields(obj, schema):
    if not isinstance(obj, dict):
        raise LLMValidationError("Model response must be a JSON object.")

    if set(obj) != set(schema["required"]):
        raise LLMValidationError(
            "Model response has missing or extra fields."
        )


def _reason(value, maximum_bytes):
    if not isinstance(value, str) or not value.strip():
        raise LLMValidationError("Reason must be a non-empty string.")

    value = value.strip()

    try:
        length = len(value.encode("utf-8"))
    except UnicodeError:
        raise LLMValidationError("Reason contains invalid text.") from None

    if length > maximum_bytes:
        raise LLMValidationError("Reason exceeds its byte limit.")

    return value


def validate_trader_response(obj):
    _exact_fields(obj, TRADER_SCHEMA)

    symbol = obj["symbol"]
    if not isinstance(symbol, str) or not symbol.strip():
        raise LLMValidationError("Symbol must be a non-empty string.")

    symbol = symbol.strip().upper()

    if len(symbol) > 20:
        raise LLMValidationError("Symbol is too long.")

    action = obj["action"]
    if not isinstance(action, str) or action not in TRADER_ACTIONS:
        raise LLMValidationError("Invalid trader action.")

    quantity = obj["quantity"]
    if not _number(quantity) or quantity < 0:
        raise LLMValidationError("Invalid trader quantity.")

    if action == "HOLD" and quantity != 0:
        raise LLMValidationError("HOLD requires quantity zero.")

    if action in {"BUY", "SELL"} and quantity <= 0:
        raise LLMValidationError("BUY/SELL requires a positive quantity.")

    return TraderDecision(
        symbol=symbol,
        action=action,
        quantity=float(quantity),
        reasoning=_reason(obj["reasoning"], 16000)
    )


def validate_guardian_response(obj):
    _exact_fields(obj, GUARDIAN_SCHEMA)

    decision = obj["decision"]
    if (
        not isinstance(decision, str)
        or decision not in GUARDIAN_DECISIONS
    ):
        raise LLMValidationError("Invalid Guardian decision.")

    risk = obj["risk_score"]
    if not _number(risk) or not 0 <= risk <= 100:
        raise LLMValidationError("Risk score must be between 0 and 100.")

    decimal_risk = Decimal(str(risk))
    if decimal_risk != decimal_risk.quantize(Decimal("0.01")):
        raise LLMValidationError(
            "Risk score allows at most two decimal places."
        )

    modified = obj["modified_quantity"]

    if decision == "MODIFY":
        if not _number(modified) or modified <= 0:
            raise LLMValidationError(
                "MODIFY requires a positive modified quantity."
            )
        modified = float(modified)
    elif modified is not None:
        raise LLMValidationError(
            "modified_quantity must be null unless decision is MODIFY."
        )

    rules = obj["rules_triggered"]

    if not isinstance(rules, list) or len(rules) > 100:
        raise LLMValidationError(
            "rules_triggered must contain at most 100 strings."
        )

    cleaned_rules = []

    for rule in rules:
        if not isinstance(rule, str) or not rule.strip():
            raise LLMValidationError("Rule names cannot be empty.")

        rule = rule.strip()

        try:
            length = len(rule.encode("utf-8"))
        except UnicodeError:
            raise LLMValidationError(
                "Rule name contains invalid text."
            ) from None

        if length > 255:
            raise LLMValidationError(
                "Rule names cannot exceed 255 UTF-8 bytes."
            )

        cleaned_rules.append(rule)

    return GuardianDecision(
        decision=decision,
        reason=_reason(obj["reason"], 8000),
        risk_score=float(risk),
        modified_quantity=modified,
        rules_triggered=cleaned_rules
    )


# Context field names shared by trader and Guardian.
COMMON_SECTIONS = {
    "quote": {
        "price_usd", "timestamp_unix", "model_version"
    },
    "theory": {
        "id", "version", "status", "title", "hypothesis",
        "entry_rule", "exit_rule", "risk_rule", "selected_symbols",
        "strategy", "lookback_minutes", "threshold_pct",
        "position_fraction", "stop_loss_pct", "take_profit_pct"
    },
    "signals": {
        "lookback_return_pct", "mean_deviation_pct",
        "return_since_avg_cost_pct", "entry_triggered",
        "exit_triggers", "replay_matches_quote"
    },
    "policy": {
        "allowed_symbols", "restricted_actions",
        "max_transaction_amount_usd", "max_trades_per_hour",
        "max_trades_per_day", "escalation_threshold_usd"
    },
    "trade_counts": {
        "executed_last_hour", "executed_last_day"
    },
    "portfolio": {
        "cash_usd", "symbol_quantity", "symbol_avg_cost_usd"
    },
    "bounds": {
        "max_buy_budget_usd", "max_buy_quantity",
        "max_sell_quantity", "minimum_settled_amount_usd",
        "quantity_decimal_places"
    }
}

ORDER_FIELDS = {
    "transaction_id", "theory_id", "theory_version",
    "action", "requested_quantity", "requested_price_usd",
    "reasoning"
}

ASSESSMENT_FIELDS = {
    "requested_amount_at_current_quote_usd",
    "theory_snapshot_matches_order",
    "hard_rules_passed",
    "rules_triggered",
    "requires_human_approval"
}


def _require_section(context, name, fields):
    section = context.get(name)

    if not isinstance(section, dict):
        raise LLMValidationError(f"{name} must be a dictionary.")

    if fields - set(section):
        raise LLMValidationError(f"{name} is missing required fields.")


def validate_context(context, kind):
    if not isinstance(context, dict):
        raise LLMValidationError("Context must be a dictionary.")

    version = context.get("context_version")
    if type(version) is not int or version != 1:
        raise LLMValidationError("context_version must be integer 1.")

    if not _number(context.get("decision_time_unix")):
        raise LLMValidationError("Invalid decision_time_unix.")

    if not isinstance(context.get("symbol"), str):
        raise LLMValidationError("Context requires a symbol string.")

    if not context["symbol"].strip():
        raise LLMValidationError("Context symbol cannot be empty.")

    if not isinstance(context.get("news"), list):
        raise LLMValidationError("news must be a list.")

    for name, fields in COMMON_SECTIONS.items():
        # Guardian may explicitly represent unavailable evidence as None.
        # The Guardian caller must escalate instead of requesting judgment.
        if kind == "guardian" and context.get(name) is None:
            raise LLMValidationError(
                f"Guardian evidence unavailable: {name}. Escalate."
            )
        _require_section(context, name, fields)

    if kind == "trader":
        _require_section(
            context, "execution_state", {"has_unresolved_order"}
        )
    elif kind == "guardian":
        _require_section(context, "order", ORDER_FIELDS)
        _require_section(context, "assessment", ASSESSMENT_FIELDS)
    else:
        raise LLMValidationError("Unknown context kind.")


def _assert_no_secrets(value):
    """Additional safeguard; callers must still exclude all credentials."""
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise LLMValidationError(
                    "Context object keys must be strings."
                )

            normalized = key.lower().replace("-", "_")
            if any(
                marker in normalized
                for marker in ("api_key", "apikey", "password", "secret", "token")
            ):
                raise LLMValidationError(
                    "Credential-like field found in context."
                )

            _assert_no_secrets(item)

    elif isinstance(value, list):
        for item in value:
            _assert_no_secrets(item)


def _extract_json(text):
    if not isinstance(text, str) or not text.strip():
        raise LLMValidationError("Model returned empty/non-text output.")

    def reject_constant(value):
        raise ValueError("Non-finite JSON number.")

    def reject_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON field.")
            result[key] = value
        return result

    try:
        obj = json.loads(
            text,
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicates
        )
    except (ValueError, RecursionError):
        raise LLMValidationError(
            "Expected one strict JSON object without surrounding text."
        ) from None

    if not isinstance(obj, dict):
        raise LLMValidationError("Expected a JSON object.")

    return obj


class ModelAdapter:
    def __init__(self, config=None):
        if not isinstance(config, ModelConfig):
            raise ValueError("Pass an explicit ModelConfig.")

        if config.provider not in {"mock", "gemini"}:
            raise ValueError("Provider must be gemini or mock.")

        if config.provider != "mock":
            if not isinstance(config.model, str) or not config.model.strip():
                raise ValueError("Specify the Gemini model.")

            expected_url = (
                "https://generativelanguage.googleapis.com/v1beta/openai"
            )
            if (
                not isinstance(config.base_url, str)
                or config.base_url.rstrip("/") != expected_url
            ):
                raise ValueError("Use the Gemini OpenAI-compatible base URL.")

        if (
            not _number(config.timeout_seconds)
            or config.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive.")

        if type(config.max_retries) is not int or config.max_retries != 0:
            raise ValueError("Set max_retries to 0.")

        if (
            not isinstance(config.api_key_env, str)
            or not config.api_key_env.strip()
        ):
            raise ValueError("Specify the API key environment variable.")

        self.config = config
        self._prompts_dir = Path(__file__).resolve().parent / "prompts"

    @classmethod
    def from_config(cls, path):
        return cls(ModelConfig.from_file(path))

    def _system_prompt(self, kind):
        path = self._prompts_dir / f"{kind}_system.txt"

        try:
            prompt = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            raise LLMValidationError(
                f"Cannot load {kind} system prompt."
            ) from None

        if not prompt:
            raise LLMValidationError(f"{kind} system prompt is empty.")

        return prompt

    def _user_payload(self, context, kind):
        _assert_no_secrets(context)
        validate_context(context, kind)

        try:
            payload = json.dumps(
                context,
                ensure_ascii=False,
                allow_nan=False
            )
            size = len(payload.encode("utf-8"))
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise LLMValidationError(
                "Context must contain valid JSON values and finite numbers."
            ) from None

        if size > 100000:
            raise LLMValidationError("Context exceeds 100,000 UTF-8 bytes.")

        return payload

    def trader_decision(self, context):
        payload = self._user_payload(context, "trader")
        system = self._system_prompt("trader")
        raw = self._call(
            kind="trader",
            system=system,
            payload=payload,
            context=context
        )
        return validate_trader_response(_extract_json(raw))

    def guardian_decision(self, context):
        payload = self._user_payload(context, "guardian")
        system = self._system_prompt("guardian")
        raw = self._call(
            kind="guardian",
            system=system,
            payload=payload,
            context=context
        )
        return validate_guardian_response(_extract_json(raw))

    def _call(self, *, kind, system, payload, context):
        if self.config.provider == "mock":
            return self._call_mock(kind, context)

        return self._call_provider(
            kind=kind, system=system, payload=payload
        )

    def _call_mock(self, kind, context):
        override = f"_mock_{kind}"

        if override in context:
            value = context[override]
            if isinstance(value, str):
                return value
            return json.dumps(value, allow_nan=False)

        if kind == "trader":
            return json.dumps({
                "symbol": context["symbol"],
                "action": "HOLD",
                "quantity": 0,
                "reasoning": "Explicit mock mode: no real model call."
            })

        return json.dumps({
            "decision": "ESCALATE",
            "reason": "Explicit mock mode: no real model assessment.",
            "risk_score": 0,
            "modified_quantity": None,
            "rules_triggered": ["mock_mode"]
        })

    def _call_provider(self, *, kind, system, payload):
        try:
            from openai import OpenAI
        except ImportError:
            raise LLMProviderError(
                "Install dependencies: python -m pip install -r requirements.txt"
            ) from None

        schema = TRADER_SCHEMA if kind == "trader" else GUARDIAN_SCHEMA
        key = self.config.api_key()

        try:
            with OpenAI(
                api_key=key,
                base_url=self.config.base_url,
                timeout=self.config.timeout_seconds,
                max_retries=0
            ) as client:
                response = client.chat.completions.create(
                    model=self.config.model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": payload}
                    ],
                    response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "name": f"{kind}_decision",
                            "strict": True,
                            "schema": schema
                        }
                    }
                )
        except Exception as exc:
            raise LLMProviderError(
                f"Gemini request failed ({type(exc).__name__}). "
                "Check credentials, model, quota, and connectivity."
            ) from None

        if not response.choices:
            raise LLMProviderError("Gemini returned no choices.")

        choice = response.choices[0]

        if getattr(choice, "finish_reason", None) != "stop":
            raise LLMProviderError("Gemini response did not finish normally.")

        message = getattr(choice, "message", None)

        if message is None:
            raise LLMProviderError("Gemini returned no message.")

        if getattr(message, "refusal", None):
            raise LLMProviderError("Gemini refused the request.")

        content = getattr(message, "content", None)

        if not isinstance(content, str) or not content.strip():
            raise LLMValidationError("Gemini returned empty/non-text output.")

        return content


ModelAdapterFactory = create_adapter