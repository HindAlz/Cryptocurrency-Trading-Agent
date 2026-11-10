import json
import random
import time
from urllib.parse import urlsplit
import requests

ENDPOINTS = {
    "market_prices.php", "market_news.php", "portfolio.php", "policies.php",
    "trade_propose.php", "trade_pending.php", "trade_review.php",
    "transactions.php", "guardian_logs.php", "theory.php", "market_model.php",
}

class ApiError(RuntimeError):
    def __init__(self, status, body):
        super().__init__(f"Ledger HTTP {status}: {body}")
        self.status, self.body = status, body

class Cooldown(RuntimeError):
    def __init__(self, seconds):
        super().__init__("Wait before the next Ledger request")
        self.seconds = seconds

class UncertainWrite(RuntimeError):
    pass

class LedgerClient:
    def __init__(self, base, key):
        url = urlsplit(base)
        if (url.scheme != "https" or not url.hostname or url.username
                or url.password or url.query or url.fragment):
            raise ValueError("Use the course's canonical HTTPS API base URL")
        if not key.strip() or key.startswith("PASTE_"):
            raise ValueError("Configure this role's complete API key")
        self.base, self.key = base.rstrip("/"), key.strip()
        self.session = requests.Session()
        self.next_allowed = 0.0
        self.next_request_at = 0.0

    def request(self, method, endpoint, *, params=None, body=None):
        if endpoint not in ENDPOINTS or method not in {"GET", "POST"}:
            raise ValueError("Use a documented endpoint and method")
        delay = self.next_allowed - time.monotonic()
        if delay > 0:
            raise Cooldown(delay)
        headers = {"X-Api-Key": self.key}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False,
                              allow_nan=False).encode("utf-8")
            if len(data) > 32768:
                raise ValueError("JSON request exceeds 32 KiB")
            headers["Content-Type"] = "application/json"
        attempts = 3 if method == "GET" else 1
        for attempt in range(attempts):
            # Pace normal traffic as well as retrying failures.
            spacing = self.next_request_at - time.monotonic()
            if spacing > 0:
                time.sleep(spacing)
            self.next_request_at = time.monotonic() + 1.1
            try:
                response = self.session.request(
                    method, f"{self.base}/{endpoint}", headers=headers,
                    params=params, data=data, timeout=(5, 15),
                    allow_redirects=False,
                )
            except requests.RequestException as exc:
                if method == "POST":
                    raise UncertainWrite("Write response lost; reconcile first") from exc
                if attempt + 1 == attempts:
                    raise ApiError(0, "Read failed; try a later cycle") from exc
                time.sleep(2 ** attempt + random.uniform(0, 1))
                continue
            if response.status_code in {429, 503}:
                retry = response.headers.get("Retry-After", "15")
                seconds = max(1, int(retry)) if retry.isdigit() else 15
                self.next_allowed = time.monotonic() + seconds + random.uniform(0, 2)
                if method == "POST" and response.status_code == 503:
                    raise UncertainWrite("Service unavailable; wait and reconcile")
                raise Cooldown(self.next_allowed - time.monotonic())
            if 300 <= response.status_code < 400:
                raise ApiError(response.status_code, "Redirect refused; check API base URL")
            if method == "POST" and response.status_code >= 500:
                raise UncertainWrite("Server error; reconcile before another write")
            try:
                result = response.json()
            except ValueError as exc:
                if method == "POST" and response.ok:
                    raise UncertainWrite("Write returned unreadable result") from exc
                raise ApiError(response.status_code, "Expected JSON response") from exc
            if not response.ok:
                raise ApiError(response.status_code, result)
            if not isinstance(result, dict):
                if method == "POST":
                    raise UncertainWrite("Write returned unexpected result")
                raise ApiError(response.status_code, "Expected a JSON object")
            return result

    def get(self, endpoint, **params):
        return self.request("GET", endpoint, params=params)

    def post(self, endpoint, body):
        return self.request("POST", endpoint, body=body)

    def get_portfolio(self):
        return self.get("portfolio.php")

    def get_policy(self):
        return self.get("policies.php")

    def get_prices(self, symbols="BTC"):
        return self.get("market_prices.php", symbols=symbols)

    def get_news(self):
        return self.get("market_news.php")

    def get_theory(self):
        return self.get("theory.php")

    def get_market_model(self):
        return self.get("market_model.php")

    def get_pending_orders(self, limit=2):
        return self.get("trade_pending.php", limit=limit)

    def get_transactions(self, limit=50, status=None):
        params = {"limit": limit}
        if status is not None:
            params["status"] = status
        return self.get("transactions.php", **params)

    def get_guardian_logs(self, limit=50):
        return self.get("guardian_logs.php", limit=limit)

    def propose_trade(self, order):
        return self.post("trade_propose.php", order)

    def submit_review(self, review):
        return self.post("trade_review.php", review)