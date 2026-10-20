"""Manual smoke check through the host DeepAgent loader (SDK 0.1.18.post1).

Run with a host SDK environment; FINANCIAL_SCREENING_PYTHON selects the separate
workflow environment. Inspect is the default and never calls a model. Screen
requires the normal explicit inference configuration and uses the real endpoint.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import tempfile
import uuid


async def smoke(args):
    from openjiuwen.core.runner import Runner
    from openjiuwen.core.session.agent import create_agent_session
    from openjiuwen.core.single_agent.schema.agent_card import AgentCard
    from openjiuwen.core.sys_operation import LocalWorkConfig, OperationMode, SysOperationCard
    from openjiuwen.harness.deep_agent import DeepAgent
    from openjiuwen.harness.rails.skills.skill_use_rail import SkillUseRail
    from openjiuwen.harness.schema.config import DeepAgentConfig
    from openjiuwen.harness.workspace.workspace import Workspace

    sys_id = f"financial_smoke_{uuid.uuid4().hex[:8]}"
    agent = None
    record = None
    with tempfile.TemporaryDirectory(prefix="financial-host-smoke-") as work:
        try:
            Runner.resource_mgr.add_sys_operation(SysOperationCard(
                id=sys_id, mode=OperationMode.LOCAL,
                work_config=LocalWorkConfig(work_dir=work)))
            agent = DeepAgent(AgentCard(id=sys_id, name=sys_id, description="Financial plugin host smoke"))
            agent.configure(DeepAgentConfig(
                workspace=Workspace(root_path=str(Path(work) / "workspace")),
                sys_operation=Runner.resource_mgr.get_sys_operation(sys_id),
                auto_create_workspace=True, enable_task_loop=False,
                enable_skill_discovery=True,
                rails=[SkillUseRail(skills_dir=[], skill_mode="all", include_tools=False)]))
            await agent.ensure_initialized()
            record = await agent.load_plugin(str(args.package))
            kinds = sorted(ref.kind.value for ref in record.refs)
            assert kinds == ["skill", "tool"], kinds
            session = create_agent_session(session_id=sys_id, card=agent.card,
                                           close_stream_on_post_run=False)
            await session.pre_run(inputs={"query": "explicit plugin smoke check"})
            try:
                ref = next(ref for ref in record.refs if ref.kind.value == "tool")
                tool = Runner.resource_mgr.get_tool(ref.identity, session=session)
                assert tool is not None
                result = await tool.invoke({
                    "mode": args.mode, "filing_dir": str(args.filing_dir),
                    "output_dir": str(args.output_dir), "timeout_seconds": args.timeout_seconds,
                }, session=session)
            finally:
                await session.post_run()
            unloaded = await agent.unload_extension(record)
            assert len(unloaded) == len(record.refs)
            record = None
            return {"host_components": kinds, "unloaded": len(unloaded), "result": result}
        finally:
            if record is not None:
                await agent.unload_extension(record)
            Runner.resource_mgr.remove_sys_operation(sys_id)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--filing-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--mode", choices=("inspect", "screen"), default="inspect")
    parser.add_argument("--timeout-seconds", type=int, default=900)
    args = parser.parse_args()
    for name in ("package", "filing_dir", "output_dir"):
        if not getattr(args, name).is_absolute():
            parser.error(f"{name} must be an absolute path")
    result = asyncio.run(smoke(args))
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["result"].get("success") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
