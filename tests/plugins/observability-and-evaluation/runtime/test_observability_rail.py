"""The same Rail supports standalone agents and DeepAgent plugin hosts."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from openjiuwen.core.foundation.llm import AssistantMessage
from openjiuwen.core.single_agent import AgentCard
from openjiuwen.core.single_agent.agents.react_agent import ReActAgent, ReActAgentConfig
from openjiuwen.core.sys_operation.cwd import init_cwd
from openjiuwen.harness import DeepAgent, DeepAgentConfig, Workspace
from ojw_observability import ObservabilityRail, observe, unobserve


class Model:
    def __init__(self):
        self.calls = 0

    async def invoke(self, **kwargs):
        self.calls += 1
        return AssistantMessage(content="offline answer", finish_reason="stop")

    async def stream(self, **kwargs):
        raise AssertionError("Model streaming should be disabled")
        yield


def make_agent(name="test"):
    agent = ReActAgent(AgentCard(id=name, name=name))
    agent.configure(ReActAgentConfig(max_iterations=2))
    model = Model()
    agent.set_llm(model)
    return agent, model


def test_react_first_call_stream_and_detach(tmp_path):
    async def run():
        agent, model = make_agent()
        original = agent.invoke
        rail = ObservabilityRail().configure(output_dir=tmp_path)
        await agent.register_rail(rail)
        await agent.invoke({"query": "first", "conversation_id": "first"})
        assert len(list(tmp_path.glob("agent_*.json"))) == 1
        async for _ in agent.stream({"query": "stream", "conversation_id": "stream"}):
            pass
        assert len(list(tmp_path.glob("agent_*.json"))) == 2
        assert model.calls == 2
        await agent.unregister_rail(rail)
        assert agent.invoke == original
        await agent.invoke({"query": "unobserved", "conversation_id": "last"})
        assert len(list(tmp_path.glob("agent_*.json"))) == 2
    asyncio.run(run())


@pytest.mark.parametrize("explicit", [False, True])
def test_deep_output_policy(tmp_path, explicit):
    async def run():
        init_cwd(str(tmp_path), workspace=str(tmp_path))
        agent = DeepAgent(AgentCard(id="deep", name="deep"))
        agent.configure(DeepAgentConfig(workspace=Workspace(root_path=str(tmp_path)),
                                       auto_create_workspace=True, enable_task_loop=False))
        model = Model()
        agent.react_agent.set_llm(model)
        rail = ObservabilityRail()
        if explicit:
            rail.configure(output_dir=tmp_path / "explicit")
        await agent.register_rail(rail)
        await agent.invoke({"query": "first", "conversation_id": "session-one"})
        directory = (tmp_path / "explicit" if explicit else tmp_path / "observability_traces" /
                     "session-one")
        paths = list(directory.glob("agent_*.json"))
        assert len(paths) == 1
        assert json.loads(paths[0].read_text())["result"]["agent_output"] == "offline answer"
        await agent.unregister_rail(rail)
        assert not getattr(agent.react_agent.invoke, "__ojw_observed__", False)
    asyncio.run(run())


def test_configuration_and_agent_binding(tmp_path):
    agent, _ = make_agent()
    rail = ObservabilityRail()
    original = agent.invoke
    with pytest.raises(ValueError, match="output_dir"):
        rail.init(agent)
    assert agent.invoke == original
    rail.configure(output_dir=tmp_path, force_non_streaming=False)
    rail.init(agent)
    assert agent.invoke.__ojw_force_non_streaming__ is False
    with pytest.raises(RuntimeError, match="before registering"):
        rail.configure(output_dir=tmp_path)
    sibling, _ = make_agent("other")
    with pytest.raises(RuntimeError, match="separate"):
        rail.init(sibling)
    with pytest.raises(RuntimeError, match="different agent"):
        rail.uninit(sibling)
    rail.uninit(agent)
    assert agent.invoke == original
    rail.uninit(agent)
    with pytest.raises(TypeError, match="supports"):
        ObservabilityRail().init(SimpleNamespace())


def test_session_trace_directory_survives_workspace_change(tmp_path):
    from openjiuwen.core.sys_operation.cwd import set_workspace
    async def run():
        init_cwd(str(tmp_path), workspace=str(tmp_path))
        agent = DeepAgent(AgentCard(id="deep-moving", name="deep-moving"))
        agent.configure(DeepAgentConfig(workspace=Workspace(root_path=str(tmp_path)),
                                       auto_create_workspace=True, enable_task_loop=False))
        agent.react_agent.set_llm(Model())
        rail = ObservabilityRail()
        rail.init(agent)
        ctx = SimpleNamespace(session=SimpleNamespace(get_session_id=lambda: "moving-session"))
        try:
            await rail.before_invoke(ctx)
            set_workspace(str(tmp_path / "chat-workspace"))
            # Call the hook directly: the SDK logs and swallows callback errors.
            await rail.before_invoke(ctx)
            await agent.react_agent.invoke({"query": "test", "conversation_id": "moving-session"})
            files = list((tmp_path / "observability_traces").glob("*/*.json"))
            assert len(files) == 1
            assert json.loads(files[0].read_text())["result"]["agent_output"] == "offline answer"
        finally:
            rail.uninit(agent)
    asyncio.run(run())


def test_does_not_take_ownership_of_existing_observation(tmp_path):
    agent, _ = make_agent()
    observe(agent, tmp_path, force_non_streaming=True)
    wrapper = agent.invoke
    rail = ObservabilityRail().configure(output_dir=tmp_path)
    with pytest.raises(RuntimeError, match="already observed"):
        rail.init(agent)
    rail.uninit(agent)
    assert agent.invoke is wrapper
    unobserve(agent)


def test_does_not_remove_later_wrapper(tmp_path):
    agent, _ = make_agent()
    original = agent.invoke
    rail = ObservabilityRail().configure(output_dir=tmp_path)
    rail.init(agent)
    owned = agent.invoke

    async def replacement(*args, **kwargs):
        return await owned(*args, **kwargs)

    agent.invoke = replacement
    with pytest.raises(RuntimeError, match="another wrapper"):
        rail.uninit(agent)
    assert agent.invoke is replacement
    agent.invoke = owned
    rail.uninit(agent)
    assert agent.invoke == original
