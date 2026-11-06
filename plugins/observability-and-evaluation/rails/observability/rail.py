"""Shared observation Rail for explicit SDK registration and plugin loading."""

from .artifacts import session_directory
from hashlib import sha256
import importlib
import importlib.util
from pathlib import Path
import logging
import sys
import threading

from openjiuwen.core.single_agent.agents.react_agent import ReActAgent
from openjiuwen.core.sys_operation.cwd import get_workspace
from openjiuwen.harness.deep_agent import DeepAgent
from openjiuwen.harness.rails.base import DeepAgentRail

logger = logging.getLogger(__name__)
_load_lock = threading.Lock()


def _collector_module():
    if __package__ in sys.modules:
        return importlib.import_module('.decorator', __package__)
    # The host discards its temporary package aliases after loading Rails.
    # Deferred imports need a namespace owned by this installed package path.
    directory = Path(__file__).resolve().parent
    name = '_ojw_observation_' + sha256(str(directory).encode()).hexdigest()[:16]
    with _load_lock:
        if name not in sys.modules:
            spec = importlib.util.spec_from_file_location(
                name, directory / '__init__.py', submodule_search_locations=[str(directory)])
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            try:
                spec.loader.exec_module(module)
            except BaseException:
                sys.modules.pop(name, None)
                raise
        return importlib.import_module(name + '.decorator')


def __getattr__(name):
    if name in {"observe", "unobserve"}:
        return getattr(_collector_module(), name)
    raise AttributeError(name)


class ObservabilityRail(DeepAgentRail):
    """Attach the existing collector without changing its trace boundaries.

    ReActAgent requires configure(output_dir=...) before registration. DeepAgent
    can instead use the current workspace and session at invocation time.
    Use a separate Rail instance for each agent.
    """

    def __init__(self) -> None:
        super().__init__()
        self._agent = None
        self._observed = None
        self._wrapper = None
        self._output_dir = None
        self._session_directories = {}
        self._force_non_streaming = True
        self.last_error = None

    def configure(self, *, output_dir=None, force_non_streaming=True):
        """Set options before registration; return this Rail for chaining.

        Explicit output_dir preserves the caller's directory layout. Without
        it, DeepAgent uses workspace/observability_traces/<session-key>.
        """
        if self._agent is not None:
            raise RuntimeError("Configure ObservabilityRail before registering it")
        if not isinstance(force_non_streaming, bool):
            raise TypeError("force_non_streaming must be a bool")
        self._output_dir = Path(output_dir).expanduser().resolve() if output_dir is not None else None
        self._force_non_streaming = force_non_streaming
        return self

    def init(self, agent) -> None:
        if self._agent is not None and self._agent is not agent:
            raise RuntimeError("Use a separate ObservabilityRail for each agent")
        if isinstance(agent, ReActAgent):
            if self._output_dir is None:
                raise ValueError("Configure output_dir before registering ObservabilityRail on ReActAgent")
            # Wrapping this agent in its own before_invoke would miss the first call.
            self._attach(agent, self._output_dir)
        elif not isinstance(agent, DeepAgent):
            raise TypeError("ObservabilityRail supports ReActAgent and DeepAgent")
        self._agent = agent

    def _attach(self, target, directory) -> None:
        try:
            collector = _collector_module()
            observe, unobserve = collector.observe, collector.unobserve
        except ImportError as exc:
            self._dependency_error(exc)
            return
        if self._observed is not None:
            if target is not self._observed or target.invoke is not self._wrapper:
                raise RuntimeError("Observed agent or invoke changed; unregister the observation before replacing it")
            if self._wrapper.__ojw_output_dir__ != directory:
                # A DeepAgent can serve another session with a different
                # output directory. Calls already running retain their own
                # collector and directory when this owned wrapper is detached.
                unobserve(target)
                self._observed = None
                self._wrapper = None
        elif getattr(target.invoke, "__ojw_observed__", False):
            raise RuntimeError("Agent is already observed; remove its existing observation before registering this Rail")
        try:
            observe(target, output_dir=directory, force_non_streaming=self._force_non_streaming)
        except ImportError as exc:
            self._dependency_error(exc)
            return
        self._observed = target
        self._wrapper = target.invoke
        self.last_error = None

    def _dependency_error(self, exc):
        self.last_error = (f"Observation unavailable: dependency {exc.name or 'unknown'} could not load. "
                           "Install the plugin requirements.txt in the agent's Python environment.")
        logger.warning(self.last_error)

    async def before_invoke(self, ctx) -> None:
        if self._agent is None:
            raise RuntimeError("ObservabilityRail has not been initialized")
        if isinstance(self._agent, ReActAgent):
            if self._observed is None:
                self._attach(self._agent, self._output_dir)
                return
            if self._agent.invoke is not self._wrapper:
                raise RuntimeError("Observed invoke changed; unregister ObservabilityRail before replacing it")
            return

        inner = self._agent.react_agent
        if inner is None:
            raise RuntimeError("OJW Observability requires an initialized inner agent")
        directory = self._output_dir
        if directory is None:
            session_id = (
                ctx.session.get_session_id()
                if ctx.session is not None
                else getattr(ctx.inputs, "conversation_id", None)
            )
            if not isinstance(session_id, str) or not session_id:
                raise RuntimeError("OJW Observability requires a session ID")
            directory = self._session_directories.get(session_id)
            if directory is None:
                workspace = get_workspace()
                if not workspace:
                    raise RuntimeError("OJW Observability requires a configured workspace")
                directory = session_directory(workspace, session_id)
                # A host may change its runtime workspace between chat turns.
                # Keep this session's collector in its original directory.
                self._session_directories[session_id] = directory
        self._attach(inner, directory)

    def uninit(self, agent) -> None:
        if self._agent is not None and self._agent is not agent:
            raise RuntimeError("Cannot detach ObservabilityRail from a different agent")
        if self._observed is not None:
            if self._observed.invoke is not self._wrapper:
                raise RuntimeError("Cannot detach observation: another wrapper replaced invoke")
            _collector_module().unobserve(self._observed)
        self._observed = None
        self._wrapper = None
        self._session_directories.clear()
        self._agent = None
        self.last_error = None
