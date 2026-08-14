"""Configuration from the environment (or a local .env)."""

import os

from dotenv import load_dotenv

load_dotenv()


def _flag(name, default=False):
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# --- Telegram -------------------------------------------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")

# --- Sources --------------------------------------------------------------
# IGDB is authenticated with Twitch app credentials.
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID", "")
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET", "")
RAWG_API_KEY = os.getenv("RAWG_API_KEY", "")

# --- Storage --------------------------------------------------------------
# Supabase or Neon connection string. NOT Render's free Postgres, which
# expires 30 days after creation.
DATABASE_URL = os.getenv("DATABASE_URL", "")

# --- Serving --------------------------------------------------------------
USE_WEBHOOK = _flag("USE_WEBHOOK", False)
PORT = int(os.getenv("PORT", "10000"))
WEBHOOK_BASE_URL = (
    os.getenv("WEBHOOK_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or ""
).rstrip("/")

# Shared secret the GitHub Actions scheduler presents to /tasks/run.
CRON_TOKEN = os.getenv("CRON_TOKEN", "")

# --- Behaviour ------------------------------------------------------------
MAX_TRACKED_PER_CHAT = int(os.getenv("MAX_TRACKED_PER_CHAT", "50"))
MAX_RULES_PER_CHAT = int(os.getenv("MAX_RULES_PER_CHAT", "10"))
DIGEST_WINDOW_DAYS = int(os.getenv("DIGEST_WINDOW_DAYS", "30"))
DISCOVERY_PER_RULE = int(os.getenv("DISCOVERY_PER_RULE", "15"))
DISCOVERY_MAX_MESSAGES = int(os.getenv("DISCOVERY_MAX_MESSAGES", "5"))
# 0 = Monday, to match date.weekday().
DIGEST_WEEKDAY = int(os.getenv("DIGEST_WEEKDAY", "0"))
SEND_DELAY_SECONDS = float(os.getenv("SEND_DELAY_SECONDS", "0.06"))


def validate():
    problems = []
    if not TELEGRAM_TOKEN:
        problems.append("TELEGRAM_TOKEN is not set")
    if not DATABASE_URL:
        problems.append("DATABASE_URL is not set (use Supabase or Neon, not Render free Postgres)")
    if not (TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET) and not RAWG_API_KEY:
        problems.append(
            "No game source configured: set TWITCH_CLIENT_ID + TWITCH_CLIENT_SECRET "
            "(IGDB) and/or RAWG_API_KEY"
        )
    if USE_WEBHOOK and not WEBHOOK_BASE_URL:
        problems.append(
            "USE_WEBHOOK is on but neither WEBHOOK_BASE_URL nor RENDER_EXTERNAL_URL is set"
        )
    if USE_WEBHOOK and WEBHOOK_BASE_URL and not WEBHOOK_BASE_URL.startswith("https://"):
        problems.append("Webhook base URL must be https:// (Telegram requires TLS)")
    if USE_WEBHOOK and not CRON_TOKEN:
        problems.append("CRON_TOKEN is not set - /tasks/run would be unauthenticated")
    return problems
