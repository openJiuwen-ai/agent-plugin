"""UI readiness reaches terminal output before a long agent turn finishes."""
import asyncio
import re
import socket
import threading
from types import SimpleNamespace

import pytest
from openjiuwen.core.foundation.llm import AssistantMessage

import ojw_evaluation.rail as rail_module
from ojw_evaluation.rail import EvaluationRail
from ojw_evaluation.runtime import ensure_service, running_service
from test_dialogues import dialogue_agent


def announced_urls(caplog):
    return re.findall(r"http://127\.0\.0\.1:\d+/[^\s]*", caplog.text)


async def wait_until(predicate):
    async with asyncio.timeout(20):
        while not predicate():
            await asyncio.sleep(.02)


def manual_rail(tmp_path, session="first", **configuration):
    rail = EvaluationRail().configure(output_dir=tmp_path, port=0, **configuration)
    agent = SimpleNamespace(card=SimpleNamespace(id=session, name=session))
    rail.init(agent)
    ctx = SimpleNamespace(inputs={"query": "work"},
                          session=SimpleNamespace(get_session_id=lambda: session,
                                                  get_state=lambda key: None,
                                                  update_state=lambda state: None))
    return rail, agent, ctx


def stop_service(tmp_path):
    handle = running_service(tmp_path / "evaluation_ui")
    if handle:
        handle.api("/api/shutdown", {})


def test_first_turn_runs_while_ui_starts_and_announces_before_answer(
        tmp_path, dialogue_agent, monkeypatch, caplog):
    agent, rail, opened = dialogue_agent
    startup_entered, allow_startup = threading.Event(), threading.Event()

    def delayed_start(*args, **kwargs):
        startup_entered.set()
        if not allow_startup.wait(20):
            raise TimeoutError("Test did not release startup")
        return ensure_service(*args, **kwargs)

    monkeypatch.setattr(rail_module, "ensure_service", delayed_start)

    async def run():
        model_entered, finish_answer = asyncio.Event(), asyncio.Event()

        class SlowModel:
            async def invoke(self, **kwargs):
                model_entered.set()
                await finish_answer.wait()
                return AssistantMessage(content="Finished", finish_reason="stop")

        agent.set_llm(SlowModel())
        turn = asyncio.create_task(agent.invoke({"query": "work", "conversation_id": "early"}))
        try:
            await wait_until(model_entered.is_set)
            await wait_until(startup_entered.is_set)
            assert not turn.done(), "UI startup must not block model execution"
            assert not announced_urls(caplog), "Do not announce readiness before health succeeds"
            allow_startup.set()
            await wait_until(lambda: bool(announced_urls(caplog)))
            handle = await asyncio.to_thread(running_service, tmp_path / "evaluation_ui")
            assert handle is not None and handle.port > 0
            assert announced_urls(caplog) == [f"http://127.0.0.1:{handle.port}/"]
            assert handle.token not in caplog.text
            assert not turn.done(), "Announcement must not wait for the agent answer"
            assert opened == []
        finally:
            allow_startup.set()
            finish_answer.set()
            await turn
    asyncio.run(run())


def test_evaluate_reannounces_existing_service_without_an_extra_turn(
        tmp_path, dialogue_agent, caplog):
    agent, rail, _ = dialogue_agent
    asyncio.run(agent.invoke({"query": "work", "conversation_id": "same"}))
    handle = running_service(tmp_path / "evaluation_ui")
    receipts = set((tmp_path / "evaluation_ui/invocations").glob("*.json"))
    for _ in range(2):
        caplog.clear()
        result = asyncio.run(agent.invoke({"query": "/evaluate", "conversation_id": "same"}))
        assert result["result_type"] == "answer"
        assert announced_urls(caplog) == [f"http://127.0.0.1:{handle.port}/"]
        assert handle.token not in caplog.text
        assert set((tmp_path / "evaluation_ui/invocations").glob("*.json")) == receipts


def test_failed_background_start_does_not_break_answer_and_next_turn_retries(
        tmp_path, dialogue_agent, monkeypatch, caplog):
    agent, rail, _ = dialogue_agent

    def unavailable(*args, **kwargs):
        raise OSError("Private diagnostic detail")

    monkeypatch.setattr(rail_module, "ensure_service", unavailable)
    result = asyncio.run(agent.invoke({"query": "first", "conversation_id": "same"}))
    assert result["output"] == "Answer: first"
    assert rail.last_error and "OSError" in rail.last_error
    assert not announced_urls(caplog)
    assert "Private diagnostic detail" not in caplog.text
    monkeypatch.setattr(rail_module, "ensure_service", ensure_service)
    result = asyncio.run(agent.invoke({"query": "second", "conversation_id": "same"}))
    assert result["output"] == "Answer: second"
    assert rail.last_error is None
    handle = running_service(tmp_path / "evaluation_ui")
    assert announced_urls(caplog) == [f"http://127.0.0.1:{handle.port}/"]


def test_busy_requested_port_announces_the_actual_listener(tmp_path, caplog):
    from ojw_evaluation.dialogues import register_dialogue
    from ojw_evaluation.opening import open_saved_dialogue
    root = tmp_path / "evaluation_ui"
    context = register_dialogue(root, "occupied", launch_dir=str(tmp_path))
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        port = occupied.getsockname()[1]
        try:
            opened = open_saved_dialogue(context["metadata_path"], port=port)
            assert opened["success"]
            handle = running_service(root)
            assert handle.port != port
            assert announced_urls(caplog) == [f"http://127.0.0.1:{handle.port}/"]
            assert handle.token not in caplog.text
        finally:
            stop_service(tmp_path)


def test_cancelled_waiter_does_not_cancel_shared_startup(tmp_path, monkeypatch, caplog):
    rail, agent, first = manual_rail(tmp_path)
    second = SimpleNamespace(inputs={"query": "more"}, session=first.session)
    entered, release = threading.Event(), threading.Event()

    def delayed_start(*args, **kwargs):
        entered.set()
        if not release.wait(20):
            raise TimeoutError("Test did not release startup")
        return ensure_service(*args, **kwargs)

    monkeypatch.setattr(rail_module, "ensure_service", delayed_start)

    async def run():
        waits = []
        try:
            await rail.before_invoke(first)
            await rail.before_invoke(second)
            await wait_until(entered.is_set)
            waits = [asyncio.create_task(rail.after_invoke(ctx)) for ctx in (first, second)]
            await asyncio.sleep(0)
            waits[0].cancel()
            with pytest.raises(asyncio.CancelledError):
                await waits[0]
            release.set()
            await waits[1]
            handle = await asyncio.to_thread(running_service, tmp_path / "evaluation_ui")
            assert handle is not None
            assert rail.last_error is None
            assert announced_urls(caplog) == [f"http://127.0.0.1:{handle.port}/"]
        finally:
            release.set()
            if waits:
                await asyncio.gather(*waits, return_exceptions=True)
            rail.uninit(agent)
    try:
        asyncio.run(run())
    finally:
        stop_service(tmp_path)


def test_unloading_during_startup_does_not_repopulate_the_rail(tmp_path, monkeypatch):
    rail, agent, ctx = manual_rail(tmp_path)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def delayed_start(*args, **kwargs):
        entered.set()
        try:
            if not release.wait(20):
                raise TimeoutError("Test did not release startup")
            return ensure_service(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(rail_module, "ensure_service", delayed_start)

    async def run():
        try:
            await rail.before_invoke(ctx)
            await wait_until(entered.is_set)
            rail.uninit(agent)
            release.set()
            await wait_until(finished.is_set)
            await asyncio.sleep(0)
            assert rail._services == {}
            assert rail.last_url is None
            assert await asyncio.to_thread(running_service, tmp_path / "evaluation_ui") is not None
        finally:
            release.set()
            rail.uninit(agent)
    try:
        asyncio.run(run())
    finally:
        stop_service(tmp_path)


def test_unload_after_startup_finishes_does_not_publish_or_open_link(tmp_path, monkeypatch):
    rail, agent, ctx = manual_rail(tmp_path, open_browser=True)
    opened = []
    monkeypatch.setattr(rail_module.webbrowser, "open", lambda url: opened.append(url))

    async def run():
        await rail.before_invoke(ctx)
        startup = rail._startups[str(tmp_path / "evaluation_ui")]
        # Register before shield's callback: detach after the worker completes,
        # but before the invocation waiter gets its result.
        startup.add_done_callback(lambda task: rail.uninit(agent))
        await rail.after_invoke(ctx)
        assert rail.last_url is None
        assert opened == []
        assert rail._services == {}
    try:
        asyncio.run(run())
    finally:
        rail.uninit(agent)
        stop_service(tmp_path)


@pytest.mark.parametrize("cancel_invocation", [False, True])
def test_unload_preserves_the_invocations_own_cancellation(tmp_path, monkeypatch, cancel_invocation):
    rail, agent, ctx = manual_rail(tmp_path)
    entered, release = threading.Event(), threading.Event()

    def delayed_start(*args, **kwargs):
        entered.set()
        if not release.wait(20):
            raise TimeoutError("Test did not release startup")
        return ensure_service(*args, **kwargs)

    monkeypatch.setattr(rail_module, "ensure_service", delayed_start)

    async def run():
        waiter = None
        try:
            await rail.before_invoke(ctx)
            await wait_until(entered.is_set)
            waiter = asyncio.create_task(rail.after_invoke(ctx))
            await asyncio.sleep(0)
            if cancel_invocation:
                waiter.cancel()
            rail.uninit(agent)
            release.set()
            result, = await asyncio.gather(waiter, return_exceptions=True)
            if cancel_invocation:
                assert isinstance(result, asyncio.CancelledError)
            else:
                assert result is None
            assert rail.last_url is None
        finally:
            release.set()
            if waiter:
                await asyncio.gather(waiter, return_exceptions=True)
            rail.uninit(agent)
    try:
        asyncio.run(run())
    finally:
        stop_service(tmp_path)


def test_startup_failure_after_unload_is_still_logged(tmp_path, monkeypatch, caplog):
    rail, agent, ctx = manual_rail(tmp_path)
    entered, release = threading.Event(), threading.Event()

    def delayed_failure(*args, **kwargs):
        entered.set()
        if not release.wait(20):
            raise TimeoutError("Test did not release startup")
        raise OSError("Private diagnostic detail")

    monkeypatch.setattr(rail_module, "ensure_service", delayed_failure)

    async def run():
        try:
            await rail.before_invoke(ctx)
            await wait_until(entered.is_set)
            rail.uninit(agent)
            release.set()
            await wait_until(lambda: "OSError" in caplog.text)
            assert "Private diagnostic detail" not in caplog.text
            assert not announced_urls(caplog)
            assert rail.last_error is None and rail._services == {}
        finally:
            release.set()
            rail.uninit(agent)
    asyncio.run(run())


def test_service_stopped_during_turn_is_ready_before_opening_link(tmp_path, monkeypatch):
    rail, agent, ctx = manual_rail(tmp_path, open_browser=True)
    opened = []

    def open_ready_service(url):
        handle = running_service(tmp_path / "evaluation_ui")
        assert handle is not None, "Do not open a link to a service stopped during the turn"
        opened.append(url)

    monkeypatch.setattr(rail_module.webbrowser, "open", open_ready_service)

    async def run():
        try:
            await rail.before_invoke(ctx)
            await wait_until(lambda: bool(rail._services))
            await asyncio.to_thread(stop_service, tmp_path)
            async with asyncio.timeout(20):
                while await asyncio.to_thread(running_service, tmp_path / "evaluation_ui"):
                    await asyncio.sleep(.02)
            await rail.after_invoke(ctx)
            assert opened == [rail.last_url]
            assert rail.last_error is None
        finally:
            rail.uninit(agent)
    try:
        asyncio.run(run())
    finally:
        stop_service(tmp_path)
