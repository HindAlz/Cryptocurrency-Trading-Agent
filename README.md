# Cryptocurrency Trading Agent

Ledger provides the dashboard, theory submission, Security Policy, Transactions, Guardian Log, Escalations, and Attack Simulator. Python runs locally. Ledger trades use simulated funds; model API calls can consume quota or incur real charges.

The Trader reads your active theory and account evidence, calculates signals and order bounds, and asks the model for a decision. A validated proposal is saved with its theory snapshot before submission. The Guardian gathers independent evidence, applies hard rules, requests model judgment where appropriate, and rechecks before submitting a review. Ledger applies its own server rules. A model recommendation never overrides a failed hard rule.

## Team setup: run the agents on your own Ledger account

These instructions use **Windows Command Prompt** and the shared repository:
https://github.com/HindAlz/Cryptocurrency-Trading-Agent

Each member runs the same code using their **own Ledger account, assigned currency, submitted theory, keys, and local state**. You do not need to edit Python files to change your currency. The trader reads the single currency in your active theory.

> Copy only commands inside code blocks, one line at a time. Do not copy terminal prompts such as `(.venv) C:\...>`.

## 1. Prepare your Ledger account

1. Open https://ledger.chanyeun.com and register/sign in using your `@ku.ac.ae` account.
2. Confirm your assigned currency. The five members must cover five distinct currencies.
3. Open **Security Policy**:
   - Enable your assigned currency only.
   - Permit BUY and SELL; keep WITHDRAW restricted.
   - Set the transaction ceiling, hourly/day limits, and escalation threshold agreed for your experiment.
   - Put the escalation threshold below the hard transaction ceiling if demonstrating size-based escalation.
4. Open **Theory Submission**:
   - Select your assigned currency only.
   - Complete the hypothesis, entry, exit, and risk rules.
   - Select **momentum** or **mean_reversion** for your assigned currency.
   - The numeric settings and written rules must agree. Arbitrary custom rules need implementation and tests before use.
   - Enter lookback, threshold, cash fraction, stop loss, and take profit settings that match your written rules.
   - Click **Submit and activate**. Saving a draft is insufficient.
5. Record your theory ID/version and policy settings for the report. 
6. Open **API Keys** and create two keys:
   - Trader key with scope **trader**.
   - Guardian key with scope **guardian**.
7. Save the complete keys privately when shown. Do not use a full-scope key.

## 2. Get the shared code

If you already have the repository, open Command Prompt inside its folder. Preserve/commit any unfinished local work before switching branches. Then:

```bat
git fetch origin
git switch main
git pull --ff-only origin main
```

Record the code version used:

```bat
git rev-parse HEAD
```

## 3. Activate your Python environment and install dependencies

From the repository folder, activate your existing virtual environment:

```bat
.venv\Scripts\activate
python -m pip install --upgrade -r requirements.txt
```

Wait until installation finishes and the command prompt returns. `requirements.txt` contains `requests` and `openai>=2.0.0`

## 4. Add your private Ledger configuration

For first-time setup only:

```bat
copy config.ledger.example.json config.local.json
notepad config.local.json
```

If `config.local.json` already exists, **edit it rather than copying over your keys**.

Enter your own values:

```json
{
  "LEDGER_BASE_URL": "https://ledger.chanyeun.com/api/v1",
  "TRADER_API_KEY": "YOUR_COMPLETE_TRADER_KEY",
  "GUARDIAN_API_KEY": "YOUR_COMPLETE_GUARDIAN_KEY"
}
```

Keep quotation marks, commas, and braces as shown. Do not add comments inside JSON.

Check that Git ignores this file:

```bat
git check-ignore config.local.json
```

It should print `config.local.json`. Do not commit this file or send it to teammates. If a key leaks, revoke and replace it.

Combined mode loads both role keys. Separate role processes may use separate config files containing only their own role key; the clients still use separate scopes in either arrangement.

## 5. Configure your model provider

Both roles use the provider selected in `config.llm.local.json`. Choose **one** option below. An existing working configuration stays private when you pull new code; do not overwrite it unintentionally. Record your provider and exact model ID for evaluation.

### Option A: OpenAI GPT-6.2 Sol

Create your own OpenAI project API key at [OpenAI API keys](https://platform.openai.com/api-keys). Check your API account’s available credits/quota and access to the model.

For first-time OpenAI setup:

```bat
copy config.openai.example.json config.llm.local.json
```

The configuration should contain:

```json
{
  "provider": "openai",
  "model": "gpt-6.2-sol",
  "base_url": "https://api.openai.com/v1/",
  "api_key_env": "OPENAI_API_KEY",
  "timeout_seconds": 30,
  "max_retries": 0,
  "reasoning_effort": "low",
  "max_output_tokens": 4096
}
```

Set the actual key in the **same Command Prompt window**:

```bat
set "OPENAI_API_KEY=YOUR_ACTUAL_OPENAI_KEY"
```

Check availability without displaying it:

```bat
python -c "import os; print('OpenAI key configured:', bool(os.environ.get('OPENAI_API_KEY', '').strip()))"
```

This provider uses the Responses API, a separate developer instruction message and JSON data message, strict JSON Schema output, and no model tools. Calls use `store=false` and do not share a conversation between Trader and Guardian. A failed request does not automatically switch providers.

### Option B: Gemini

Create your own Gemini API key at [Google AI Studio](https://aistudio.google.com/apikey).

For first-time Gemini setup:

```bat
copy config.gemini.example.json config.llm.local.json
```

The template uses the model already verified during development:

```json
{
  "provider": "gemini",
  "model": "gemini-3.5-flash-lite",
  "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
  "api_key_env": "GEMINI_API_KEY",
  "timeout_seconds": 30,
  "max_retries": 0
}
```

Set and check your key in the current Command Prompt:

```bat
set "GEMINI_API_KEY=YOUR_ACTUAL_GEMINI_KEY"
python -c "import os; print('Gemini key configured:', bool(os.environ.get('GEMINI_API_KEY', '').strip()))"
```

Use the exact model ID verified for your API account. Report an unavailable-model error rather than guessing a replacement during final evaluation. The generic `config.llm.example.json` contains a placeholder model; using the provider-specific template avoids that placeholder.

### Keep the settings private and persistent

Only the **environment variable name** belongs in `api_key_env`. The actual model key belongs in your environment. It is different from both Ledger role keys.

For future terminals, add a **user environment variable** through Windows’ **Edit environment variables for your account** dialog. Use `OPENAI_API_KEY` or `GEMINI_API_KEY` as appropriate and your key as its value. Restart Command Prompt or VS Code afterwards. A `set` command applies immediately to the current terminal only.

Check that the model config is ignored:

```bat
git check-ignore config.llm.local.json
```

Do not put real keys in code, prompts, screenshots, GitHub, or shared evidence. Keep the model timeout at most 30 seconds for the bounded runner and `max_retries` at 0. Trader model failures result in HOLD; Guardian model failures result in ESCALATE when Ledger can accept the review. Cooldowns and uncertain writes have their own recovery behavior.

## 6. Choose your stable account label and entry option

Set a label for your account. This is a local label, not your Ledger login or key. Example for Member 2 assigned XRP:

```bat
set "LEDGER_ACCOUNT=member2-xrp"
```

This is an illustrative label, not a currency assignment. Replace `member2-xrp` with your own agreed label. Use only letters, digits, underscores, and hyphens. Keep the same label and state file for all future runs on this account.

If your submitted theory says **do not buy more while already holding the currency**, set:

```bat
set "ENTRY_OPTION=--flat-entry"
```

Otherwise, leave the optional restriction disabled:

```bat
set "ENTRY_OPTION="
```

Choose **one** of those ENTRY_OPTION commands. Do not run both unintentionally. The examples below expand `%ENTRY_OPTION%` automatically.

These two variables are terminal-session settings. Re-enter them when opening a new Command Prompt. Do not change the account label to bypass unresolved work.

The commands use the existing default state file `runtime\ledger_state.sqlite3`. Both trader and Guardian must use the same file and account label. Keep your state file: it contains theory snapshots and unresolved-order records. Never delete it to clear a trading block. Do not run another computer against the same Ledger account.

## 7. Verify connections and tests

Check Guardian access to the real Ledger policy:

```bat
python test_connection.py
```

This is read-only. It should print your policy JSON, not credentials.

Verify real calls to your selected provider for both roles:

```bat
python verify_llm.py
```

This makes two real model calls using synthetic momentum examples and submits no Ledger orders. A sample BTC decision does not change your assigned currency or active theory. It verifies connectivity and output validation, not your personal theory, live trading performance, or injection resistance.

Confirm the header shows your intended provider/model and the final line reports validated responses.

Run the offline tests:

```bat
python -m pip install pytest
python -m pytest tests -q
```

The explicit `tests` path excludes old installer folders that might contain duplicate test filenames. Installation packs such as `sol_api_pack/` are not needed after their changes have been applied; keep `runtime/` because it holds your state.

Save the test summary and any errors. Test counts may change as the team adds cases; investigate failures rather than requiring a particular count.

## 8. Preview your own account without trading

First inspect pending orders:

```bat
python run_guardian_once.py --account "%LEDGER_ACCOUNT%" %ENTRY_OPTION%
```

Then preview the trader:

```bat
python run_trader_once.py --account "%LEDGER_ACCOUNT%" %ENTRY_OPTION%
```

Neither command submits trades or reviews without `--submit`. The trader preview may call your selected model. It returns:

| Output | Meaning |
| --- | --- |
| `PREVIEW` with BUY or SELL | A candidate passed the trader's checks; nothing was submitted |
| `HOLD: No eligible theory trigger` | No qualifying entry/exit under current conditions, or optional flat-entry rule prevents another BUY |
| Other `HOLD` reason | Inspect the reason; it may indicate missing theory, permissions, data, or a model/validation problem |
| `COOLDOWN` | Wait at least the stated cooldown; do not repeatedly retry |
| Unresolved-order message | Investigate/reconcile earlier work before another proposal |

PREVIEW is not guaranteed execution. It does not reserve an order or funds. The later live run builds a fresh context and may return HOLD because evidence, checks, or the model decision changed. Quotes and policy can also change before Guardian review. Do not lower thresholds or weaken checks just to get activity.

## 9. Run one controlled combined cycle

Before first live use:

- Note cash, holdings, and total portfolio value on Ledger.
- Confirm you have no unexplained pending/escalated orders.
- Confirm the preview candidate is affordable and fits your actual theory.
- Check the active theory ID/version and selected currency.

Run:

```bat
python main.py --account "%LEDGER_ACCOUNT%" --mode both --once %ENTRY_OPTION% --submit
```

**This enables actual simulated-account writes.** It processes outstanding orders, checks for a new candidate, proposes at most one trade, and reviews the new order.

Interpret the result:

| Result | Action |
| --- | --- |
| `HOLD` | No new proposal; valid if no trigger. If the concise summary hides the reason, run the standalone trader preview to inspect the current reason; it does not reconstruct the earlier cycle |
| `PROPOSED` / `PENDING` | A proposal exists; inspect the following Guardian outcome or dashboard |
| Guardian `EXECUTED` with `fill` | A trade completed; verify Transactions, Guardian Log, and actual portfolio change |
| `BLOCKED` / `REJECTED` | No fill for that order; record the reason. These do not count toward completed trades |
| `ESCALATED` | Use Ledger's Escalations page to review the request; do not blindly approve |
| `UNCERTAIN` / `UNRESOLVED` | Stop new submissions and follow recovery below |

Compare dashboard records and balances with the output. Save transaction ID, executed quantity, price, total, theory version, and Guardian decision.

Do not separately submit with the trader-only script for this combined test. Do not run multiple runners/standalone scripts concurrently on one account.

## 10. Run an autonomous evaluation session

Start this only after your single-cycle test is understood and your session recording/portfolio sampling is ready.


```bat
python main.py --account "%LEDGER_ACCOUNT%" --mode both %ENTRY_OPTION% --submit --interval 60 --duration 540
```

Without `--once`, the system repeatedly makes its own trading/review decisions. It waits at least 60 seconds after each cycle, respects longer cooldowns, and adds jitter. You do not need to manually choose each BUY/SELL. Human escalations still need attention.

The 540-second session budget and stopping margins are designed to stay within the course's **10-minute maximum per session**. Watch actual start/end timestamps; press **Ctrl+C** if necessary. Do not leave it running in another terminal or launch overlapping sessions.

The program runs locally and stops when the process/terminal stops. Holdings remain on Ledger, but stop-loss and take-profit checks run only while an agent is running; they are not standing exchange orders. Multiple short sessions are permitted, but hourly/daily execution quotas are rolling and do not reset when you restart.

Expect HOLDs and occasional legitimate blocks. A 1% BUY sizing reserve leaves room for price movement, but does not guarantee approval or execution.

## 11. Note evaluation evidence on the Google Doc

For every session, record:

- Your name, account identifier and assigned currency; never credentials.
- Git commit, provider, exact model ID, active theory ID/version, and policy settings.
- Session start/end and timezone. Runner logs use UTC; record the dashboard timezone rather than assuming it matches.
- Starting/ending cash, holdings and total portfolio value.
- Executed BUY/SELL records with transaction IDs and fills.
- Blocks, modifications, rejections, escalations, errors and unresolved work separately.
- A short explanation of the BUY, SELL and HOLD decisions.

Coordinate with Member 5 to sample portfolio value at a fixed documented interval, including starting and ending values. **Runner logs alone do not contain a complete portfolio-value curve.** Starting/ending values alone are insufficient to measure maximum drawdown reliably. Keep sample timestamps and values for the metrics and matching benchmarks.

The runner writes logs such as:

```text
runtime/member2-xrp_runner.jsonl
```

Note on the Google Doc: sanitized session records, relevant logs and dashboard evidence. Do not share your private configs, raw keys, or entire runtime database.

The team needs at least **20 EXECUTED BUY/SELL fills in total**, with trades/results covering all five assigned currencies. One BUY and one SELL count as two. Four each is a planning target, not a per-person guide requirement. HOLD, blocked, rejected, pending and escalated entries do not count. Respect the theory and limits; explain any shortfall rather than forcing trades.

## 12. Recovery and common problems

### Uncertain or unresolved order

Stop new submissions. Read/reconcile known outcomes:

```bat
python main.py --account "%LEDGER_ACCOUNT%" --mode recover
```

This makes no Ledger POST. Known IDs are checked against recent history. Unknown IDs and missing entries intentionally remain unresolved. Note on the Google Doc: the intent ID, transaction ID if present, and status—no secrets. Explicit local binding is an investigation tool; do not guess IDs, delete state, or blindly resend an order.

### Position-fraction block

The current cost exceeded cash times the per-BUY fraction. Price movement between checks can cause this. Preserve the block as evidence; do not increase safety limits to bypass it. The shared trader includes a planning reserve, but Guardian limits remain authoritative.

### SELL_SIGNAL_NOT_TRIGGERED

The Guardian's fresh data did not support a SELL. The price may have moved back across a trigger. No sale completed. Wait for a valid signal and preview later.

### No API key / 401 / 403 / model-access error

Check which provider is selected in `config.llm.local.json`. Set `OPENAI_API_KEY` for OpenAI or `GEMINI_API_KEY` for Gemini in the terminal running Python. Check model access, API quota/credits, complete original Ledger keys, and role scopes. Use the real trader key for proposals and Guardian key for reviews. Never paste credentials into an error report.


### Preview showed SELL but the live cycle returned HOLD

A preview is an independent calculation, not an order waiting to be executed. The live cycle checks again and the Guardian also refreshes evidence. Run the standalone preview for the current detailed result. Preserve a block or failure reason and investigate it; do not weaken checks to force the earlier preview.

### Custom theory differs / unregistered custom theory

Only the BTC custom implementation in Section 13 is registered. Its title, strategy, currency selection, and lookback must match exactly. A theory submitted under a different title is not compatible. Confirm the active theory returned by Ledger instead of assuming a new version is active. Other currencies should use the implemented momentum/mean-reversion strategies unless a separate custom implementation has been reviewed and tested.

### Pytest import-file mismatch

Run `python -m pytest tests -q` so pytest collects only the installed test suite. An installer folder can contain a second file with the same name. Remove the obsolete installer copy after installation; preserve `tests/` and `runtime/`.

### Missing theory snapshot

The Guardian needs the saved theory snapshot matching the proposal’s ID/version. An order from a different computer, an earlier installation, or the Attack Simulator may lack that evidence. Do not substitute the latest theory. Note on the Google Doc: the order’s origin, IDs, and observed review outcome. Safe escalation may be expected.

### Clock/replay disagreement

Keep your Windows clock synchronized. The code checks fresh quote age and matches replay against the quote timestamp. A mismatch refreshes once and then HOLDs/escalates rather than using unreliable evidence.

### A role lock is held

Check for another active `main.py` process on the same account. Stop that process cleanly rather than starting another runner. A leftover lock filename does not itself mean the OS lock is still held.

