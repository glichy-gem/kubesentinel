import json
import logging
import sys
from datetime import UTC, datetime

from app.redact import redact


class JsonFormatter(logging.Formatter):
    """One JSON object per line, easy to query in Loki. Secrets are redacted."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Extra structured fields are passed as logger.info(..., extra={"fields": {...}}).
        payload.update(getattr(record, "fields", {}))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        # Last line of defence: some clients put credentials in URLs (Telegram does).
        return redact(json.dumps(payload, default=str))


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    # httpx logs every request URL at INFO, and the Telegram bot token is part of
    # its URLs (found in the first live run). Our own logs say what matters.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
