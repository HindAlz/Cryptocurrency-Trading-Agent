# Team setup: run the agents on your own Ledger account

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
   - Select **momentum** or **mean_reversion**. Custom strategies are not implemented in the shared code.
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

## 3. activate your python environment

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

## 5. Configure Gemini

Create/view your own Gemini API key in Google AI Studio:
https://aistudio.google.com/apikey

For first-time model setup:

```bat
copy config.llm.example.json config.llm.local.json
notepad config.llm.local.json
```

Keep the existing file if you have already configured it. Use the exact model ID verified by the team. The model used successfully in my development runs was `gemini-3.5-flash-lite`; report an unavailable-model error rather than guessing a replacement during final evaluation.

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

Set the actual key in the **same Command Prompt window**:

```bat
set "GEMINI_API_KEY=YOUR_ACTUAL_GEMINI_KEY"
```

Check availability without displaying it:

```bat
python -c "import os; print('Key found' if os.getenv('GEMINI_API_KEY', '').strip() else 'Key missing')"
```

For future terminals, either repeat the `set` command privately or save a **user environment variable** through Windows' “Edit environment variables for your account” dialog. Restart VS Code/Command Prompt after saving it there. The variable name is `GEMINI_API_KEY`; its value is your key.

Check that the model config is ignored:

```bat
git check-ignore config.llm.local.json
```

The Gemini key is different from both Ledger keys. Do not put any real key into code, model prompts, screenshots, GitHub, or shared evidence.

## 6. Choose your stable account label and entry option

Set a label for your account. This is a local label, not your Ledger login or key. Example for Member 2 assigned XRP:

```bat
set "LEDGER_ACCOUNT=member2-xrp"
```

Replace `member2-xrp` with your own agreed label. Use only letters, digits, underscores, and hyphens. Keep the same label and state file for all future runs on this account.

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

Verify real Gemini calls for both roles:

```bat
python verify_llm.py
```

This uses synthetic example data and submits no Ledger orders. It is not a check of your personal theory or evidence of a real fill.

Run the offline tests:

```bat
python -m pip install pytest
python -m pytest -q
```

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

Neither command submits trades or reviews without `--submit`. The trader preview may call Gemini. It returns:

| Output | Meaning |
| --- | --- |
| `PREVIEW` with BUY or SELL | A candidate passed the trader's checks; nothing was submitted |
| `HOLD: No eligible theory trigger` | No qualifying entry/exit under current conditions, or optional flat-entry rule prevents another BUY |
| Other `HOLD` reason | Inspect the reason; it may indicate missing theory, permissions, data, or a model/validation problem |
| `COOLDOWN` | Wait at least the stated cooldown; do not repeatedly retry |
| Unresolved-order message | Investigate/reconcile earlier work before another proposal |

PREVIEW is not guaranteed execution. Quotes and policy can change before Guardian review. Do not lower thresholds or weaken checks just to get activity.

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
| `HOLD` | No new proposal; valid if no trigger. If the concise summary hides the reason, use the trader preview again |
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

The program runs locally and stops when the process/terminal stops. Multiple short sessions are permitted, but hourly/daily execution quotas are rolling and do not reset when you restart.

Expect HOLDs and occasional legitimate blocks. A 1% BUY sizing reserve leaves room for price movement, but does not guarantee approval or execution.

## 11. Note evaluation evidence on the Google Doc

For every session, record:

- Your name, account identifier and assigned currency; never credentials.
- Git commit, active theory ID/version, and policy settings.
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

### No API key / 401 / 403

Check your local Gemini environment variable, complete original Ledger keys, and role scopes. Use the real trader key for proposals and Guardian key for reviews. Never paste credentials into an error report.


### Clock/replay disagreement

Keep your Windows clock synchronized. The code checks fresh quote age and matches replay against the quote timestamp. A mismatch refreshes once and then HOLDs/escalates rather than using unreliable evidence.

### A role lock is held

Check for another active `main.py` process on the same account. Stop that process cleanly rather than starting another runner. A leftover lock filename does not itself mean the OS lock is still held.


