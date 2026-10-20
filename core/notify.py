"""Plain Telegram notifications for scripts and timers that run outside the bot process."""
import logging

import requests

logger = logging.getLogger("AI_COACH_V1.notify")


def notify_telegram(env: dict, text: str):
    token = env.get("TELEGRAM_BOT_TOKEN")
    ids = [x for x in env.get("TELEGRAM_ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x]
    if not token or not ids:
        logger.warning("Telegram not configured - skipping notification.")
        return
    for chat_id in ids:
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text[:4000]},
                timeout=30,
            )
            resp.raise_for_status()
        except Exception as e:
            logger.error(f"Telegram notification to {chat_id} failed: {e}")
