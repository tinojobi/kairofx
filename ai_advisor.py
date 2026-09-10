"""Advisor layer for candidate trade setups.

The advisor is read-only: it scores setups and reports red flags. It never places,
closes, or sizes trades. The deterministic trading loop remains responsible for
execution and risk controls.
"""
import json
import logging
import re
import subprocess
from openai import OpenAI

import config
from signals import Signal

log = logging.getLogger(__name__)

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        if not config.OPENAI_API_KEY:
            raise RuntimeError("Missing OPENAI_API_KEY in .env")
        _client = OpenAI(api_key=config.OPENAI_API_KEY)
    return _client


SCORE_SYSTEM_PROMPT = """You are a risk-aware trading analyst reviewing forex setups.
You will be given a candidate forex trade signal with pattern, RSI, MACD, ATR, and recent price action.

This bot is currently a conservative mean-reversion EUR/USD H1 system.
Primary signal logic is pattern + RSI extreme.
MACD is informational context only, not a hard veto.
Bullish MACD against a short, or bearish MACD against a long, should reduce conviction but should not automatically force a very low score if the reversal setup is otherwise clean.

Return STRICT JSON with this exact shape:
{
  "score": <integer 1-10>,
  "reasoning": "<2-3 sentences explaining the score>",
  "red_flags": ["<flag1>", "<flag2>"]
}

Scoring guide:
- 1-3: weak setup, conflicting signals, low conviction
- 4-6: marginal, only trade if other factors strong
- 7-8: clean mean-reversion setup, take the trade
- 9-10: rare, very strong setup with clear reversal conditions

Be conservative. The trader is a beginner on a practice account. Better to skip than force trades.
Output JSON only, no markdown fences."""


ATHENA_PROMPT_TEMPLATE = """You are Athena, acting as the advisory layer for KairoFX, Tino's OANDA practice forex bot.

Your role is advisory only. You do not place trades, close trades, change settings, or override risk controls. The deterministic Python bot owns execution and safety.

Strategy context:
- This is a conservative EUR/USD H1 mean-reversion bot.
- The deterministic signal gate is pattern + RSI extreme.
- MACD is informational context only, not a hard veto.
- If MACD conflicts with the proposed direction, reduce conviction and mention it as a red flag, but do not automatically force a very low score if the reversal setup is otherwise clean.
- Judge whether the setup is clean enough for a conservative paper-trading bot, not whether it is trend-following.

Review this candidate forex H1 setup and return STRICT JSON only, with no markdown fences and no extra text:
{{
  "score": <integer 1-10>,
  "decision": "allow" | "skip",
  "reasoning": "<2-3 concise sentences>",
  "red_flags": ["<flag1>", "<flag2>"]
}}

Scoring guide:
- 1-3: weak setup, conflicting signals, skip
- 4-6: marginal, usually skip unless unusually clean
- 7-8: clean mean-reversion setup, allow if deterministic risk checks also pass
- 9-10: rare, very strong reversal setup

Rules:
- Be conservative and risk-aware.
- If the setup is unclear, conflicting, or context is insufficient, choose skip.
- You are reviewing a paper-trading learning bot, not giving financial advice.
- Do not include commands, tool calls, or commentary outside JSON.

Candidate setup JSON:
{payload}
"""


def _setup_payload(signal: Signal, recent_closes: list[float]) -> dict:
    return {
        "instrument": config.INSTRUMENT,
        "timeframe": config.TIMEFRAME,
        "proposed_direction": signal.direction,
        "pattern": signal.pattern,
        "rsi": round(signal.rsi, 2),
        "macd_state": signal.macd_state,
        "atr": round(signal.atr, 5),
        "last_close": signal.last_close,
        "last_high": signal.last_high,
        "last_low": signal.last_low,
        "recent_20_closes": [round(c, 5) for c in recent_closes[-20:]],
        "notes": signal.notes,
    }


def _normalize_advisor_response(data: dict, source: str) -> dict:
    score = int(data.get("score", 0))
    score = max(0, min(10, score))
    decision = str(data.get("decision", "")).lower().strip()
    if decision not in ("allow", "skip"):
        decision = "allow" if score >= config.MIN_AI_SCORE else "skip"

    return {
        "score": score,
        "decision": decision,
        "reasoning": str(data.get("reasoning", ""))[:500],
        "red_flags": list(data.get("red_flags", []))[:5] if isinstance(data.get("red_flags", []), list) else [],
        "advisor": source,
    }


def _extract_json_object(text: str) -> dict:
    """Parse strict JSON, tolerating accidental surrounding text/fences."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        return json.loads(cleaned[start:end + 1])


def _score_with_openai(signal: Signal, recent_closes: list[float]) -> dict:
    user_msg = json.dumps(_setup_payload(signal, recent_closes))
    client = _get_client()
    resp = client.chat.completions.create(
        model=config.OPENAI_MODEL,
        messages=[
            {"role": "system", "content": SCORE_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.2,
        response_format={"type": "json_object"},
    )
    raw = resp.choices[0].message.content
    return _normalize_advisor_response(json.loads(raw), "openai")


def _score_with_athena(signal: Signal, recent_closes: list[float]) -> dict:
    payload = json.dumps(_setup_payload(signal, recent_closes), indent=2)
    prompt = ATHENA_PROMPT_TEMPLATE.format(payload=payload)
    cmd = [
        config.ATHENA_COMMAND,
        "chat",
        "-Q",
        "--source",
        "kairo-fx-advisor",
        "--ignore-rules",
        "--toolsets",
        "safe",
        "--provider",
        config.ATHENA_PROVIDER,
        "--model",
        config.ATHENA_MODEL,
        "--max-turns",
        "1",
        "-q",
        prompt,
    ]
    log.info("Requesting Athena advisor score via Hermes CLI")
    proc = subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        timeout=config.ATHENA_TIMEOUT_SECONDS,
        cwd="/opt/kairo-fx",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"athena advisor exited {proc.returncode}: {proc.stderr[-300:]}")
    data = _extract_json_object(proc.stdout)
    result = _normalize_advisor_response(data, "athena")
    if result["decision"] == "skip" and result["score"] >= config.MIN_AI_SCORE:
        # Fail conservative if the verbal decision conflicts with the numeric gate.
        result["score"] = min(config.MIN_AI_SCORE - 1, result["score"])
    return result


def score_setup(signal: Signal, recent_closes: list[float]) -> dict:
    """Rate the setup. Returns dict with score, reasoning, red_flags, advisor.

    Fail-closed behavior: advisor errors return score 0, so the bot skips the
    trade instead of silently weakening the filter.
    """
    if signal.direction == "none":
        return {"score": 0, "decision": "skip", "reasoning": "no direction", "red_flags": [], "advisor": "none"}

    mode = str(config.ADVISOR_MODE).lower().strip()
    try:
        if mode == "athena":
            return _score_with_athena(signal, recent_closes)
        if mode == "openai":
            return _score_with_openai(signal, recent_closes)
        if mode == "deterministic_only":
            return {
                "score": config.MIN_AI_SCORE,
                "decision": "allow",
                "reasoning": "deterministic_only advisor mode; deterministic signal rules passed",
                "red_flags": [],
                "advisor": "deterministic_only",
            }
        raise RuntimeError(f"Unknown ADVISOR_MODE={config.ADVISOR_MODE!r}")
    except Exception as e:
        log.exception("Advisor scoring failed: %s", e)
        return {"score": 0, "decision": "skip", "reasoning": f"advisor_error: {e}", "red_flags": ["advisor_error"], "advisor": mode}


def write_daily_summary(trades_today: list[dict], account: dict) -> str:
    """Generate a plain-English end-of-day summary via Hermes CLI."""
    try:
        prompt = (
            "You are a concise forex trading journal writer. "
            "Write a short EOD summary in plain English. 4-6 sentences. No financial advice.\n\n"
            + json.dumps({"account": account, "trades": trades_today})
        )
        result = subprocess.run(
            [config.ATHENA_COMMAND, "chat", "-q", prompt, "-Q"],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0 and result.stdout.strip():
            # Strip leading session_id line from --quiet output
            out = result.stdout.strip()
            if out.startswith("session_id:"):
                out = out.split("\n", 1)[-1].strip() if "\n" in out else ""
            return out or "Summary unavailable (empty response)"
        raise RuntimeError(f"hermes chat exited {result.returncode}: {result.stderr.strip()}")
    except Exception as e:
        log.exception("Daily summary failed: %s", e)
        return f"Summary generation failed: {e}"
