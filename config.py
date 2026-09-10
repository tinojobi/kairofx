"""Central config. Reads .env for secrets, settings.json for tunable params.

The agent edits settings.json via `fxctl set`. The bot picks up changes on the next tick.
"""
import os
import json
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ===== Secrets (from .env, never auto-edited) =====
OANDA_API_TOKEN = os.getenv("OANDA_API_TOKEN")
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID")
OANDA_ENV = os.getenv("OANDA_ENV", "practice")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# Athena/Hermes advisor bridge. The bot still owns execution; Athena only scores setups.
ATHENA_COMMAND = os.getenv("ATHENA_COMMAND", "hermes")
ATHENA_PROVIDER = os.getenv("ATHENA_PROVIDER", "openai-codex")
ATHENA_MODEL = os.getenv("ATHENA_MODEL", "gpt-5.5")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# ===== Paths =====
DB_PATH = "fx_bot.db"
LOG_PATH = "fx_bot.log"
SETTINGS_PATH = "settings.json"

# ===== Defaults (used to seed settings.json on first run) =====
DEFAULTS = {
    # Trading
    "INSTRUMENT": "EUR_USD",
    "TIMEFRAME": "H1",
    "CANDLE_HISTORY": 200,

    # Risk
    "RISK_PER_TRADE_PCT": 1.0,
    "REWARD_TO_RISK": 2.0,
    "MAX_OPEN_POSITIONS": 3,
    "DAILY_LOSS_LIMIT_PCT": 3.0,
    "ATR_PERIOD": 14,
    "ATR_MULTIPLIER_SL": 1.5,

    # Signals
    "RSI_PERIOD": 14,
    "RSI_LONG_MAX": 40,
    "RSI_SHORT_MIN": 60,
    "MACD_FAST": 12,
    "MACD_SLOW": 26,
    "MACD_SIGNAL": 9,
    "MIN_AI_SCORE": 6,
    "REQUIRE_MACD_ALIGNMENT": False,

    # Advisor: openai, athena, or deterministic_only. Athena is advisory only.
    "ADVISOR_MODE": "athena",
    "ATHENA_TIMEOUT_SECONDS": 45,

    # Pattern toggles
    "PATTERN_BULLISH_ENGULFING": True,
    "PATTERN_BEARISH_ENGULFING": True,
    "PATTERN_HAMMER": True,
    "PATTERN_SHOOTING_STAR": True,
    "PATTERN_DOJI": False,  # neutral pattern, off by default

    # Time filters
    "NO_TRADE_FRI_AFTER_UTC_HOUR": 20,
    "NO_TRADE_WEEKEND": True,

    # Master switch (agent uses this to pause trading without killing process)
    "TRADING_ENABLED": True,
}


# ===== Hard-coded safety bounds (agent CANNOT exceed these) =====
BOUNDS = {
    "RISK_PER_TRADE_PCT":   (0.1,  2.0),    # max 2% per trade, ever
    "REWARD_TO_RISK":       (1.0,  5.0),
    "MAX_OPEN_POSITIONS":   (1,    5),
    "DAILY_LOSS_LIMIT_PCT": (1.0,  5.0),
    "ATR_MULTIPLIER_SL":    (0.5,  4.0),
    "RSI_LONG_MAX":         (20,   60),
    "RSI_SHORT_MIN":        (40,   80),
    "MIN_AI_SCORE":         (1,    10),
    "CANDLE_HISTORY":       (50,   500),
    "ATHENA_TIMEOUT_SECONDS": (10,  120),
}


def load_settings() -> dict:
    p = Path(SETTINGS_PATH)
    if not p.exists():
        p.write_text(json.dumps(DEFAULTS, indent=2))
        return dict(DEFAULTS)
    data = json.loads(p.read_text())
    # Fill in any missing keys from defaults
    for k, v in DEFAULTS.items():
        data.setdefault(k, v)
    return data


def save_settings(settings: dict) -> None:
    Path(SETTINGS_PATH).write_text(json.dumps(settings, indent=2))


def validate_setting(key: str, value):
    """Raise ValueError if value is out of safety bounds. Returns coerced value."""
    if key not in DEFAULTS:
        raise ValueError(f"Unknown setting: {key}")
    default = DEFAULTS[key]
    # Coerce to default's type
    try:
        if isinstance(default, bool):
            if isinstance(value, str):
                value = value.lower() in ("true", "1", "yes", "on")
            else:
                value = bool(value)
        elif isinstance(default, int):
            value = int(value)
        elif isinstance(default, float):
            value = float(value)
        else:
            value = str(value)
    except (ValueError, TypeError):
        raise ValueError(f"{key}: cannot coerce {value!r} to {type(default).__name__}")

    if key in BOUNDS:
        lo, hi = BOUNDS[key]
        if not (lo <= value <= hi):
            raise ValueError(f"{key}={value} out of safe bounds [{lo}, {hi}]")
    return value


# Convenience: provide module-level attributes that read live from settings.json each access.
# Old code that uses `config.RISK_PER_TRADE_PCT` keeps working.
def __getattr__(name):
    if name in DEFAULTS:
        return load_settings()[name]
    raise AttributeError(name)
