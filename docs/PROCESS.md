# Kairo FX — End-to-End Process

Operational reference for the practice FX bot: how a single H1 candle tick moves from market data to (potential) trade, plus the history of what's been tried and where it stands.

For agent-facing authority and rules, see `AGENT.md`. For product positioning and strategy rationale, see `README.md`.

---

## 1. What this is

A deterministic, paper-trading forex bot running against the **OANDA practice tier** on **EUR/USD H1 candles**. Strategy:

- **Entry:** candlestick pattern + RSI/MACD confluence, ATR-based stop-loss
- **Sizing:** fixed % of NAV risked per trade, with ATR-derived stop distance
- **AI layer (advisory only):** an external LLM scores each setup before execution
- **Mode:** `OANDA_ENV=practice` — no live trading without explicit manual approval

The bot is deliberately boring on purpose: deterministic code places trades, the agent (Athena) manages settings and reviews results, the user reviews/approves parameter changes.

---

## 2. End-to-end data flow

```
┌──────────────────┐    ┌──────────────────┐    ┌────────────────┐
│  OANDA REST API   │    │  Cron scheduler  │    │  External LLM  │
│  - candles (H1)   │    │  every H1 candle │    │  AI advisor    │
│  - account info   │    │  close (~50 min) │    │  (scoring)     │
│  - pricing        │    │                  │    │                │
│  - order place    │    │                  │    │                │
└────────┬─────────┘    └────────┬─────────┘    └────────┬───────┘
         │                       │                       │
         ▼                       ▼                       ▼
┌──────────────────────────────────────────────────────────────┐
│                  oanda_client.py                            │
│   Retry window: 8 attempts × exp backoff capped 60s (~263s) │
│   - module constants _RETRY_ATTEMPTS / _RETRY_BACKOFF_BASE_  │
│     SECONDS / _RETRY_BACKOFF_CAP_SECONDS for easy revert     │
└─────────────────────┬────────────────────────────────────────┘
                      │
                      ▼
              ┌──────────────────┐
              │   bot.py loop    │
              │ 1. fetch candles │
              │ 2. signal logic  │
              │ 3. AI advisor    │
              │ 4. risk check    │
              │ 5. place order   │
              │ 6. log to db     │
              └────────┬─────────┘
                       │
              ┌────────┴────────┐
              ▼                 ▼
    ┌──────────────────┐  ┌──────────────────┐
    │   OANDA order    │  │   fx_bot.db      │
    │   placement      │  │  - signals       │
    │   (with SL/TP)   │  │  - trades        │
    └──────────────────┘  │  - ticks         │
                          │  - alerts        │
                          └────────┬─────────┘
                                   │
                                   ▼
                          ┌──────────────────┐
                          │  Telegram notify │
                          │  (alerts only)    │
                          └──────────────────┘
```

Single-direction pipeline: market → candles → signals → risk check → order → DB → notify. Each step is deterministic and re-runnable.

---

## 3. Components

### 3.1 `oanda_client.py` — REST wrapper

Wraps `oandapyV20` with a `_request_with_retry` helper that handles transient OANDA failures.

**Current config (post 2026-10-21 patch):**

| Constant                              | Value | What it controls                                     |
|---------------------------------------|-------|------------------------------------------------------|
| `_RETRY_ATTEMPTS`                     | 8     | Max attempts before giving up                        |
| `_RETRY_BACKOFF_BASE_SECONDS`         | 5     | Base for exponential backoff                          |
| `_RETRY_BACKOFF_CAP_SECONDS`          | 60    | Maximum backoff between attempts                     |

Total wait budget at defaults: `5+10+20+40+60+60+60 = 255s` + ~8 request times ≈ **263s** of retry window. This was raised from the original 3-attempt × 5/10/15s ≈ 31s window after observing that ~50% of H1 candles were being lost to OANDA practice-tier 30-180s outages in Oct 2026.

To revert to the conservative window: change `_RETRY_ATTEMPTS` back to 3 and the sleep line back to `time.sleep(5 * attempt)`.

### 3.2 `bot.py` — main loop

Drives one tick per H1 candle close. Sequence:

1. **Fetch candles** via `oanda_client.get_candles(EUR_USD, H1, 200)`. Cached history for technical indicators.
2. **Compute signal** in `signals.py`:
   - Candlestick pattern (bullish/bearish engulfing, hammer, shooting star; doji off).
   - RSI(14): long if RSI ≤ `RSI_LONG_MAX` (47), short if RSI ≥ `RSI_SHORT_MIN` (53).
   - MACD alignment: required if `REQUIRE_MACD_ALIGNMENT` (true).
3. **AI advisor** in `ai_advisor.py`:
   - Sends candle state + signal context to an external LLM.
   - Returns a score 1-10; **skip** if below `MIN_AI_SCORE` (5).
   - Has `ATHENA_TIMEOUT_SECONDS=120` to bound the call.
4. **Risk check** in `risk.py`:
   - Stop-loss distance = `ATR(14) * ATR_MULTIPLIER_SL` (currently **1.25**).
   - Position size = `NAV * RISK_PER_TRADE_PCT / SL_distance` (currently 0.25%).
   - TP = entry + `REWARD_TO_RISK * SL_distance` (currently 2.5).
5. **Place order** via `oanda_client.place_market_order(units, SL, TP)`.
6. **Log** to `fx_bot.db` via `db.py`.

### 3.3 `signals.py` — pattern + technical logic

Pure-Python detection of:
- Bullish/bearish engulfing patterns
- Hammer / shooting-star
- Doji (currently disabled)
- RSI(14), MACD(12,26,9), ATR(14)

All patterns gated by toggles in `settings.json` so they can be enabled/disabled without code changes.

### 3.4 `risk.py` — position sizing

ATR-based SL distance, fixed-% risk per trade. Never overrides the configured `RISK_PER_TRADE_PCT` even when NAV changes — that's the safety invariant.

### 3.5 `ai_advisor.py` — LLM scoring

Calls an external LLM (slow, ~60-120s typical) to give a final sanity-check score. Used as a tie-breaker, not as a primary signal. The agent (Athena) is the "advisor_mode" target by default; can be retargeted via `ADVISOR_MODE` in settings.

### 3.6 `backtest.py` + `variants.py` — offline research

- `backtest.py::run_backtest` — replays historical candles through the same signal/risk logic as the live bot.
- `variants.py::sweep_variants` — runs `run_backtest` under multiple config overrides (RSI bands, MACD on/off, ATR multiplier, reward:risk) and ranks by net P&L.

Both use `_snapshot` / `_restore` to ensure config overrides never leak back to the live bot.

### 3.7 `fxctl` — operator CLI

The single command-line interface for managing the bot. Lives at `/opt/kairo-fx/fxctl` and is agent-callable (it respects safety bounds).

### 3.8 `db.py` + `fx_bot.db` — SQLite log

Single SQLite database. Tables: `signals`, `trades`, `ticks`, `alerts`. Trade history is the durable record of what the bot actually did; signals table records setups that didn't fire (because of MIN_AI_SCORE or pattern filters).

---

## 4. Operating it

### 4.1 CLI

```bash
cd /opt/kairo-fx
./fxctl status                    # bot state, account, key settings
./fxctl positions                 # open positions with unrealized P&L
./fxctl signals --limit 20        # recent signal evaluations
./fxctl trades --limit 20         # recent executed trades
./fxctl settings                  # full settings dump
./fxctl get ATR_MULTIPLIER_SL     # read one setting
./fxctl set RSI_LONG_MAX 45       # change one setting (validated against bounds)
./fxctl patterns                  # show all pattern toggles
./fxctl pattern hammer off        # toggle a pattern
./fxctl pause                     # stop opening new trades
./fxctl resume                    # resume
./fxctl restart                   # restart the systemd service
./fxctl variants --count 500      # offline variant sweep on last 500 H1 candles
./fxctl backtest --count 500      # single backtest with current settings
```

### 4.2 Service control

The bot runs as a systemd service (`fx-bot.service`). Lifecycle:

```bash
systemctl status fx-bot
systemctl restart fx-bot         # equivalent to ./fxctl restart
journalctl -u fx-bot -n 100      # recent logs
tail -f fx_bot.log               # live runtime log
tail -f fx-bot.err.log           # stderr
```

### 4.3 Tuning

The agent (Athena) is authorised to:
- Read status, positions, signals, trades, logs
- Edit `settings.json` via `fxctl set` (bounded by safety limits in `config.BOUNDS`)
- Toggle patterns on/off
- Pause / resume / restart the bot

The agent is NOT authorised to:
- Edit `.env` (secrets)
- Change `OANDA_ENV` from `practice` to `live` (manual only)
- Place trades directly (only the bot's deterministic logic does this)
- Bypass safety bounds
- Run `fxctl close-all` without explicit user request

Full authority list: `AGENT.md`.

---

## 5. Recent changes and results

### 5.1 Settings (current)

```json
{
  "INSTRUMENT": "EUR_USD",
  "TIMEFRAME": "H1",
  "RISK_PER_TRADE_PCT": 0.25,
  "REWARD_TO_RISK": 2.5,
  "ATR_PERIOD": 14,
  "ATR_MULTIPLIER_SL": 1.25,
  "RSI_PERIOD": 14,
  "RSI_LONG_MAX": 47,
  "RSI_SHORT_MIN": 53,
  "MACD_FAST": 12, "MACD_SLOW": 26, "MACD_SIGNAL": 9,
  "MIN_AI_SCORE": 5,
  "REQUIRE_MACD_ALIGNMENT": true,
  "NO_TRADE_FRI_AFTER_UTC_HOUR": 20,
  "TRADING_ENABLED": true,
  "ADVISOR_MODE": "athena",
  "ATHENA_TIMEOUT_SECONDS": 120,
  "PATTERN_BULLISH_ENGULFING": true,
  "PATTERN_BEARISH_ENGULFING": true,
  "PATTERN_HAMMER": true,
  "PATTERN_SHOOTING_STAR": true,
  "PATTERN_DOJI": false
}
```

### 5.2 What changed in Oct 2026

| Date       | Change                                                                                  | Why                                  | Result                      |
|------------|-----------------------------------------------------------------------------------------|--------------------------------------|-----------------------------|
| 2026-10-21 | `ATR_MULTIPLIER_SL`: 1.5 → 1.25                                                          | 21-day sweep winner (+$1,365)        | Applied, bot restarted      |
| 2026-10-21 | `oanda_client.py` retry: 3 attempts × 5/10/15s → 8 attempts × exp backoff capped 60s   | OANDA practice-tier 30-180s outages  | Pushed, awaiting observation |

### 5.3 Variant sweep results (21-day, 500 H1 candles)

Source: `reports/variants-20261012-123850.md`.

| variant       | trades | win% | net P&L   | vs baseline         |
|---------------|--------|------|-----------|---------------------|
| **atr_1_25**  | 9      | 33.3 | **+$1,365** | clear winner        |
| rr_1_5        | 9      | 44.4 | +$927     | beat baseline       |
| baseline      | 8      | 12.5 | −$4,386   | reference           |
| rr_2_5        | 8      | 12.5 | −$4,386   | same as baseline    |
| macd_on       | 8      | 12.5 | −$4,386   | same as baseline    |
| macd_off      | 16     | 18.8 | −$5,391   | worse               |
| atr_1_75      | 7      | 14.3 | +$3,438   | but only 7 trades   |
| rsi_looser    | 10     | 20.0 | −$3,295   | worse               |
| rsi_tighter   | 3      | 0.0  | −$2,917   | too few trades      |

**Reading:** `atr_1_25` is the cleanest single-knob change. The win rate (33.3%) is still low and n=9 is too small for confidence — but it's the best result on the table and was applied.

### 5.4 Performance to date

| Metric                  | Value                                         |
|-------------------------|-----------------------------------------------|
| Started                 | ~$100,000 paper NAV                           |
| Current NAV             | **$98,199.54** (delta −1.8%)                  |
| Lifetime trades         | 8                                             |
| Last trade              | 2026-09-24                                    |
| Trades in last 30 days  | 0                                             |
| Open positions         | 0                                             |
| Service uptime          | Continuous since 2026-10-19 06:37 UTC         |

**Honest interpretation:** 8 lifetime trades is well below the threshold needed for any meaningful evaluation (30+ trades is the minimum). The bot has not traded since 2026-09-24 because:
1. Market has been in a 50/50 no-pattern regime
2. RSI 47/53 + MACD alignment requirement creates a restrictive entry condition
3. ~50% of H1 candles were being lost to OANDA practice-tier outages (until the 2026-10-21 retry patch)

### 5.5 Known structural issues (not yet fixed)

- **RSI 47/53 + MACD required:** structurally contradictory for counter-trend entries. RSI band 47-53 is a "dead zone" where MACD often disagrees. Flagged but not retuned — n=8 is too small.
- **OANDA practice-tier 401s:** 5-week monotonic degradation (0% → 45-54% loss rate). Mitigation: retry window extension. If outages persist, consider switching the practice tier or migrating signal source.

---

## 6. Standing rules

1. **Never live trade without explicit approval.** `OANDA_ENV=practice` only. Manual change of `.env` is required to switch to live, and that change must come from the user directly.
2. **No retuning on small samples.** Three losing trades is not a signal. 30+ trades minimum for any meaningful parameter adjustment.
3. **Reversible changes only.** Settings tweaks via `fxctl set` are reversible in seconds. Code changes (like the retry patch) are reversible by reverting the commit.
4. **Single-knob changes preferred.** One parameter at a time, then wait for several candles before judging effect.
5. **Evidence over intuition.** Every parameter change should be grounded in either a backtest, a sweep, or live observation.

---

## 7. File layout (cheat sheet)

```
/opt/kairo-fx/
├── oanda_client.py        # OANDA REST wrapper (retry window)
├── bot.py                 # main loop
├── config.py              # config loader + safety bounds
├── settings.json          # editable params (the things fxctl set changes)
├── signals.py             # candlestick patterns + RSI + MACD + ATR
├── risk.py                # position sizing, SL/TP math
├── ai_advisor.py          # external LLM scoring (advisory only)
├── backtest.py            # offline backtest harness
├── variants.py            # offline variant sweeper
├── fxctl                  # operator CLI (agent-callable)
├── performance_report.py  # reporting
├── telegram_notify.py     # alerts (no inbound)
├── db.py                  # SQLite logging
├── fx_bot.db              # live SQLite (signals/trades/ticks/alerts)
├── fx_bot.log             # runtime stdout
├── fx-bot.err.log         # runtime stderr
├── fx-bot.service         # systemd unit
├── AGENT.md               # agent-facing authority and rules
├── README.md              # product positioning
├── docs/
│   └── PROCESS.md         # this file
├── reports/               # variant sweep outputs (one per run)
└── tests/                 # pytest suite
```
