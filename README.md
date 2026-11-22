# Cryptocurrency Trading Agent

The Trader proposes trades using your submitted theory. The Guardian independently checks each proposal before Ledger can execute it.

Each teammate uses their **own Ledger account, assigned currency, theory, API keys and local state**. Ledger trades use simulated money. 

These instructions use **Windows Command Prompt**. Copy commands one line at a time.

## 1. Update and install

From your existing project folder, after preserving any unfinished changes:

```bat
git switch main
git pull --ff-only origin main
.venv\Scripts\activate
python -m pip install --upgrade -r requirements.txt
python -m pip install pytest
```

Wait until installation finishes. The evaluation commands below require the evaluation pull request to be merged into `main`.

## 2. Prepare your Ledger account

On [Ledger](https://ledger.chanyeun.com):

1. Enable your assigned currency in **Security Policy** and set your agreed limits.
2. Submit and activate your theory with that currency selected. A draft is insufficient.
3. Create separate **trader-scoped** and **guardian-scoped** API keys.

For first-time configuration:

```bat
copy config.ledger.example.json config.local.json
notepad config.local.json
```

Fill in the Ledger base URL and your two complete role keys. **Keep an existing working configuration rather than copying over it.**

## 3. Choose one model provider

Both agents use `config.llm.local.json`. Keep your existing working settings, or choose one option below.

**OpenAI GPT-6.2 Sol:**

```bat
copy config.openai.example.json config.llm.local.json
set "OPENAI_API_KEY=YOUR_ACTUAL_OPENAI_KEY"
```

Create your key at [OpenAI API keys](https://platform.openai.com/api-keys).

**Gemini:**

```bat
copy config.gemini.example.json config.llm.local.json
set "GEMINI_API_KEY=YOUR_ACTUAL_GEMINI_KEY"
```

Create your key at [Google AI Studio](https://aistudio.google.com/apikey). Use a model your account can access; the template contains the model verified during development.

Replace the key placeholder locally. `set` applies to the current terminal. For future terminals, save the key through Windows **Edit environment variables for your account**, then reopen your terminal or VS Code.

Keep real keys, private configurations and runtime files out of GitHub, screenshots and the Google Doc.

## 4. Check setup

```bat
python -m pytest tests -q
python test_connection.py
python verify_llm.py
```

Tests run offline. `test_connection.py` reads your Ledger policy. `verify_llm.py` makes two real model calls using sample data; it does not place trades or test your personal theory.

Resolve any failures before continuing.

## 5. Run with portfolio sampling

**Replace `label` and `Name` below with your existing account label and your name.** Keep using the same label and state database as your earlier runs.

First, run a short preview:

```bat
python -m evaluation.run_session --account label --member "Name" --duration 90
```

This collects samples and previews decisions without enabling Ledger trades.

After it succeeds, run a measured trading session:

```bat
python -m evaluation.run_session --account label --member "Name" --submit --duration 360 --sample-interval 15
```

- `--submit` enables proposals and Guardian reviews on your simulated account.
- Add `--flat-entry` if your theory prohibits buying more while holding. 
- This command starts the agents and sampler together. **Do not run another agent process alongside it.**
- Keep the terminal open. HOLD and zero-trade sessions are normal possible results.
- Keep theory, policy, model and schedule fixed during final evaluation; record any changes separately.

The default agent budget is six minutes, leaving time for setup and final data collection. Keep each live session within the course's ten-minute limit. Stop-loss and take-profit checks run only while the agent is running.

## 6. Save your results

Look for the printed output folder under:

```text
runtime\evaluation\
```

Each run saves portfolio samples, agent logs, before/after Ledger evidence and:

- **`.report.json`** — portfolio return, sampled maximum drawdown, cash/buy-and-hold comparisons, fills and other outcomes.
- **`.samples.csv`** — timestamped portfolio values and paired prices.

Check **`Recording complete: True`** and read the report's warnings. Incomplete runs keep their evidence and provisional metrics. Drawdown uses the samples collected; starting/ending balances alone cannot establish it. Benchmark assumptions and separate quote/portfolio observation times are documented in the report.

Confirm completed trades using Ledger's **Transactions** and **Guardian Log**. Only `EXECUTED` orders with fills count as completed trades.

**Note on the Google Doc:**
use the template provided

## 7. If something fails

- **Missing model key:** set the selected provider's environment variable in this terminal.
- **HOLD:** inspect the recorded reason where available. A later preview can have a different result because it uses fresh data.
- **Incomplete recording:** keep the files and report the warnings; do not invent missing samples.
- **Pending, uncertain or unresolved work:** check Ledger and reconcile before another trading run:

```bat
python main.py --account label --mode recover
```

Use your own existing account label. Keep `runtime/`: it contains your trading state, theory snapshots and evaluation evidence. Never delete state or change labels to bypass unresolved orders.
