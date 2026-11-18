# 💰 Xərc Agenti (Project B — Expense Agent)

An AI agent that understands free text like *"Bu gün naharda 7 manat xərclədim"*, records the expense,
gives reports, and warns you when you go over your budget. Interface is in Azerbaijani.

Built with **Python + Streamlit + three free LLM providers** (Google Gemini, Groq, OpenRouter) and an optional paid **Claude Haiku 5.5** mode. In **Avtomatik** mode the agent decides which model
handles each message; you can also pick one yourself in the sidebar. Data is stored in a simple JSON file
(`data/expenses.json`).

## Project structure

```
xerc-agenti/
├── app.py                 # Streamlit UI (chat + sidebar dashboard)
├── agent.py               # Agent loop: pick model → think → call tool → read result → repeat → answer
├── router.py              # "Avtomatik" mode: decides Gemini vs Groq for each message
├── models.py              # Model registry (Gemini, Groq, OpenRouter, router model) + cooldown
├── tools.py               # 9 tools + their JSON schemas
├── prompts.py             # System prompt (role, goal, tool rules, guardrails)
├── tests/test_tools.py    # Tests (no API key needed)
├── docs/DELIVERABLES.md   # Exam deliverables: system prompt, tool list, test table, prompt journal
├── data/                  # expenses.json is created here automatically
├── .env.example           # Copy to .env and add your API key
├── .streamlit/secrets.toml.example
└── requirements.txt
```

## Tools (9)

| Tool | What it does |
|---|---|
| `add_expense(amount, category, description?, date?)` | Records an expense |
| `get_summary(period, category?)` | Total + breakdown for today / yesterday / week / month / all |
| `list_expenses(period?, limit?)` | Recent expenses with IDs (used for edit/delete) |
| `update_expense(expense_id, ...)` | Edits an expense ("bayaq yazdığımı 5 manat et") |
| `delete_expense(expense_id)` | Deletes an expense |
| `set_budget(period, amount)` | Sets a daily / weekly / monthly limit |
| `check_budget()` | Compares spending with limits → ok / warning / exceeded |
| `send_notification(message, level)` | Sends a warning (shown in the app, optionally to Telegram) |
| `convert_currency(amount, from_currency, to_currency?)` | Converts currencies. Rate sources in order: currencylayer.com (if `CURRENCYLAYER_API_KEY` is set) → Central Bank of Azerbaijan (CBAR) → open.er-api.com. Rates are fetched once a day and cached in `data/rates_cache.json` |

## Models and how the agent chooses

| Model | Default ID | Used for |
|---|---|---|
| Google Gemini Flash | `gemini-flash-latest` | Complex requests: several expenses in one message, "bayaq yazdığımı dəyiş", budgets + analysis, unclear wording. Better Azerbaijani. |
| Groq · GPT-OSS 120B | `openai/gpt-oss-120b` | Simple requests: one clear expense, a quick summary, greetings/off-topic. Very fast. |
| OpenRouter (free models) | `openrouter/free` | Backup: used automatically when the others hit their limits. OpenRouter picks a free model that supports tool calling. |
| Claude Haiku 5.5 💲 (paid, cheapest Claude) | `claude-haiku-5-5` | Only when you pick it in the sidebar (~0.1 cent per message). Not used by Avtomatik mode; set `ANTHROPIC_AS_FALLBACK=true` to use it when the free models hit their limits. The sidebar shows the session's Claude cost. |
| Router (Groq · Llama 3.1 8B) | `llama-3.1-8b-instant` | In **Avtomatik** mode it reads each message and returns `{"model": ..., "reason": ...}`. |

The sidebar has a **🤖 Model** selector: *Avtomatik (agent seçir)*, *Google Gemini Flash*, *Groq · GPT-OSS 120B*
or *OpenRouter*. Models that recently hit their limit show **⏳ limitdə**.
Every reply shows which model answered and why (for example *🧭 Groq · GPT-OSS 120B — agent seçdi: sadə sorğu*).

- If the router fails, simple keyword rules decide instead.
- If the chosen model hits its free-tier limit, the agent **switches to the next model automatically** (🔁)
  and skips the limited one for 60 seconds, so no requests are wasted on it.
- Only the last 24 messages are sent to the model (`MAX_HISTORY_MESSAGES`), which keeps token use down.
- Both models share the same conversation history, so memory still works after a switch.
- With only one key, that model is used and the selector is disabled.

## Get free API keys (no credit card)

- **Gemini:** https://aistudio.google.com/apikey → *Create API key* → `GEMINI_API_KEY`
- **Groq:** https://console.groq.com/keys → *Create API Key* → `GROQ_API_KEY`
- **OpenRouter:** https://openrouter.ai/settings/keys → *Create Key* → `OPENROUTER_API_KEY`
- **Claude (paid):** https://console.anthropic.com/settings/keys → `ANTHROPIC_API_KEY` (uses your Anthropic credits)

Add Gemini + Groq for Avtomatik mode; add OpenRouter as extra capacity. OpenRouter's free models allow
about 20 requests/minute and 50/day on an account that has never bought credits. Free tiers have per-minute limits; one message can use 2–4 model calls.

## Run locally (Windows)

```bash
cd Desktop\xerc-agenti
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

copy .env.example .env      # then open .env and paste GEMINI_API_KEY and GROQ_API_KEY
streamlit run app.py
```

The app opens at http://localhost:8501.

Run the tests (no API key needed):

```bash
python tests/test_tools.py
```

## Get a working link (Streamlit Community Cloud, free)

1. Push this folder to a GitHub repo (`.env` is ignored, so your key will not be uploaded).
2. Go to https://share.streamlit.io → **Create app** → choose the repo, main file `app.py`.
3. **Advanced settings → Secrets**, paste:
   ```toml
   GEMINI_API_KEY = "your-gemini-key"
   GROQ_API_KEY = "your-groq-key"
   ```
4. Deploy. You get a public `https://<name>.streamlit.app` link to submit.

> Note: on Streamlit Cloud everyone who opens the link shares the same `expenses.json`,
> and it resets when the app restarts. Use **🗑️ Məlumatları sıfırla** before the demo.

## Optional: Telegram notifications

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env` and `send_notification` will also send
the budget warning to your Telegram.
