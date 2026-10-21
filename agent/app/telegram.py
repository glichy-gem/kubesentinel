"""Telegram incident cards with Approve / Reject (design doc section 6.7).

The agent long-polls Telegram (getUpdates) instead of receiving a webhook, so the
cluster needs no public URL. A button press is accepted only if it comes from the
configured chat, carries the action's one-time token, and the action has not
expired or been superseded; the database transition makes double taps harmless.
"""

import html
import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime

import httpx

from app.db import Database
from app.remediation import token_hash

logger = logging.getLogger("kubesentinel.telegram")

SEVERITY_ICON = {"critical": "🔴", "warning": "🟠", "info": "🔵"}
MODE_LINE = {
    "auto": "🤖 <b>Running automatically</b> ({reason})",
    "approval": "⏳ <b>Needs your approval</b> ({reason}) · expires {expires} UTC",
    "blocked": "⛔ <b>Blocked by policy:</b> {reason}",
    "none": "ℹ️ No action proposed",
}


def esc(text: object) -> str:
    return html.escape(str(text), quote=False)


class TelegramClient:
    def __init__(self, token: str, timeout: float = 35) -> None:
        self._client = httpx.Client(
            base_url=f"https://api.telegram.org/bot{token}", timeout=timeout
        )

    def call(self, method: str, **params) -> dict:
        response = self._client.post(f"/{method}", json=params)
        data = response.json()
        if not data.get("ok"):
            # The URL holds the token: report the method and Telegram's description only.
            raise RuntimeError(f"telegram {method} failed: {data.get('description')}")
        return data["result"]


def buttons(action_id: int, token: str) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Approve", "callback_data": f"ok:{action_id}:{token}"},
                {"text": "❌ Reject", "callback_data": f"no:{action_id}:{token}"},
            ]
        ]
    }


def render_card(incident: dict, action: dict) -> str:
    diagnosis = (incident.get("diagnosis") or {}).get("diagnosis", {})
    meta = incident.get("diagnosis") or {}
    facts = {e["fact_id"]: e for e in incident.get("evidence", [])}
    firing = sorted(
        {a["name"] for a in incident.get("alerts", []) if a["status"] == "firing"}
    )
    lines = [
        (
            f"{SEVERITY_ICON.get(incident['severity'], '⚪')} <b>Incident #{incident['id']} · "
            f"{esc(incident['service'])}</b> ({esc(incident['namespace'])})"
        ),
        (
            f"{esc(', '.join(firing) or 'resolved')} · {esc(incident['severity'])} · "
            f"{incident['raw_alert_count']} alert(s)"
        ),
        "",
        (
            f"<b>Root cause:</b> {esc(diagnosis.get('root_cause', 'unknown'))} · "
            f"{diagnosis.get('confidence', 0):.0%} · "
            f"{esc(meta.get('model') or 'rule matcher')}"
        ),
        esc(diagnosis.get("summary", "")),
    ]
    cited = [facts[e] for e in diagnosis.get("evidence", []) if e in facts][:3]
    if cited:
        lines += ["", "<b>Evidence</b>"]
        lines += [
            f"• {esc(f['fact_id'])} [{esc(f['source'])}] {esc(f['content'][:160])}"
            for f in cited
        ]
    lines += [
        "",
        f"<b>Proposed fix:</b> {esc(action['type'])} {esc(action['target'])}",
    ]
    if action["mode"] != "none":
        lines.append(f"<code>{esc(action['description'])}</code>")
    # Once decided, the card's footer (added by close_card) says what happened instead.
    if not (action["mode"] == "approval" and action["status"] != "proposed"):
        lines.append(
            MODE_LINE[action["mode"]].format(
                reason=esc(action["reason"]),
                expires=(action.get("expires_at") or "")[11:16],
            )
        )
    return "\n".join(lines)


def render_follow_up(incident: dict, action: dict) -> str:
    head = f"Incident #{incident['id']} · action #{action['id']} ({esc(action['type'])} {esc(action['target'])})"
    status = action["status"]
    if status == "executed":
        return f"▶️ {head}\n{esc(action['result'])}\nChecking that the service recovers…"
    if status == "recovered":
        return f"✅ {head}\nRecovered: {esc(action['verification'])}"
    if status == "not_recovered":
        return (
            f"⚠️ {head}\nDid not recover: {esc(action['verification'])}\n"
            f"Automatic actions for {esc(action['service'])} are paused; a human should look."
        )
    if status == "failed":
        return f"❌ {head}\nFailed: {esc(action['result'])}"
    return f"{head}: {esc(status)}"


class Notifier:
    def __init__(self, client: TelegramClient, chat_id: str, db: Database) -> None:
        self.client = client
        self.chat_id = chat_id
        self.db = db

    def _send(self, text: str, **extra) -> int | None:
        try:
            result = self.client.call(
                "sendMessage",
                chat_id=self.chat_id,
                text=text,
                parse_mode="HTML",
                link_preview_options={"is_disabled": True},
                **extra,
            )
            return result["message_id"]
        except Exception as exc:  # noqa: BLE001 - a notification failure must not stop remediation
            logger.warning(
                "telegram send failed", extra={"fields": {"error": str(exc)}}
            )
            return None

    def incident_card(self, incident: dict, action: dict, token: str | None) -> None:
        extra = {}
        if token:
            extra["reply_markup"] = buttons(action["id"], token)
        if incident.get("telegram_message_id"):
            # Later diagnoses of the same incident thread under its first card.
            extra["reply_parameters"] = {
                "message_id": incident["telegram_message_id"],
                "allow_sending_without_reply": True,
            }
        message_id = self._send(render_card(incident, action), **extra)
        if message_id:
            self.db.set_action_message(action["id"], message_id)
            if not incident.get("telegram_message_id"):
                self.db.set_telegram_message(incident["id"], message_id)

    def follow_up(self, incident: dict, action: dict) -> None:
        extra = {}
        if action.get("telegram_message_id"):
            extra["reply_parameters"] = {
                "message_id": action["telegram_message_id"],
                "allow_sending_without_reply": True,
            }
        self._send(render_follow_up(incident, action), **extra)

    def close_card(self, action: dict, note: str) -> None:
        """Remove the buttons from an action's card and append what happened."""
        if not action.get("telegram_message_id"):
            return
        incident = self.db.get_incident(action["incident_id"])
        try:
            self.client.call(
                "editMessageText",
                chat_id=self.chat_id,
                message_id=action["telegram_message_id"],
                text=f"{render_card(incident, action)}\n\n{esc(note)}",
                parse_mode="HTML",
                link_preview_options={"is_disabled": True},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "telegram edit failed", extra={"fields": {"error": str(exc)}}
            )


class Approvals:
    """The one place where a human decision turns into (no) action."""

    def __init__(
        self, db: Database, execute: Callable[[int], object], notifier: Notifier | None
    ) -> None:
        self.db = db
        self.execute = execute
        self.notifier = notifier

    def decide(self, action_id: int, token: str, approve: bool, by: str) -> str:
        now = datetime.now(UTC)
        if not self.db.decide_action(action_id, token_hash(token), approve, by, now):
            action = self.db.get_action(action_id)
            state = action["status"] if action else "unknown"
            return (
                f"Not applied: action #{action_id} is {state} (or the link is invalid)."
            )
        action = self.db.get_action(action_id)
        if self.notifier:
            verb = "Approved" if approve else "Rejected"
            self.notifier.close_card(action, f"{verb} by {by} at {now:%H:%M} UTC")
        if approve:
            self.execute(action_id)
            return f"Approved. Running action #{action_id}."
        return f"Rejected. Nothing was changed for action #{action_id}."

    def expire(self) -> None:
        for action_id in self.db.expire_actions(datetime.now(UTC)):
            action = self.db.get_action(action_id)
            if self.notifier and action:
                self.notifier.close_card(
                    action, "⌛ Expired without a decision; nothing was changed."
                )


class Poller(threading.Thread):
    """Long-polls Telegram for button presses from the configured chat."""

    def __init__(
        self, client: TelegramClient, chat_id: str, approvals: Approvals
    ) -> None:
        super().__init__(name="telegram-poller", daemon=True)
        self.client = client
        self.chat_id = str(chat_id)
        self.approvals = approvals
        self.offset = 0
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.approvals.expire()
                updates = self.client.call(
                    "getUpdates",
                    offset=self.offset,
                    timeout=25,
                    allowed_updates=["callback_query"],
                )
                for update in updates:
                    self.offset = update["update_id"] + 1
                    if "callback_query" in update:
                        self.handle(update["callback_query"])
            except Exception as exc:  # noqa: BLE001 - keep polling through network blips
                logger.warning(
                    "telegram poll failed", extra={"fields": {"error": str(exc)}}
                )
                time.sleep(5)

    def handle(self, query: dict) -> str:
        sender = query.get("from", {})
        chat = query.get("message", {}).get("chat", {})
        if str(sender.get("id")) != self.chat_id or str(chat.get("id")) != self.chat_id:
            logger.warning(
                "button press from an unknown user ignored",
                extra={"fields": {"from": sender.get("id")}},
            )
            answer = "Not authorised."
        else:
            try:
                verb, action_id, token = query.get("data", "").split(":", 2)
                if verb not in ("ok", "no"):
                    raise ValueError(verb)
                who = sender.get("first_name", "someone")
                if sender.get("username"):
                    who += f" (@{sender['username']})"
                answer = self.approvals.decide(int(action_id), token, verb == "ok", who)
            except ValueError:
                answer = "Malformed button."
        try:
            self.client.call(
                "answerCallbackQuery", callback_query_id=query["id"], text=answer[:190]
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "telegram answer failed", extra={"fields": {"error": str(exc)}}
            )
        return answer
