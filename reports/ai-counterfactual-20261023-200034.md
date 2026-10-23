# AI advisor counterfactual (EURUSD)

Generated 2026-10-23 20:00 UTC.

> **WARNING: tiny sample.** Every row below has far too few trades for a reliable conclusion; the bootstrap CIs are wide and nearly all include zero. Treat as anecdote, not evidence.

- Logged signals (long/short, trade_placed or skip_low_ai_score): 119; replayed: 119; outside candle window / unusable: 0.
- Each simulated from next bar open, SL=1.25*logged ATR, RR=2.5, engine costs (0.8 pip spread + 0.2 pip stop slippage); signals independent (no position cap). Yahoo mid prices differ slightly from OANDA fills.

| group | n | win% | exp net R | total R | 95% CI exp |
|---|---|---|---|---|---|
| AI score >= 5 (would trade) | 25 | 24.0% | -0.211 | -5.3 | [-0.80, +0.39] |
| AI score < 5 (skipped) | 94 | 41.5% | +0.351 | +33.0 | [-0.02, +0.74] |
| ALL signals (no AI filter) | 119 | 37.8% | +0.233 | +27.7 | [-0.10, +0.57] |
| logged action = trade_placed | 8 | 37.5% | +0.237 | +1.9 | [-1.07, +1.55] |
| logged action = skip_low_ai_score | 111 | 37.8% | +0.233 | +25.8 | [-0.09, +0.61] |

By AI score bucket:

| score | n | win% | exp net R | total R | 95% CI exp |
|---|---|---|---|---|---|
| 0 | 6 | 0.0% | -1.369 | -8.2 | [-1.73, -1.08] |
| 3 | 30 | 33.3% | +0.185 | +5.5 | [-0.47, +0.84] |
| 4 | 58 | 50.0% | +0.615 | +35.7 | [+0.13, +1.09] |
| 5 | 17 | 17.6% | -0.422 | -7.2 | [-1.08, +0.35] |
| 7 | 7 | 42.9% | +0.428 | +3.0 | [-0.59, +1.93] |
| 8 | 1 | 0.0% | -1.099 | -1.1 | [-1.10, -1.10] |

## Conclusion

Taken-by-AI group expectancy -0.211 R (n=25) vs skipped +0.351 R (n=94) vs all +0.233 R. 
The confidence intervals overlap heavily, so this sample does NOT demonstrate that the AI filter adds (or destroys) value. Absolute expectancy of the underlying signal is also the baseline-strategy question answered in the main research report.

Caveats: logged signals cluster in time (several fall in the same few trending days and their simulated trades overlap), so the effective independent sample is smaller than n and the CIs are too narrow. The ~5 months covered is a single regime. The positive 'ALL signals' result here contrasts with the multi-year baseline backtest (negative after costs); it is most likely a lucky window rather than evidence of edge. Score buckets 0 and 8 hold only 6 and 1 trades.
