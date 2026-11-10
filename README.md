Cryptocurrency Trading Agent
A team project using Ledger's simulated cryptocurrency market. The intended system consists of a Trading Agent that proposes trades and an independent AI Guardian that reviews them. Both agents must use an existing LLM, such as the team's selected Muse provider.


Installation
Clone the repository and enter its directory:
```bash
git clone https://github.com/HindAlz/Cryptocurrency-Trading-Agent.git
cd Cryptocurrency-Trading-Agent
python -m venv .venv
```
Activate the environment on Windows Command Prompt:
```bat
.venv\Scripts\activate.bat
```
Or Windows PowerShell:
```powershell
.venv\Scripts\Activate.ps1
```
Or macOS/Linux:
```bash
source .venv/bin/activate
```
Install dependencies:
```bash
`python -m pip install -r requirements.txt`
```


```
Create `config.local.json`, your api keys are from the website:
```json
{
  "LEDGER_BASE_URL": "https://ledger.chanyeun.com/api/v1",
  "TRADER_API_KEY": "REPLACE_LOCALLY",
  "GUARDIAN_API_KEY": "REPLACE_LOCALLY"
}
```

Do not commit keys or share private configurations. Never include credentials in model prompts, screenshots, reports, or logs. Revoke and replace any exposed key.

Ledger account setup
Use the website dashboard to:
Assign one distinct currency to each team member.
Configure Security Policy to permit that member's assigned currency and appropriate limits.
Submit and activate a trading theory selecting that currency. Saving a draft does not activate it.
Create the two scoped API keys.
Policy, theory, keys, and human escalation decisions are managed through the dashboard, not changed through the API.


Client.py usage
```python
import json
from client import LedgerClient

with open("config.local.json", encoding="utf-8") as file:
    config = json.load(file)

trader = LedgerClient(
    config["LEDGER_BASE_URL"], config["TRADER_API_KEY"]
)
guardian = LedgerClient(
    config["LEDGER_BASE_URL"], config["GUARDIAN_API_KEY"]
)

portfolio = trader.get_portfolio()
print(json.dumps(portfolio, indent=2))
```
The named wrappers are added inside `LedgerClient`. Each returns the complete parsed JSON object.
Method	Endpoint	Role
`get_prices(symbols="BTC")`	`market_prices.php`	Either
`get_news()`	`market_news.php`	Either
`get_portfolio()`	`portfolio.php`	Either
`get_policy()`	`policies.php`	Either
`get_theory()`	`theory.php`	Either
`get_market_model()`	`market_model.php`	Either
`get_pending_orders(limit=2)`	`trade_pending.php`	Guardian
`get_transactions(limit=50, status=None)`	`transactions.php`	Either
`get_guardian_logs(limit=50)`	`guardian_logs.php`	Either
`propose_trade(order)`	`trade_propose.php`	Trader
`submit_review(review)`	`trade_review.php`	Guardian
For multiple currencies, use a comma-separated string, such as `get_prices("BTC,ETH")`. Each member's trading decisions must still follow their assigned currency and account permissions.
The lower-level methods remain available:
```python
prices = trader.get("market_prices.php", symbols="BTC")
```
