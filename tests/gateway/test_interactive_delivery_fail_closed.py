"""Offline caller checks: native approval and its text fallback both fail closed."""
import asyncio
from types import SimpleNamespace

import pytest
from gateway.platforms.base import SendResult
from gateway.run_turn_runner import TurnRunner


class Adapter:
    typed_command_prefix = "/"

    def __init__(self, card, text):
        self.card, self.text = card, text
        self.text_attempts = 0

    def pause_typing_for_chat(self, chat_id):
        pass

    async def send_exec_approval(self, **kwargs):
        return self.card

    async def send(self, chat_id, message, **kwargs):
        self.text_attempts += 1
        return self.text


def runner_for(adapter):
    runner = object.__new__(TurnRunner)
    runner._ctx = SimpleNamespace(
        _status_adapter=adapter, _status_chat_id="C1",
        _status_thread_metadata={"thread_id": "1111"}, session_key="sk1",
        source=SimpleNamespace(chat_id="C1", platform="slack", session_key="sk1"),
    )

    class Future:
        def __init__(self, value): self.value = value
        def result(self, timeout=None): return self.value

    runner._schedule = lambda coro, label: Future(asyncio.run(coro))
    runner._close_native_stream_boundary = lambda why: None
    return runner


def test_definitive_card_and_text_failure_raises_to_cancel_central_waiter():
    adapter = Adapter(SendResult(success=False, error="card rejected"),
                      SendResult(success=False, error="channel_not_found"))
    with pytest.raises(RuntimeError, match="undeliverable"):
        runner_for(adapter)._approval_notify_sync({"command": "fixture", "description": "fixture"})
    assert adapter.text_attempts == 1


def test_ambiguous_card_does_not_post_text_fallback():
    adapter = Adapter(SendResult(success=False, raw_response={"ambiguous": True}),
                      SendResult(success=True, message_id="text-ts"))
    runner_for(adapter)._approval_notify_sync({"command": "fixture", "description": "fixture"})
    assert adapter.text_attempts == 0


def test_definitive_failure_removes_central_approval_without_polling(monkeypatch):
    from tools import approval
    from tools.approval_gateway_wait import _await_gateway_decision

    adapter = Adapter(SendResult(success=False, error="card rejected"),
                      SendResult(success=False, error="channel_not_found"))
    runner = runner_for(adapter)
    session_key = runner._ctx.session_key
    assert isinstance(session_key, str)

    def never_poll(*args, **kwargs):
        raise AssertionError("definitive send failure must not enter the human waiter")

    monkeypatch.setattr("tools.approval_gateway_wait._poll_event", never_poll)
    result = _await_gateway_decision(
        session_key, runner._approval_notify_sync,
        {"command": "fixture", "description": "fixture", "pattern_key": "fixture"},
    )
    assert result["notify_failed"] is True
    with approval._lock:
        assert not approval._gateway_queues.get(session_key)


def test_text_fallback_egress_decline_cannot_be_reported_as_delivered():
    from gateway.relay.egress import EGRESS_DECLINE_CODE
    adapter = Adapter(
        SendResult(success=False, error="card rejected"),
        SendResult(success=False, raw_response={"success": False, "code": EGRESS_DECLINE_CODE}),
    )
    with pytest.raises(RuntimeError, match="egress declined"):
        runner_for(adapter)._approval_notify_sync({"command": "fixture", "description": "fixture"})
    assert adapter.text_attempts == 1
