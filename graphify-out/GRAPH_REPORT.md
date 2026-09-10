# Graph Report - kairo-fx  (2026-05-26)

## Corpus Check
- 13 files · ~6,189 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 163 nodes · 246 edges · 11 communities
- Extraction: 94% EXTRACTED · 6% INFERRED · 0% AMBIGUOUS · INFERRED: 14 edges (avg confidence: 0.5)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Community 0|Community 0]]
- [[_COMMUNITY_Community 1|Community 1]]
- [[_COMMUNITY_Community 2|Community 2]]
- [[_COMMUNITY_Community 3|Community 3]]
- [[_COMMUNITY_Community 4|Community 4]]
- [[_COMMUNITY_Community 5|Community 5]]
- [[_COMMUNITY_Community 6|Community 6]]
- [[_COMMUNITY_Community 7|Community 7]]
- [[_COMMUNITY_Community 8|Community 8]]
- [[_COMMUNITY_Community 9|Community 9]]
- [[_COMMUNITY_Community 10|Community 10]]

## God Nodes (most connected - your core abstractions)
1. `Signal` - 16 edges
2. `OandaClient` - 16 edges
3. `detect_signal()` - 14 edges
4. `score_setup()` - 11 edges
5. `run_one_tick()` - 10 edges
6. `calc_position_size()` - 7 edges
7. `validate_trade_conditions()` - 7 edges
8. `_score_with_openai()` - 7 edges
9. `_score_with_athena()` - 7 edges
10. `FX Bot — EUR/USD H1 Paper Trading` - 7 edges

## Surprising Connections (you probably didn't know these)
- `Signal` --uses--> `Signal`  [INFERRED]
  risk.py → signals.py
- `int` --uses--> `Signal`  [INFERRED]
  risk.py → signals.py
- `bool` --uses--> `Signal`  [INFERRED]
  risk.py → signals.py
- `OpenAI` --uses--> `Signal`  [INFERRED]
  ai_advisor.py → signals.py
- `Signal` --uses--> `Signal`  [INFERRED]
  ai_advisor.py → signals.py

## Communities (11 total, 0 thin omitted)

### Community 0 - "Community 0"
Cohesion: 0.07
Nodes (26): ADVISOR_MODE, ATHENA_TIMEOUT_SECONDS, ATR_MULTIPLIER_SL, ATR_PERIOD, CANDLE_HISTORY, DAILY_LOSS_LIMIT_PCT, INSTRUMENT, MACD_FAST (+18 more)

### Community 1 - "Community 1"
Cohesion: 0.15
Nodes (16): calc_position_size(), calc_sl_tp(), Risk management: position size, stop loss, take profit.  For EUR/USD on a USD-de, Calculate stop loss and take profit prices based on ATR., Returns number of units to trade so that loss-at-SL equals RISK_PER_TRADE_PCT of, Return (ok, reason). Checks position limits and daily loss limit., validate_trade_conditions(), Signal (+8 more)

### Community 2 - "Community 2"
Cohesion: 0.11
Nodes (18): 1. Get credentials, 2. Install on VPS, 3. Configure, 4. Test connection, 5. Run, 6. Run as systemd service (recommended), Agent Management, code:bash (sudo apt update && sudo apt install -y python3-pip python3-v) (+10 more)

### Community 3 - "Community 3"
Cohesion: 0.26
Nodes (16): float, Signal, str, _extract_json_object(), _get_client(), _normalize_advisor_response(), Advisor layer for candidate trade setups.  The advisor is read-only: it scores s, Parse strict JSON, tolerating accidental surrounding text/fences. (+8 more)

### Community 4 - "Community 4"
Cohesion: 0.21
Nodes (13): bool, float, str, datetime, is_trading_window(), main(), Main trading loop. Runs forever, ticks on H1 candle close., Block weekend and Friday-late trading. (+5 more)

### Community 5 - "Community 5"
Cohesion: 0.26
Nodes (15): atr(), detect_signal(), is_bearish_engulfing(), is_bullish_engulfing(), is_doji(), is_hammer(), is_shooting_star(), macd() (+7 more)

### Community 6 - "Community 6"
Cohesion: 0.14
Nodes (12): code:block1 (fx-bot/), code:bash (# Health check), Common Commands, How to Add a New Candlestick Pattern, Project at a Glance, Project Layout, Reporting Style, Required Behaviors (+4 more)

### Community 7 - "Community 7"
Cohesion: 0.25
Nodes (7): Return last N completed candles as a DataFrame., Return current bid/ask., Place a market order with SL and TP attached., DataFrame, float, int, str

### Community 8 - "Community 8"
Cohesion: 0.31
Nodes (9): float, int, str, _conn(), get_today_trades(), init(), log_signal(), log_trade() (+1 more)

### Community 9 - "Community 9"
Cohesion: 0.29
Nodes (6): str, __getattr__(), load_settings(), Central config. Reads .env for secrets, settings.json for tunable params.  The a, Raise ValueError if value is out of safety bounds. Returns coerced value., validate_setting()

### Community 10 - "Community 10"
Cohesion: 0.50
Nodes (3): Telegram alerts. Silent if not configured., send(), str

## Knowledge Gaps
- **52 isolated node(s):** `INSTRUMENT`, `TIMEFRAME`, `CANDLE_HISTORY`, `RISK_PER_TRADE_PCT`, `REWARD_TO_RISK` (+47 more)
  These have ≤1 connection - possible missing edges or undocumented components.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `OandaClient` connect `Community 4` to `Community 1`, `Community 7`?**
  _High betweenness centrality (0.108) - this node is a cross-community bridge._
- **Why does `detect_signal()` connect `Community 5` to `Community 1`, `Community 4`?**
  _High betweenness centrality (0.074) - this node is a cross-community bridge._
- **Why does `Signal` connect `Community 1` to `Community 3`, `Community 5`?**
  _High betweenness centrality (0.065) - this node is a cross-community bridge._
- **Are the 9 inferred relationships involving `Signal` (e.g. with `Signal` and `float`) actually correct?**
  _`Signal` has 9 INFERRED edges - model-reasoned connections that need verification._
- **Are the 5 inferred relationships involving `OandaClient` (e.g. with `datetime` and `bool`) actually correct?**
  _`OandaClient` has 5 INFERRED edges - model-reasoned connections that need verification._
- **What connects `INSTRUMENT`, `TIMEFRAME`, `CANDLE_HISTORY` to the rest of the system?**
  _73 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Community 0` be split into smaller, more focused modules?**
  _Cohesion score 0.07407407407407407 - nodes in this community are weakly interconnected._