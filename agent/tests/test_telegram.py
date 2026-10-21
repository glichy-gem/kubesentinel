from datetime import timedelta

import pytest
from test_executor import new_action

from app.db import iso
from app.remediation import token_hash
from app.telegram import Approvals, Notifier, Poller, buttons, render_card

CHAT = "424242"


class FakeTelegram:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def call(self, method: str, **params) -> dict:
        self.calls.append((method, params))
        return {"message_id": 99} if method == "sendMessage" else {}


def proposed(db, token="unit-test-token", expires_in=timedelta(minutes=15)):
    from datetime import UTC, datetime

    return new_action(
        db,
        status="proposed",
        mode="approval",
        decided_by=None,
        token_hash=token_hash(token),
        expires_at=iso(datetime.now(UTC) + expires_in),
    )


def test_callback_data_fits_telegrams_64_byte_limit():
    for row in buttons(9_999_999, "x" * 16)["inline_keyboard"]:
        for button in row:
            assert len(button["callback_data"].encode()) <= 64


def test_card_escapes_untrusted_text(db):
    action_id = new_action(
        db, description="<b>sneaky</b>", mode="approval", reason="medium risk"
    )
    incident = db.get_incident(1)
    incident["evidence"] = [
        {"fact_id": "E1", "source": "logs", "content": '<a href="x">click</a>'}
    ]
    incident["diagnosis"] = {
        "model": "m",
        "diagnosis": {
            "root_cause": "unknown",
            "confidence": 0.5,
            "summary": "<i>s</i>",
            "evidence": ["E1"],
        },
    }
    card = render_card(incident, db.get_action(action_id))
    assert "<a href" not in card and "&lt;a href" in card
    assert "&lt;b&gt;sneaky&lt;/b&gt;" in card and "&lt;i&gt;s&lt;/i&gt;" in card


def test_approve_once_runs_once(db):
    action_id = proposed(db)
    executed = []
    approvals = Approvals(db, executed.append, None)
    assert approvals.decide(action_id, "unit-test-token", True, "Shivam").startswith(
        "Approved"
    )
    assert approvals.decide(action_id, "unit-test-token", True, "Shivam").startswith(
        "Not applied"
    )
    assert executed == [action_id]
    action = db.get_action(action_id)
    assert (action["status"], action["decided_by"]) == ("approved", "Shivam")


def test_wrong_token_rejected_and_reject_changes_nothing(db):
    action_id = proposed(db)
    executed = []
    approvals = Approvals(db, executed.append, None)
    assert approvals.decide(action_id, "guess-guess-guess", True, "x").startswith(
        "Not applied"
    )
    assert approvals.decide(action_id, "unit-test-token", False, "Shivam").startswith(
        "Rejected"
    )
    assert executed == [] and db.get_action(action_id)["status"] == "rejected"


def test_expired_proposals_cannot_be_approved_and_cards_are_closed(db):
    action_id = proposed(db, expires_in=timedelta(seconds=-1))
    notifier = Notifier(FakeTelegram(), CHAT, db)
    db.set_action_message(action_id, 7)
    approvals = Approvals(db, lambda _: None, notifier)
    assert approvals.decide(action_id, "unit-test-token", True, "x").startswith(
        "Not applied"
    )
    approvals.expire()
    assert db.get_action(action_id)["status"] == "expired"
    method, params = notifier.client.calls[-1]
    assert method == "editMessageText" and "Expired" in params["text"]


@pytest.mark.parametrize(
    ("sender", "chat", "data", "answer"),
    [
        (
            "1",
            CHAT,
            "ok:1:unit-test-token",
            "Not authorised.",
        ),  # someone else pressing
        (
            CHAT,
            "1",
            "ok:1:unit-test-token",
            "Not authorised.",
        ),  # the bot added to another chat
        (CHAT, CHAT, "rm -rf:1:x", "Malformed button."),
        (CHAT, CHAT, "garbage", "Malformed button."),
        (CHAT, CHAT, "ok:1:unit-test-token", "Approved. Running action #1."),
    ],
)
def test_button_presses_are_authorised_by_chat_and_token(
    db, sender, chat, data, answer
):
    proposed(db)
    executed = []
    client = FakeTelegram()
    poller = Poller(client, CHAT, Approvals(db, executed.append, None))
    query = {
        "id": "q1",
        "from": {"id": int(sender), "first_name": "S"},
        "message": {"chat": {"id": int(chat)}},
        "data": data,
    }
    assert poller.handle(query) == answer
    assert client.calls[-1][0] == "answerCallbackQuery"
    assert executed == ([1] if answer.startswith("Approved") else [])


def test_decided_card_no_longer_asks_for_approval(db):
    # Found live: the closed card still said "Needs your approval · expires".
    action_id = proposed(db)
    client = FakeTelegram()
    notifier = Notifier(client, CHAT, db)
    db.set_action_message(action_id, 7)
    Approvals(db, lambda _: None, notifier).decide(
        action_id, "unit-test-token", True, "Heyo"
    )
    text = client.calls[-1][1]["text"]
    assert "Needs your approval" not in text
    assert text.endswith("UTC") and "Approved by Heyo" in text


def test_first_card_becomes_the_incident_thread(db):
    action_id = proposed(db)
    client = FakeTelegram()
    notifier = Notifier(client, CHAT, db)
    notifier.incident_card(
        db.get_incident(1), db.get_action(action_id), "unit-test-token"
    )
    method, params = client.calls[0]
    assert method == "sendMessage" and params["parse_mode"] == "HTML"
    assert (
        params["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        == f"ok:{action_id}:unit-test-token"
    )
    assert db.get_incident(1)["telegram_message_id"] == 99
    assert db.get_action(action_id)["telegram_message_id"] == 99
