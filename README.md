# FX Bot — EUR/USD H1 Paper Trading

Automated forex bot for OANDA practice account.
- **Timeframe:** H1 (1-hour candles)
- **Pair:** EUR/USD
- **Mode:** Paper trading (OANDA practice account)
- **Signals:** Candlestick patterns confirmed by RSI + MACD
- **Risk:** 1% per trade, 2:1 reward-to-risk
- **AI Layer:** GPT (advisory only, scores setups, writes daily reports)
- **Alerts:** Telegram on entries/exits

---

## Setup

### 1. Get credentials

- **OANDA practice account:** sign up at oanda.com, switch to "practice" environment, generate API token under "Manage API Access". Note your account ID.
- **OpenAI API key:** from platform.openai.com
- **Telegram bot:**
  - Talk to `@BotFather` on Telegram, create a bot, save the token
  - Talk to `@userinfobot` to get your chat ID

### 2. Install on VPS

```bash
sudo apt update && sudo apt install -y python3-pip python3-venv
cd ~ && python3 -m venv fx-bot-env && source fx-bot-env/bin/activate

# Copy this folder to ~/fx-bot, then:
cd ~/fx-bot
pip install -r requirements.txt
```

### 3. Configure

```bash
cp .env.example .env
nano .env   # fill in your keys
```

### 4. Test connection

```bash
python test_connection.py
```

### 5. Run

```bash
# Foreground (for testing):
python bot.py

# Background (production):
nohup python bot.py > bot.log 2>&1 &
```

### 6. Run as systemd service (recommended)

See `fx-bot.service` for the template.

---

## Files

| File | Purpose |
|------|---------|
| `bot.py` | Main loop, ticks every hour on candle close |
| `fxctl` | **Agent's control CLI**, see `./fxctl help` |
| `AGENT.md` | **Guide for your AI agent**, paste into agent context |
| `oanda_client.py` | OANDA REST API wrapper |
| `signals.py` | Candle patterns + RSI + MACD detection |
| `risk.py` | Position sizing, SL/TP calculation |
| `ai_advisor.py` | GPT advisory scoring (not for execution) |
| `telegram_notify.py` | Alerts |
| `db.py` | SQLite trade logging |
| `config.py` | Loads .env and settings.json, enforces safety bounds |
| `settings.json` | Runtime-editable params (auto-created on first run) |
| `test_connection.py` | Verify OANDA + OpenAI + Telegram work |

---

## Agent Management

This project is designed to be managed by your Codex agent via the `fxctl` CLI.

**Setup for your agent:**
1. Give your agent the `AGENT.md` file as context (paste it into the system prompt or upload it)
2. Tell your agent the project lives at `~/fx-bot` on the VPS
3. The agent runs `./fxctl <command>` for all operations

**What the agent can do:**
- Read status, positions, signals, trades, logs
- Tweak strategy params within hard-coded safety bounds
- Toggle candlestick patterns on/off
- Pause/resume trading
- Start/stop/restart the bot
- Close positions (only when you ask)

**What the agent cannot do:**
- Place trades directly (only the bot's deterministic logic does)
- Edit `.env` secrets
- Switch from `practice` to `live` (you must do this manually)
- Exceed risk bounds (e.g., > 2% per trade)

**Example agent session:**
```
You: "How's the bot doing?"
Agent: [runs `./fxctl status` and `./fxctl trades --limit 10`]
Agent: "NAV: $9,847 (down 1.5% on day). 2 open positions. Last 10 trades: 4W/6L..."

You: "Why no trades yesterday?"
Agent: [runs `./fxctl signals --limit 20 --json`]
Agent: "20 candles, 3 patterns detected but all had AI scores 3-5. MIN_AI_SCORE is 6."
```

---

## Safety rules built in

1. **Paper account only by default** (`OANDA_ENV=practice`)
2. **Max 1% account risk per trade**
3. **Max 3 open positions at once**
4. **No trading during Friday close window** (Fri 20:00 UTC onward)
5. **AI is advisory only**, GPT cannot place or cancel trades
6. **Hard daily loss limit** stops the bot for the day

---

## What the bot does each hour

1. Wait for H1 candle close
2. Pull last 200 H1 candles
3. Detect candlestick pattern on the just-closed candle
4. Confirm with RSI (oversold for long, overbought for short)
5. Confirm with MACD crossover direction
6. If all 3 agree: calculate SL (1.5x ATR), TP (2x SL distance)
7. Ask GPT for a setup quality score (1-10) and reasoning
8. If GPT score ≥ 6 AND position limit OK: place market order with SL/TP
9. Send Telegram alert with full context
10. Log to SQLite

End of day: GPT writes a plain-English summary of trades.

---

## Important

- **Trade on demo for at least 2-3 months** before considering real money
- **Backtest is not included** in v1, you're testing live on paper
- **Past performance ≠ future results.** This is a learning tool, not a money printer
- Most retail algo strategies underperform. Adjust your expectations accordingly
