"""Publish invocation context to the workspace UI; never own its lifetime."""
import asyncio
from contextvars import Context, ContextVar
from hashlib import sha256
import json
import logging
from pathlib import Path
import time
import webbrowser
from uuid import uuid4

from openjiuwen.core.sys_operation.cwd import get_original_cwd, get_workspace
from openjiuwen.core.single_agent.interrupt.state import INTERRUPTION_KEY
from openjiuwen.harness.rails.base import DeepAgentRail

from .runtime import announce_service, ensure_service
from .state import write_json
from .state import locked, read_json
from .dialogues import SESSION_POINTER, begin_exchange, dialogue_id, register_dialogue
from .opening import open_saved_dialogue
from .dependencies import EvaluationDependencyError

logger = logging.getLogger(__name__)


def _is_evaluation_command(query):
    if not isinstance(query, str):
        return False
    query = query.strip()
    if query == '/evaluate':
        return True
    # JiuwenSwarm renders the current user turn as a JSON envelope before
    # invoking the SDK. Unwrap exactly one host envelope, never quoted history
    # or an internal dispatch, and keep standalone SDK usage dependency-free.
    for prefix in ('You receive a new message:\n', '\u4f60\u6536\u5230\u4e00\u6761\u6d88\u606f\uff1a\n'):
        if not query.startswith(prefix):
            continue
        try:
            envelope = json.loads(query[len(prefix):])
        except (ValueError, RecursionError):
            return False
        if not isinstance(envelope, dict):
            return False
        source = envelope.get('source')
        content = envelope.get('content')
        return (envelope.get('type') == 'user input'
                and envelope.get('origin_kind') == 'external_user_authored'
                and isinstance(source, str) and bool(source.strip())
                and source.strip().lower() not in {'system', 'cron', 'heartbeat', 'agent_session'}
                and isinstance(content, str) and content.strip() == '/evaluate')
    return False


class EvaluationRail(DeepAgentRail):
    def __init__(self) -> None:
        super().__init__()
        self._agent = None
        self._generation = 0
        self._output_dir = None
        self._port = 8765
        self._open_browser = False
        self._scope = 'dialogue'
        self._handoffs = {}
        self._namings = {}
        self._invocations = {}
        self._commands = {}
        self._contexts = {}
        self._services = {}
        self._startups = {}
        self.last_error = None
        self.last_url = None

    def configure(self, *, output_dir=None, port=8765, open_browser=False, scope='dialogue'):
        if self._agent is not None:
            raise RuntimeError('Configure EvaluationRail before registration')
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError('port must be between 0 and 65535')
        if type(open_browser) is not bool:
            raise TypeError('open_browser must be a bool')
        if scope not in ('dialogue', 'invocation'):
            raise ValueError('scope must be dialogue or invocation')
        self._output_dir = Path(output_dir).expanduser().resolve() if output_dir is not None else None
        self._port, self._open_browser = port, open_browser
        self._scope = scope
        return self

    def init(self, agent) -> None:
        if self._agent is not None and self._agent is not agent:
            raise RuntimeError('Use a separate EvaluationRail for each agent')
        if self._agent is None:
            self._generation += 1
        self._agent = agent

    @staticmethod
    def _session_id(ctx):
        session = getattr(ctx, 'session', None)
        inputs = getattr(ctx, 'inputs', None)
        return session.get_session_id() if session is not None else (
            inputs.get('conversation_id') if isinstance(inputs, dict) else getattr(inputs, 'conversation_id', None))

    def _context(self, ctx):
        sid = self._session_id(ctx)
        if not isinstance(sid, str) or not sid:
            raise ValueError('Evaluation UI needs a workspace/output_dir and session ID')
        card = getattr(self._agent, 'card', None)
        agent_id = str(getattr(card, 'id', None) or 'agent')
        key = dialogue_id(sid) if self._scope == 'dialogue' else sha256((sid + '\0' + agent_id).encode()).hexdigest()[:32]
        if key not in self._contexts:
            session = getattr(ctx, 'session', None)
            pointer = session.get_state(SESSION_POINTER) if session is not None and self._scope == 'dialogue' else None
            saved_workspace = (pointer.get('workspace') if isinstance(pointer, dict)
                               and pointer.get('session_id') == sid else None)
            workspace = self._output_dir or saved_workspace or get_workspace()
            if not workspace:
                raise ValueError('Evaluation UI needs a workspace/output_dir and session ID')
            context = {
                'id': key, 'session_id': sid, 'agent_id': agent_id,
                'agent_name': str(getattr(card, 'name', None) or agent_id),
                'launch_dir': str(Path(get_original_cwd()).resolve())}
            if self._scope == 'dialogue':
                context = register_dialogue(Path(workspace) / 'evaluation_ui', sid,
                                            launch_dir=context['launch_dir'])
            self._contexts[key] = (Path(workspace).resolve(), context)
        workspace, context = self._contexts[key]
        return workspace, dict(context)

    async def before_invoke(self, ctx) -> None:
        if self._agent is None:
            return
        target = getattr(self._agent, 'react_agent', None) or self._agent
        self._invocations.pop(id(ctx), None)
        inputs = getattr(ctx, 'inputs', None)
        query = inputs.get('query') if isinstance(inputs, dict) else getattr(inputs, 'query', None)
        command = None
        if _is_evaluation_command(query):
            command = {'session_id': self._session_id(ctx)}
            self._commands[id(ctx)] = command
            self.last_error = self.last_url = None
        try:
            variable = getattr(target, '_ojw_trace_handoff', None)
            if variable is not None:
                variable.set(None)
            naming_variable = getattr(target, '_ojw_trace_naming', None)
            if naming_variable is not None:
                naming_variable.set(None)
            if command is not None:
                self._suspend_interruption(ctx, command)
            if command is not None and self._scope != 'dialogue':
                raise ValueError('/evaluate requires EvaluationRail scope="dialogue"')
            workspace, context = self._context(ctx)
            self._remember_session(ctx, workspace, context)
            if command is not None:
                # A UI command has no execution receipt and cannot enter the
                # merged trajectory. The scheduler bridges the model hook by
                # session ID, just as it does for ordinary trace receipts.
                command['metadata_path'] = context['metadata_path']
                if read_json(context['metadata_path'], {}).get('artifact_layout_version') == 2:
                    command['naming'] = {'kind': 'command'}
                    self._set_naming(target, command['naming'])
                return
            if self._scope == 'dialogue':
                session = getattr(ctx, 'session', None)
                resuming = session is not None and session.get_state(INTERRUPTION_KEY) is not None
                exchange = begin_exchange(context['metadata_path'], resuming=resuming)
                if exchange is not None:
                    context['exchange_index'] = exchange
                    self._set_naming(target, {'kind': 'exchange', 'exchange_index': exchange})
            if variable is None:
                variable = ContextVar('ojw_trace_handoff', default=None)
                target._ojw_trace_handoff = variable
                self._handoffs[id(target)] = (target, variable)
            invocation = uuid4().hex
            receipt = workspace / 'evaluation_ui' / 'invocations' / f'{invocation}.json'
            write_json(receipt, {'id': invocation, 'context_id': context['id'], **{k: v for k, v in context.items() if k != 'id'},
                                 'created_at': time.time(), 'source_path': None, 'source_paths': [], 'closed': False})
            # Keep the exact workspace and context for AFTER_INVOKE: the host
            # can change its SDK workspace while this invocation is running.
            self._invocations[id(ctx)] = (workspace, context, receipt)
            variable.set(receipt)
            self.last_error = None
            self._start_service(workspace, refresh=True)
        except Exception as exc:
            if command is not None:
                command['error'] = str(exc)
                self.last_error = str(exc)
                return
            self.last_error = f'Evaluation context unavailable ({type(exc).__name__})'
            logger.warning(self.last_error)

    def _set_naming(self, target, naming):
        variable = getattr(target, '_ojw_trace_naming', None)
        if variable is None:
            variable = ContextVar('ojw_trace_naming', default=None)
            target._ojw_trace_naming = variable
            self._namings[id(target)] = (target, variable)
        variable.set(naming)

    def _start_service(self, workspace, *, refresh=False):
        root = workspace / 'evaluation_ui'
        key = str(root)
        task = self._startups.get(key)
        if task is None or (refresh and task.done()):
            # Keep startup independent of the invoking task and its trace handoff.
            # Concurrent invocations share a pending check; later turns check
            # again so a stopped service or failed startup can recover.
            task = asyncio.create_task(self._prepare_service(root, workspace), context=Context())
            self._startups[key] = task
        return task

    @staticmethod
    def _check_service(root, workspace, port):
        try:
            return ensure_service(root, workspace=workspace, port=port)
        except Exception as exc:
            # Cancelling the asyncio waiter cannot stop this launcher thread.
            # Log here so late failures remain visible even after Rail unload.
            logger.warning('%s', str(exc) if isinstance(exc, EvaluationDependencyError)
                           else f'Evaluation UI could not start ({type(exc).__name__})')
            raise

    async def _prepare_service(self, root, workspace):
        generation = self._generation
        try:
            handle = await asyncio.to_thread(self._check_service, root, workspace, self._port)
        except Exception as exc:
            if generation == self._generation:
                self.last_error = (str(exc) if isinstance(exc, EvaluationDependencyError)
                                   else f'Evaluation UI could not start ({type(exc).__name__})')
            return None
        if generation != self._generation:
            return None
        self._services[str(root)] = handle
        announce_service(handle)
        return handle

    @staticmethod
    def _suspend_interruption(ctx, command):
        # ReAct resumes tools/workflows before BEFORE_MODEL_CALL. Preserve the
        # shared SDK interruption slot so this command cannot supply a reply to
        # an outstanding approval or consume its continuation state.
        session = getattr(ctx, 'session', None)
        pending = session.get_state(INTERRUPTION_KEY) if session is not None else None
        if pending is not None:
            command.setdefault('suspended', {})[id(session)] = (session, pending)
            session.update_state({INTERRUPTION_KEY: None})

    @staticmethod
    def _restore_interruption(command):
        for session, pending in command.get('suspended', {}).values():
            session.update_state({INTERRUPTION_KEY: pending})

    async def before_model_call(self, ctx) -> None:
        sid = self._session_id(ctx)
        matches = [command for command in self._commands.values() if command['session_id'] == sid]
        if not matches or ctx.has_force_finish_request:
            return
        try:
            if len(matches) != 1 or any(context['session_id'] == sid for _, context, _ in self._invocations.values()):
                raise ValueError('Cannot open evaluation during concurrent invocations of the same dialogue')
            command = matches[0]
            if 'error' in command:
                raise ValueError(command['error'])
            if 'result' not in command:
                opened = await asyncio.to_thread(open_saved_dialogue, command['metadata_path'], port=self._port)
                command['result'] = {'output': opened['url'], 'result_type': 'answer'}
            self.last_url = command['result']['output']
            self.last_error = None
            ctx.request_force_finish(command['result'])
        except Exception as exc:
            self.last_error = str(exc)
            ctx.request_force_finish({'output': str(exc), 'result_type': 'error'})

    async def before_task_iteration(self, ctx) -> None:
        if self._agent is None:
            return
        sid = self._session_id(ctx)
        for command in self._commands.values():
            if command['session_id'] == sid:
                self._suspend_interruption(ctx, command)
        target = getattr(self._agent, 'react_agent', None) or self._agent
        command_matches = [command for command in self._commands.values() if command['session_id'] == sid]
        invocation_matches = [context for _, context, _ in self._invocations.values() if context['session_id'] == sid]
        naming = None
        if len(command_matches) == 1 and not invocation_matches:
            naming = command_matches[0].get('naming')
        elif len(invocation_matches) == 1 and not command_matches and invocation_matches[0].get('exchange_index'):
            naming = {'kind': 'exchange', 'exchange_index': invocation_matches[0]['exchange_index']}
        if naming is not None or hasattr(target, '_ojw_trace_naming'):
            self._set_naming(target, naming)
        variable = getattr(target, '_ojw_trace_handoff', None)
        if variable is not None:
            # The scheduler predates BEFORE_INVOKE, so its copied Context does
            # not inherit that hook's ContextVar value. Bind the address inside
            # the worker, using only this agent's active matching session.
            matches = [receipt for _, context, receipt in self._invocations.values()
                       if context['session_id'] == sid]
            variable.set(matches[0] if len(matches) == 1 else None)
            for workspace, context, _ in self._invocations.values():
                if context['session_id'] == sid:
                    self._remember_session(ctx, workspace, context)
            if len(matches) > 1:
                logger.warning('Evaluation trace reference is ambiguous for concurrent invocations')

    async def after_task_iteration(self, ctx) -> None:
        if self._agent is None:
            return
        target = getattr(self._agent, 'react_agent', None) or self._agent
        variable = getattr(target, '_ojw_trace_handoff', None)
        if variable is not None:
            variable.set(None)
        naming = getattr(target, '_ojw_trace_naming', None)
        if naming is not None:
            naming.set(None)

    def _remember_session(self, ctx, workspace, context):
        session = getattr(ctx, 'session', None)
        if self._scope == 'dialogue' and session is not None:
            session.update_state({SESSION_POINTER: {'session_id': context['session_id'],
                                  'workspace': str(workspace), 'context_id': context['id']}})

    async def after_invoke(self, ctx) -> None:
        if self._agent is None:
            return
        generation = self._generation
        try:
            command = self._commands.pop(id(ctx), None)
            if command is not None:
                self._restore_interruption(command)
                return
            invocation = self._invocations.pop(id(ctx), None)
            if invocation is None:
                workspace, context = self._context(ctx)
                receipt = None
            else:
                workspace, context, receipt = invocation
            root = workspace / 'evaluation_ui'
            if receipt is not None:
                with locked(receipt.with_suffix('.lock')):
                    record = read_json(receipt, {})
                    record.update(closed=True, finished_at=time.time())
                    write_json(receipt, record)
                if self._scope == 'invocation':
                    context['latest_invocation'] = receipt.stem
            write_json(root / 'contexts' / f'{context["id"]}.json', context)
            startup = self._start_service(workspace)
            if startup.done() and not startup.cancelled() and startup.result() is not None:
                # A long turn may outlive the service it started. Preserve the
                # end-of-turn health check before publishing or opening its URL.
                startup = self._start_service(workspace, refresh=True)
            # Do not let a cancelled invocation cancel another turn's startup.
            try:
                handle = await asyncio.shield(startup)
            except asyncio.CancelledError:
                # Unload owns cancellation of its startup task, not of this
                # invocation. A cancellation requested by the caller still wins.
                if generation != self._generation and not asyncio.current_task().cancelling():
                    return
                raise
            if generation != self._generation or handle is None:
                return
            self.last_url = handle.url + '&context=' + context['id']
            if receipt is not None and self._scope == 'invocation':
                self.last_url += '&invocation=' + receipt.stem
            self.last_error = None
            if self._scope == 'invocation' or self._open_browser:
                logger.warning('Evaluation UI ready: %s (connection details: %s)', self.last_url, root / 'launch.json')
            else:
                logger.info('Dialogue evaluation recorded; send /evaluate to get its link.')
            if self._open_browser:
                await asyncio.to_thread(webbrowser.open, self.last_url)
        except Exception as exc:
            self.last_error = f'Evaluation UI could not start ({type(exc).__name__})'
            logger.warning(self.last_error)
        finally:
            # A later direct inner-agent call must not inherit the completed
            # invocation's receipt. The collector has already captured it.
            if generation == self._generation:
                target = getattr(self._agent, 'react_agent', None) or self._agent
                variable = getattr(target, '_ojw_trace_handoff', None)
                if variable is not None:
                    variable.set(None)
                naming = getattr(target, '_ojw_trace_naming', None)
                if naming is not None:
                    naming.set(None)

    def uninit(self, agent) -> None:
        if self._agent is not None and self._agent is not agent:
            raise RuntimeError('Cannot detach EvaluationRail from a different agent')
        for target, variable in self._handoffs.values():
            if getattr(target, '_ojw_trace_handoff', None) is variable:
                del target._ojw_trace_handoff
        self._handoffs.clear()
        for target, variable in self._namings.values():
            if getattr(target, '_ojw_trace_naming', None) is variable:
                del target._ojw_trace_naming
        self._namings.clear()
        self._invocations.clear()
        for command in self._commands.values():
            self._restore_interruption(command)
        self._commands.clear()
        self._contexts.clear()
        self._generation += 1
        # Cancel only coordination tasks. A running ensure_service thread may
        # finish launching the independently owned workspace process; it must
        # not repopulate this detached Rail when it returns.
        for task in self._startups.values():
            task.cancel()
        self._startups.clear()
        self._services.clear()
        self._agent = None
        # Workspace service and evaluations continue until explicitly stopped.
