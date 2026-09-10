# Kairo FX Phase 1 Baseline Report — 2026-09-06

## Scope

Phase 1 is the measurement foundation for Kairo FX. This report records the first verified baseline after adding local performance-reporting helpers.

## Files changed

- `broker_adapters.py`
  - Added broker-neutral `ClosedTrade` data shape.
  - Added OANDA closed-trade adapter that normalizes raw OANDA trades.
  - Added explicit MT5 adapter placeholder so future MT5 support plugs into the same reporting interface instead of OANDA-specific report logic.
- `performance_report.py`
  - Added read-only SQLite signal/trade aggregation.
  - Added broker-neutral closed-trade matching helpers for realized P/L review.
  - Added `build_closed_performance_report(broker=..., count=...)` for adapter-driven broker reconciliation.
  - Added concise operator summary rendering for Telegram/log use.
  - Added `build_daily_digest(...)` for Telegram-ready daily operator reporting.
- `backtest.py`
  - Added minimal candle replay foundation for current strategy simulation.
  - Added SL/TP execution simulation, expectancy, win rate, net P/L and drawdown summary.
- `/root/.hermes/scripts/kairo_daily_digest.sh`
  - Added read-only scheduled-report script that runs `./fxctl daily-digest --broker oanda --count 100 --recent-days 30`.
- `fxctl`
  - Added `performance-report` command.
  - Added `closed-performance --broker oanda|mt5 --count N` command.
  - Added `daily-digest --broker oanda|mt5 --count N --recent-days N` command.
  - Added `backtest --count N --starting-balance N --risk-per-trade-pct N` command.
  - Added `--format json|summary` for machine-readable and short human-readable output.
- `tests/test_broker_adapters.py`
  - Added tests for the broker-neutral trade shape, OANDA normalization, and explicit MT5 placeholder behavior.
- `tests/test_performance_report.py`
  - Added unit tests for local signal bottleneck reporting and closed-trade P/L summaries.
- `tests/test_fxctl_performance_report.py`
  - Added unit test confirming `fxctl performance-report` is registered.

## Current operating state checked before implementation

- Bot service: active/running via `systemctl is-active fx-bot` and process check.
- Mode: OANDA practice.
- Instrument/timeframe: EUR/USD H1.
- Open positions: 0.
- Account NAV: 98478.3608 USD.
- Current settings: `RISK_PER_TRADE_PCT=0.25`, `MIN_AI_SCORE=6`, `REQUIRE_MACD_ALIGNMENT=true`.

## Local signal baseline

Source: `./fxctl --json performance-report --recent-days 30`

- Signals logged: 1,565.
- Signal window: 2026-05-25T15:00:10.284544+00:00 to 2026-09-04T19:00:10.922267+00:00.
- Action counts:
  - `skip_no_signal`: 1,465.
  - `skip_low_ai_score`: 93.
  - `trade_placed`: 7.
- Direction counts:
  - `none`: 1,465.
  - `long`: 51.
  - `short`: 49.
- Pattern counts:
  - `none`: 1,193.
  - `bullish_engulfing`: 125.
  - `bearish_engulfing`: 123.
  - `shooting_star`: 63.
  - `hammer`: 61.
- Directional candidates: 100.
- Candidate rate: 6.39% of signal evaluations.
- Trade conversion from directional candidates: 7.0%.

## Recent 30-day signal baseline

- Signals: 432.
- `skip_no_signal`: 406.
- `skip_low_ai_score`: 23.
- `trade_placed`: 3.

## Local trade baseline

- Local bot trade records: 7.
- First local bot trade: 2026-06-18T13:01:33.916372+00:00.
- Latest local bot trade: 2026-08-31T07:00:42.806113+00:00.
- Direction split:
  - Long: 5.
  - Short: 2.

## Broker-reconciled closed-trade baseline

Source: OANDA closed trades matched to local bot trade records by instrument, direction and open hour.

- Matched bot trades: 7.
- Wins: 3.
- Losses: 4.
- Win rate: 42.9%.
- Net realized P/L: -1420.29 USD.
- Average win: 951.73 USD.
- Average loss: -1068.87 USD.
- Take-profit fills: 3.
- Stop-loss fills: 4.

## Interpretation

The bot is operationally healthy, but the performance bottleneck is upstream signal quality and selectivity:

- 93.61% of all signal evaluations produced no directional candidate.
- Only 6.39% produced a long/short candidate.
- Only 7 of 100 directional candidates became trades.
- Broker-confirmed realized P/L is negative across the 7 bot-matched trades.

This supports keeping risk low while Phase 1/2 measurement and research improves the strategy.

## Verification performed

- Wrote failing tests first for the new performance report module and `fxctl` command registration.
- Ran focused tests until green:
  - `.venv/bin/python -m unittest tests.test_performance_report -v`
  - `.venv/bin/python -m unittest tests.test_fxctl_performance_report -v`
- Ran full test suite:
  - `.venv/bin/python -m unittest discover -s tests -v`
  - Result: 19 tests passed.
- Ran live local report command:
  - `./fxctl --json performance-report --recent-days 30`
  - Result: produced the baseline metrics recorded above.
- Ran live summary report command:
  - `./fxctl performance-report --recent-days 30 --format summary`
  - Result: produced concise operator summary and identified upstream signal starvation as the primary bottleneck.
- Ran broker-neutral closed-performance command against OANDA:
  - `./fxctl --json closed-performance --broker oanda --count 100`
  - Result: matched 7 local bot trades to broker closed trades, with 3 wins, 4 losses and -1420.29 USD net realized P/L.
- Ran MT5 placeholder check:
  - `./fxctl --json closed-performance --broker mt5 --count 10`
  - Result: exited cleanly with code 2 and a clear message that the MT5 adapter boundary exists but implementation is pending.

## Phase 1 completion additions

### Daily digest

- Command: `./fxctl daily-digest --broker oanda --count 100 --recent-days 30`.
- Script: `/root/.hermes/scripts/kairo_daily_digest.sh`.
- Scheduled job: `Kairo FX Daily Digest`, job id `9be45a88aa54`.
- Schedule: `30 7 * * *`.
- Next run after creation: 2026-09-07T07:30:00+02:00.
- Delivery: origin chat.
- Mode: `no_agent=true`; script output is delivered directly.

Verified script output:

```text
Kairo FX Daily Digest
Mode: practice | Pair: EUR_USD H1
Trading enabled: True | Risk/trade: 0.25% | Min AI: 6
NAV: 98478.3608 USD
Open positions: 0
Signals: 1565 total; 6.39% candidate rate; 7.0% candidate-to-trade conversion.
Recent 30d: 431 signals; 405 no-signal, 23 low-score, 3 trades.
Closed P/L: -1420.29 USD; 3W/4L; broker=oanda.
Primary bottleneck: upstream signal starvation.
```

### Backtest foundation

- Command: `./fxctl --json backtest --count 500 --starting-balance 10000 --risk-per-trade-pct 1.0`.
- Uses current strategy signal logic and SL/TP execution simulation over fetched candles.
- Measurement only; does not place trades or alter settings.
- Current limitation: baseline replay simulates deterministic signal + SL/TP behavior, not Athena advisor scoring. That is deliberate for the foundation slice; advisor simulation belongs in Phase 2 research.

Verified 500-candle baseline output summary:

- Trades: 6.
- Wins: 3.
- Losses: 3.
- Win rate: 50.0%.
- Net P/L on 10000 starting balance at 1.0% risk: 296.26.
- Expectancy: 49.38 per simulated trade.
- Max drawdown: 2.97%.
- Ending balance: 10296.26.

## Final Phase 1 verification

- Full test suite:
  - `.venv/bin/python -m unittest discover -s tests -v`
  - Result: 24 tests passed.
- Daily digest command:
  - `./fxctl daily-digest --broker oanda --count 100 --recent-days 30`
  - Result: produced Telegram-ready digest with status, NAV, open positions, signal counts, broker P/L and bottleneck diagnosis.
- Daily digest script:
  - `/root/.hermes/scripts/kairo_daily_digest.sh`
  - Result: produced the same digest successfully.
- Cron job listing confirmed:
  - `Kairo FX Daily Digest`, job id `9be45a88aa54`, scheduled and enabled.
- Backtest command:
  - `./fxctl --json backtest --count 500 --starting-balance 10000 --risk-per-trade-pct 1.0`
  - Result: produced baseline replay metrics above.

## Phase 1 status

Complete.

## Next phases

Phase 2 should now use the measurement layer to run strategy research safely offline:

1. Add advisor-score simulation or historical advisor-decision replay.
2. Run RSI/MACD/session/ATR/reward-risk grids against the backtest foundation.
3. Compare candidates against the current baseline before changing live practice settings.
