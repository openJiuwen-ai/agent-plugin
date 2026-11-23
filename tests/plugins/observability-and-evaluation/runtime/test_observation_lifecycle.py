"""Observation attachment/detachment without model or network calls."""

import asyncio
from functools import wraps
from types import SimpleNamespace

import pytest
from opentelemetry import trace

from ojw_observability import observe, unobserve
from ojw_observability import decorator


class Agent:
    def __init__(self):
        self.card = SimpleNamespace(name="test", id="test")
        self.rails = []

    async def invoke(self, inputs, **kwargs):
        return inputs, kwargs

    async def register_rail(self, rail):
        self.rails.append(rail)

    async def unregister_rail(self, rail):
        self.rails.remove(rail)


@pytest.fixture
def observations(monkeypatch):
    finished = []

    class Collector:
        def __init__(self, agent, inputs):
            self.root = trace.INVALID_SPAN

        def finish(self, result, error, output_dir):
            finished.append((result, error, output_dir))

    monkeypatch.setattr(decorator, "enable_bridge", lambda current: None)
    monkeypatch.setattr(decorator, "_InvocationRail", Collector)
    return finished


def test_detach_restores_method_and_streaming_then_allows_new_settings(tmp_path, observations):
    agent, sibling = Agent(), Agent()
    original = agent.invoke
    assert observe(agent, tmp_path / "first", force_non_streaming=True) is agent
    retained = agent.invoke
    assert observe(agent, tmp_path / "first", force_non_streaming=True).invoke is retained
    assert asyncio.run(agent.invoke("first", _streaming=True))[1] == {"_streaming": False}
    assert sibling.invoke.__func__ is original.__func__
    assert "invoke" not in vars(sibling)

    assert unobserve(agent) is agent
    assert agent.invoke == original
    assert "invoke" not in vars(agent)
    assert unobserve(agent) is agent
    assert asyncio.run(retained("later", _streaming=True))[1] == {"_streaming": True}
    assert len(observations) == 1
    observe(agent, tmp_path / "second")
    assert asyncio.run(agent.invoke("second", _streaming=True))[1] == {"_streaming": True}
    assert [item[2] for item in observations] == [tmp_path / "first", tmp_path / "second"]
    assert agent.rails == []
    unobserve(agent)


def test_detach_restores_existing_instance_override(tmp_path, observations):
    agent = Agent()

    async def custom(inputs, **kwargs):
        return "custom"

    agent.invoke = custom
    observe(agent, tmp_path)
    unobserve(agent)
    assert agent.invoke is custom


def test_detach_does_not_overwrite_a_later_wrapper(tmp_path, observations):
    agent = observe(Agent(), tmp_path)
    observed = agent.invoke

    @wraps(observed)
    async def outer(*args, **kwargs):
        return await observed(*args, **kwargs)

    agent.invoke = outer
    with pytest.raises(ValueError, match="another wrapper"):
        unobserve(agent)
    assert agent.invoke is outer
    agent.invoke = observed
    unobserve(agent)
    assert "invoke" not in vars(agent)


@pytest.mark.parametrize("outcome", ["success", "error", "cancel"])
def test_detach_during_invocation_preserves_cleanup(tmp_path, observations, outcome):
    async def exercise():
        started, resume = asyncio.Event(), asyncio.Event()

        class BlockingAgent(Agent):
            async def invoke(self, inputs, **kwargs):
                started.set()
                await resume.wait()
                if outcome == "error":
                    raise RuntimeError("fixture failure")
                return inputs

        agent = observe(BlockingAgent(), tmp_path, force_non_streaming=True)
        task = asyncio.create_task(agent.invoke("running"))
        await started.wait()
        assert len(agent.rails) == 1
        unobserve(agent)
        if outcome == "cancel":
            task.cancel()
        else:
            resume.set()
        if outcome == "success":
            assert await task == "running"
        else:
            expected = asyncio.CancelledError if outcome == "cancel" else RuntimeError
            with pytest.raises(expected):
                await task
        assert agent.rails == []
        assert "invoke" not in vars(agent)
        assert decorator._active.get() is None
        assert decorator._parent_span.get() is None

    asyncio.run(exercise())
    assert len(observations) == 1
    result, error, directory = observations[0]
    assert directory == tmp_path
    assert (error is None) == (outcome == "success")


def test_unobserve_rejects_class():
    with pytest.raises(TypeError, match="instance"):
        unobserve(Agent)
