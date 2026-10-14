"""Background-required admission never turns into an inline child run."""
from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tools import delegate_tool_dispatch as dispatch
from tools.delegate_tool_child_run import _attach_child
from tools.delegation_live_log import create_live_transcripts


def make_batch(tmp_path, n=1):
    parent = SimpleNamespace(session_id="admission-parent", _delegate_depth=0,
                             _interrupt_requested=False, quiet_mode=True,
                             _active_children=[], _active_children_lock=threading.Lock())
    tasks = [{"goal": f"bounded task {i}"} for i in range(n)]
    children = []
    for i, task in enumerate(tasks):
        child = SimpleNamespace(_subagent_id=f"child-{i}", _delegate_role="leaf",
                                close=Mock(), hard_interrupt=Mock())
        _attach_child(parent, child)
        children.append((i, task, child))
    deleg_id, writers, paths = create_live_transcripts(tasks, home=tmp_path)
    return dispatch._Batch(tasks, children, parent, {"model": "test-model"}, None, "leaf", n,
                           deleg_id, writers, paths, "", "", None, None, False,
                           time.monotonic(), live_home=tmp_path)


def test_background_required_without_consumer_never_executes_inline(tmp_path, monkeypatch):
    batch = make_batch(tmp_path)
    monkeypatch.setattr(dispatch, "_resolve_async_wake_sid", lambda *_: None)
    inline = Mock(return_value={"results": [{"task_index": 0, "status": "completed"}]})
    monkeypatch.setattr(dispatch, "_execute_and_aggregate", inline)

    result = json.loads(dispatch._run_batch(batch, background=True))

    assert inline.call_count == 0, "background-required ran synchronously without a consumer"
    assert result["status"] == "unsupported"
    assert result["count"] == 0
    assert result["rejected_task_indexes"] == [0]
    assert result["rejected_tasks"][0]["start_state"] == "not_started"
    assert batch.parent_agent._active_children == []
    batch.children[0][2].close.assert_called_once()
    manifest = json.loads((tmp_path / "cache/delegation/live" / batch.live_deleg_id / "manifest.json").read_text())
    assert manifest["tasks"][0]["status"] == "unsupported"
    assert "end status=unsupported" in batch.live_writers[0].path.read_text()


@pytest.mark.parametrize("accepted", [0, 1])
def test_rejected_background_units_never_run_inline(tmp_path, monkeypatch, accepted):
    batch = make_batch(tmp_path, 3)
    monkeypatch.setattr(dispatch, "_resolve_async_wake_sid", lambda *_: "")
    monkeypatch.setattr("tools.delegate_tool_config._get_independent_completions", lambda: True)
    admissions = []

    def admit(unit, uid, slot, routing):
        admissions.append([i for i, _, _ in unit.children])
        if len(admissions) <= accepted:
            return {"status": "dispatched", "delegation_id": uid}
        return {"status": "rejected", "error": "Async delegation capacity reached"}

    monkeypatch.setattr(dispatch, "_dispatch_unit", admit)
    inline = Mock(return_value={"results": [{"task_index": 1, "status": "completed"}]})
    monkeypatch.setattr(dispatch, "_execute_and_aggregate", inline)
    result = json.loads(dispatch._run_batch(batch, background=True))
    assert inline.call_count == 0, "rejected background work ran inline"
    assert len(admissions) == accepted + 1, "must stop submitting after rejection"
    assert result["status"] == ("partially_dispatched" if accepted else "busy")
    assert result["count"] == accepted
    assert result["accepted_task_indexes"] == list(range(accepted))
    assert result["rejected_task_indexes"] == list(range(accepted, 3))
    for _, _, child in batch.children[accepted:]:
        child.close.assert_called_once()
    if accepted:
        assert result["subagent_ids"] == ["child-0"]
        assert result["live_transcripts"] == [str(batch.live_writers[0].path)]
        assert result["units"][0]["task_indexes"] == [0]
        assert batch.live_deleg_id is not None
        assert result["units"][0]["delegation_id"] == batch.live_deleg_id + "-1"
        batch.children[0][2].close.assert_not_called()
    assert not batch.parent_agent._active_children


def test_explicit_foreground_still_joins_without_consumer(tmp_path, monkeypatch):
    batch = make_batch(tmp_path)
    monkeypatch.setattr(dispatch, "_resolve_async_wake_sid", lambda *_: None)
    inline = Mock(return_value={"results": [{"task_index": 0, "status": "completed"}]})
    monkeypatch.setattr(dispatch, "_execute_and_aggregate", inline)
    result = json.loads(dispatch._run_batch(batch, background=False))
    assert result["results"][0]["status"] == "completed"
    inline.assert_called_once_with(batch)


@pytest.mark.parametrize("runtime", ["single_query", "cron", "kanban", "subagent"])
def test_real_model_caller_preserves_declared_finite_join(harness, monkeypatch, runtime):
    from gateway import session_context as sc
    from run_agent import AIAgent
    from tests.tools.test_finite_delegation_outcomes import _Child
    parent, children, _ = harness
    if runtime != "single_query":
        monkeypatch.delenv("HERMES_SINGLE_QUERY_SESSION")
        if runtime == "cron":
            sc.set_session_vars(cron_session="1", async_delivery=False)
        elif runtime == "kanban":
            monkeypatch.setenv("HERMES_KANBAN_TASK", "finite-worker")
        else:
            parent._delegate_depth = 1
            monkeypatch.setattr("tools.delegate_tool._get_max_spawn_depth", lambda: 2)
            sc.set_session_vars(async_delivery=False)
    children.append(_Child())
    result = json.loads(AIAgent._dispatch_delegate_task(parent, {"tasks": [{"goal": "finite join"}]}))
    assert result["results"][0]["status"] == "completed"
    assert children[0].closed.is_set()


# Reuse the synthetic child boundary, not the dispatcher, for real-caller coverage.
from tests.tools.test_finite_delegation_outcomes import harness as harness


def test_admission_exception_preserves_unknown_start_without_resubmit(tmp_path, monkeypatch):
    batch = make_batch(tmp_path, 2)
    monkeypatch.setattr(dispatch, "_resolve_async_wake_sid", lambda *_: "")
    monkeypatch.setattr("tools.delegate_tool_config._get_independent_completions", lambda: True)
    attempted = []

    def ambiguous_admission(unit, uid, slot, routing):
        attempted.append(uid)
        raise RuntimeError("failure after possible worker handoff")

    monkeypatch.setattr(dispatch, "_dispatch_unit", ambiguous_admission)
    result = json.loads(dispatch._run_batch(batch, background=True))
    assert result["status"] == "admission_unknown"
    assert result["count"] == 0
    assert result["unknown_tasks"][0]["start_state"] == "unknown"
    assert result["unknown_tasks"][0]["delegation_id"] == attempted[0]
    assert result["rejected_task_indexes"] == [1]
    assert len(attempted) == 1
    uncertain = batch.children[0][2]
    uncertain.close.assert_not_called()  # May own an in-flight transport.
    uncertain.hard_interrupt.assert_called_once()
    assert batch.parent_agent._active_children == [uncertain]
    batch.children[1][2].close.assert_called_once()
    from hermes_cli.observability import shared_metrics_loop as metrics
    assert id(batch.task_list) not in metrics._DELEGATIONS


@pytest.mark.parametrize("finished_before_error", [False, True])
@pytest.mark.parametrize("admission_result", ["raise", "rejected"])
def test_real_worker_handoff_exception_retains_cleanup_ownership(registry_state, monkeypatch, finished_before_error, admission_result):
    from tests.tools.test_delegate_capacity_interrupt import _Parent, _ControlledChild, _batch
    from tools import async_delegation
    from hermes_cli.observability import shared_metrics_loop as metrics
    parent, child = _Parent(), _ControlledChild()
    batch = _batch(parent, child)
    original = dispatch._dispatch_unit
    accepted_ids = []
    metric_calls = Mock(wraps=dispatch.finish_delegation_unit)
    monkeypatch.setattr(dispatch, "finish_delegation_unit", metric_calls)

    def handed_off_then_raised(unit, uid, slot, routing):
        accepted = original(unit, uid, slot, routing)
        accepted_ids.append(accepted["delegation_id"])
        assert child.started.wait(5)
        if finished_before_error:
            child.hard_interrupt("finish before admission exception")
            child.allow_finish.set()
            completion = registry_state.get(timeout=5)
            assert completion["delegation_id"] == uid
        if admission_result == "rejected":
            return {"status": "rejected", "error": "exception after real handoff"}
        raise RuntimeError("exception after real handoff")

    monkeypatch.setattr(dispatch, "_dispatch_unit", handed_off_then_raised)
    try:
        result = json.loads(dispatch._run_batch(batch, background=True))
        assert result["status"] == "admission_unknown"
        assert result["unknown_tasks"][0]["delegation_id"] == accepted_ids[0]
        assert result["unknown_tasks"][0]["start_state"] == "unknown"
        if not finished_before_error:
            assert child.unwinding.wait(5)
            assert child.close_count == 0, "must not close a running transport on admission exception"
            child.allow_finish.set()
            completion = registry_state.get(timeout=5)
            assert completion["results"][0]["status"] == "interrupted"
        assert child.closed.wait(5)
        assert child.close_count == 1
        assert not child.closed_while_running
        assert not parent._active_children
        assert metric_calls.call_count == 1, "unknown admission and worker accounted twice"
        assert id(batch.task_list) not in metrics._DELEGATIONS
        durable = async_delegation.get_durable_delegation(accepted_ids[0])
        assert durable is not None
        assert durable["result"]["results"][0]["status"] == "interrupted"
    finally:
        child.hard_interrupt("test cleanup")
        child.allow_finish.set()
        assert child.closed.wait(5)


from tests.tools.test_delegate_capacity_interrupt import registry_state as registry_state

def test_capability_lookup_failure_is_unsupported(monkeypatch):
    def unavailable():
        raise RuntimeError("capability binding unavailable")
    monkeypatch.setattr("gateway.session_context.async_delivery_supported", unavailable)
    assert dispatch._resolve_async_wake_sid("", False) is None


def test_rejected_queued_runner_cannot_start_after_children_close(tmp_path, monkeypatch):
    batch = make_batch(tmp_path)
    monkeypatch.setattr(dispatch, "_resolve_async_wake_sid", lambda *_: "")
    queued = []
    def rejected_after_queue(**kwargs):
        queued.append(kwargs["runner"])
        return {"status": "rejected", "error": "submit failed after queueing"}
    monkeypatch.setattr("tools.async_delegation.dispatch_async_delegation_batch", rejected_after_queue)
    inline = Mock()
    monkeypatch.setattr(dispatch, "_execute_and_aggregate", inline)
    result = json.loads(dispatch._run_batch(batch, background=True))
    assert result["status"] == "busy"
    assert result["rejected_tasks"][0]["start_state"] == "not_started"
    batch.children[0][2].close.assert_called_once()
    assert queued[0]()["results"][0]["status"] == "busy"
    inline.assert_not_called()


def test_partial_payload_indexes_sparse_transcripts_by_writer(tmp_path, monkeypatch):
    batch = make_batch(tmp_path, 3)
    batch.live_writers[0] = None
    batch.live_paths = batch.live_paths[1:]
    monkeypatch.setattr(dispatch, "_resolve_async_wake_sid", lambda *_: "")
    monkeypatch.setattr("tools.delegate_tool_config._get_independent_completions", lambda: True)
    def admit(unit, uid, *_):
        return ({"status": "dispatched", "delegation_id": uid} if unit.children[0][0] < 2
                else {"status": "rejected", "error": "capacity reached"})
    monkeypatch.setattr(dispatch, "_dispatch_unit", admit)
    result = json.loads(dispatch._run_batch(batch, background=True))
    assert result["count"] == 2
    assert result["accepted_task_indexes"] == [0, 1]
    assert result["live_transcripts"] == [str(batch.live_writers[1].path)]
    assert result["rejected_tasks"][0]["live_transcript"] == str(batch.live_writers[2].path)
