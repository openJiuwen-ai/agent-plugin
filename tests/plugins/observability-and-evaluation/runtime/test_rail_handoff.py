"""Invocation references stay correct across the SDK and collector lifecycles."""
import asyncio
import json
from pathlib import Path

import pytest

from openjiuwen.core.foundation.llm import AssistantMessage
from openjiuwen.core.single_agent import AgentCard
from openjiuwen.core.single_agent.agents.react_agent import ReActAgent, ReActAgentConfig
from openjiuwen.core.sys_operation.cwd import init_cwd
from openjiuwen.harness import DeepAgent, DeepAgentConfig, Workspace
from ojw_evaluation.rail import EvaluationRail
from ojw_observability.rail import ObservabilityRail


class Model:
    def __init__(self):
        self.answer = "outer"

    async def invoke(self, **kwargs):
        return AssistantMessage(content=self.answer, finish_reason="stop")


async def make_agent(tmp_path, *, deep=True):
    init_cwd(str(tmp_path), workspace=str(tmp_path))
    card = AgentCard(id="handoff", name="handoff")
    if deep:
        agent = DeepAgent(card)
        agent.configure(DeepAgentConfig(workspace=Workspace(root_path=str(tmp_path)),
                                       auto_create_workspace=True, enable_task_loop=False))
        inner = agent.react_agent
        observer = ObservabilityRail()
    else:
        agent = inner = ReActAgent(card)
        agent.configure(ReActAgentConfig(max_iterations=2))
        observer = ObservabilityRail().configure(output_dir=tmp_path / "traces")
    model = Model()
    inner.set_llm(model)
    evaluator = EvaluationRail().configure(output_dir=tmp_path, port=0, open_browser=False)
    await agent.register_rail(observer)
    await agent.register_rail(evaluator)
    return agent, inner, model, observer, evaluator


async def cleanup(agent, observer, evaluator):
    for handle in list(evaluator._services.values()):
        handle.api("/api/shutdown", {})
    await agent.unregister_rail(evaluator)
    await agent.unregister_rail(observer)


def test_deep_session_switch_keeps_each_trace_in_its_session(tmp_path):
    async def run():
        agent, inner, model, observer, evaluator = await make_agent(tmp_path)
        try:
            for sid, answer in [("first", "first-1"), ("second", "second-1"), ("first", "first-2")]:
                model.answer = answer
                await agent.invoke({"query": answer, "conversation_id": sid})
            receipts = [json.loads(path.read_text()) for path in (tmp_path / "evaluation_ui/invocations").glob("*.json")]
            assert len(receipts) == 3
            for receipt in receipts:
                trace = Path(receipt["source_path"])
                assert trace.parent == tmp_path / "observability_traces" / receipt["session_id"]
                result = json.loads(trace.read_text())["result"]["agent_output"]
                assert result.startswith(receipt["session_id"] + "-")
        finally:
            await cleanup(agent, observer, evaluator)
    asyncio.run(run())


def test_completed_deep_receipt_cannot_be_overwritten_by_later_inner_call(tmp_path):
    async def run():
        agent, inner, model, observer, evaluator = await make_agent(tmp_path)
        try:
            await agent.invoke({"query": "outer", "conversation_id": "current"})
            receipt = next((tmp_path / "evaluation_ui/invocations").glob("*.json"))
            original = receipt.read_bytes()
            source = Path(json.loads(original)["source_path"])
            assert json.loads(source.read_text())["result"]["agent_output"] == "outer"
            model.answer = "later-inner"
            await inner.invoke({"query": "inner", "conversation_id": "current"})
            assert receipt.read_bytes() == original
            assert len(list(source.parent.glob("agent_*.json"))) == 1
            unlinked, = source.parent.glob("unlinked_agent_*.json")
            assert json.loads(unlinked.read_text())["result"]["agent_output"] == "later-inner"
            assert json.loads(source.read_text())["result"]["agent_output"] == "outer"
        finally:
            await cleanup(agent, observer, evaluator)
    asyncio.run(run())


@pytest.mark.parametrize("deep", [False, True])
def test_reference_is_published_before_invocation_binding_is_released(tmp_path, deep):
    async def run():
        agent, inner, model, observer, evaluator = await make_agent(tmp_path, deep=deep)
        try:
            for answer in ("first", "second"):
                model.answer = answer
                await agent.invoke({"query": answer, "conversation_id": "current"})
                # A new task must not inherit an address for an already completed run.
                async def inherited_reference():
                    return inner._ojw_trace_handoff.get()
                assert await asyncio.create_task(inherited_reference()) is None
            receipts = [json.loads(path.read_text()) for path in (tmp_path / "evaluation_ui/invocations").glob("*.json")]
            assert len(receipts) == 2
            assert len({receipt["source_path"] for receipt in receipts}) == 2
            assert {json.loads(Path(receipt["source_path"]).read_text())["result"]["agent_output"]
                    for receipt in receipts} == {"first", "second"}
        finally:
            await cleanup(agent, observer, evaluator)
    asyncio.run(run())
