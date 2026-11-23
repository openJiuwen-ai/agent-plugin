"""Runtime boundaries must work independently of a writable source checkout."""

from pathlib import Path
import shutil
import subprocess
import sys
import textwrap

import pytest

pytest.importorskip("openjiuwen")
PACKAGE = Path(__file__).resolve().parents[3] / "plugins/observability-and-evaluation"

PRELUDE = '''
import asyncio, importlib, importlib.abc, json, os, sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from openjiuwen.core.single_agent import AgentCard
from openjiuwen.core.single_agent.agents.react_agent import ReActAgent, ReActAgentConfig
from openjiuwen.core.foundation.llm import AssistantMessage
from openjiuwen.core.sys_operation.cwd import init_cwd
from openjiuwen.harness import DeepAgent, DeepAgentConfig, Workspace
package = Path(sys.argv[1])
work = Path(sys.argv[2])
init_cwd(str(work), workspace=str(work))
namespace = ModuleType("tested_plugin")
namespace.__path__ = [str(package)]
sys.modules[namespace.__name__] = namespace
def module(name):
    return importlib.import_module("tested_plugin." + name)
def block(*prefixes):
    for name in list(sys.modules):
        if any(name == p or name.startswith(p + ".") for p in prefixes):
            del sys.modules[name]
    class Missing(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if any(fullname == p or fullname.startswith(p + ".") for p in prefixes):
                raise ModuleNotFoundError("Test dependency unavailable", name=fullname)
    sys.meta_path.insert(0, Missing())
'''


def exercise(tmp_path, code, *, readonly=False):
    package = tmp_path / "checkout/plugins/observability-and-evaluation"
    shutil.copytree(PACKAGE, package, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (tmp_path / "checkout/pyproject.toml").write_text("[project]\nname='unrelated-project'\n")
    legacy = tmp_path / "checkout/agentic_structured_test_trajectories/unlabelled"
    legacy.mkdir(parents=True)
    (legacy / "only-legacy.json").write_text('{"steps": [{"step_id": "1"}]}')
    work = tmp_path / "workspace"
    work.mkdir()
    paths = [*package.rglob("*"), package]
    if readonly:
        for path in paths:
            path.chmod(0o555 if path.is_dir() else 0o444)
    try:
        result = subprocess.run([sys.executable, "-I", "-B", "-c", PRELUDE + textwrap.dedent(code),
                                 str(package), str(work)], cwd=work, capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        if readonly:
            for path in paths:
                path.chmod(0o755 if path.is_dir() else 0o644)


def test_observe_requires_an_explicit_output_directory(tmp_path):
    exercise(tmp_path, '''
    observer = module("rails.observability")
    agent = ReActAgent(AgentCard(id="explicit", name="explicit"))
    original = agent.invoke
    try:
        observer.observe(agent)
    except ValueError as exc:
        assert "output_dir" in str(exc)
    else:
        raise AssertionError("observe accepted an implicit package output directory")
    assert agent.invoke == original
    ''')


def test_cli_does_not_find_trajectories_in_an_ancestor_repository(tmp_path):
    exercise(tmp_path, '''
    cli = module("rails.evaluation.cli")
    try:
        cli.resolve_trajectory("only-legacy.json")
    except SystemExit as exc:
        assert "No trajectory" in str(exc)
    else:
        raise AssertionError("CLI selected a trajectory from an unrelated repository")
    config = module("rails.evaluation.core.config")
    trajectory = work / "run/trajectory.json"
    run = config.RunConfig(module="failure-detector", strategy="zero-shot", model="test", client="openai", trajectory=trajectory)
    assert run.output_dir == trajectory.parent / "evaluation"
    ''')


def test_file_picker_uses_launch_and_explicit_roots_only(tmp_path):
    exercise(tmp_path, '''
    server = module("rails.evaluation.server")
    service = server.EvaluationService(work / "service", port=0, browse_roots={"workspace": work})
    try:
        assert set(service.files.roots) == {"launch", "results", "workspace"}
    finally:
        service.close()
    ''')


def test_dataset_and_pdf_libraries_are_not_required_by_public_apis(tmp_path):
    exercise(tmp_path, '''
    block("pandas", "pypdf")
    observer = module("rails.observability")
    assert callable(observer.observe)
    assert callable(observer.merge_workflow)
    assert callable(observer.merge_hierarchical_workflow)
    helpers = module("rails.observability.utils")
    assert helpers.PromptTemplate("{name}", "hello", ["name"]).render(name="test")["system"] == "test"
    ''')


def test_missing_observation_dependencies_do_not_break_plugin_loading(tmp_path):
    exercise(tmp_path, '''
    block("opentelemetry.sdk", "opentelemetry.instrumentation")
    observer = module("rails.observability_rail").ObservabilityRail().configure(output_dir=work)
    module("rails.evaluation_rail").EvaluationRail()
    tool = module("tools.open_evaluation_tool").OpenDialogueEvaluationTool()
    agent = ReActAgent(AgentCard(id="missing", name="missing"))
    original = agent.invoke
    observer.init(agent)
    assert observer.last_error and "opentelemetry" in observer.last_error
    assert agent.invoke == original
    observer.uninit(agent)
    session = SimpleNamespace(get_session_id=lambda: "dialogue", get_state=lambda key: None)
    result = asyncio.run(tool.invoke({}, session=session))
    assert result["success"] is False
    assert "opentelemetry" in result["error"] and "requirements" in result["error"]
    ''')


def test_missing_judge_dependency_has_an_actionable_runtime_error(tmp_path):
    exercise(tmp_path, '''
    block("openai")
    cli = module("rails.evaluation.cli")
    assert cli.build_parser().prog == "ojw_evaluation"
    module("rails.evaluation.server")
    trajectory = work / "trajectory.json"
    trajectory.write_text('{"steps": [{"step_id": "1"}]}')
    assert cli.main(["run", "--trajectory", str(trajectory)]) != 0
    assert not (work / "evaluation").exists()
    rail = module("rails.evaluation_rail").EvaluationRail()
    assert asyncio.run(rail._prepare_service(work / "service", work)) is None
    assert "openai" in rail.last_error and "requirements" in rail.last_error
    assert not (work / "service").exists()
    server = module("rails.evaluation.server")
    try:
        server.EvaluationService(work / "direct-service", port=0)
    except RuntimeError as exc:
        assert "openai" in str(exc) and "requirements" in str(exc)
    else:
        raise AssertionError("Service started without its evaluator dependencies")
    assert not (work / "direct-service").exists()
    try:
        asyncio.run(module("rails.evaluation.workflow").open_evaluation_ui(trajectory, workspace=work / "workflow"))
    except RuntimeError as exc:
        assert "openai" in str(exc)
    else:
        raise AssertionError("Workflow UI started without evaluator dependencies")
    assert not (work / "workflow").exists()
    ''')


def test_readonly_plugin_records_sessions_in_their_workspaces(tmp_path):
    exercise(tmp_path, '''
    # Observe package imports after the host SDK has initialized. Bytecode
    # writes are disabled by -B; plugin imports must have no runtime effects.
    def audit(event, args):
        if event == "subprocess.Popen" or event == "socket.connect":
            raise AssertionError("Import caused " + event)
        if event == "open" and isinstance(args[2], int) and args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
            raise AssertionError("Import wrote " + str(args[0]))
        if event in {"os.mkdir", "os.remove", "os.rename"}:
            raise AssertionError("Import changed filesystem: " + event)
    active = True
    sys.addaudithook(lambda event, args: audit(event, args) if active else None)
    observation = module("rails.observability_rail")
    module("rails.evaluation_rail")
    module("tools.open_evaluation_tool")
    active = False
    class Model:
        async def invoke(self, **kwargs):
            return AssistantMessage(content="offline answer", finish_reason="stop")
    async def record():
        agent = DeepAgent(AgentCard(id="readonly", name="readonly"))
        agent.configure(DeepAgentConfig(workspace=Workspace(root_path=str(work)), auto_create_workspace=True, enable_task_loop=False))
        agent.react_agent.set_llm(Model())
        rail = observation.ObservabilityRail()
        await agent.register_rail(rail)
        try:
            for session in ("session-one", "session-two"):
                await agent.invoke({"query": "test", "conversation_id": session})
                assert rail.last_error is None
        finally:
            await agent.unregister_rail(rail)
        directories = [p for p in (work / "observability_traces").iterdir() if p.is_dir()]
        assert len(directories) == 2
        assert all(len(list(path.glob("agent_*.json"))) == 1 for path in directories)
    asyncio.run(record())
    ''', readonly=True)


def test_observer_initializes_after_host_removes_temporary_import_aliases(tmp_path):
    exercise(tmp_path, '''
    rail_type = module("rails.observability_rail").ObservabilityRail
    for name in list(sys.modules):
        if name == "tested_plugin" or name.startswith("tested_plugin."):
            del sys.modules[name]
    agent = ReActAgent(AgentCard(id="loader", name="loader"))
    rail = rail_type().configure(output_dir=work)
    original = agent.invoke
    rail.init(agent)
    try:
        assert rail.last_error is None, rail.last_error
        assert agent.invoke != original
    finally:
        rail.uninit(agent)
    assert agent.invoke == original
    ''')
