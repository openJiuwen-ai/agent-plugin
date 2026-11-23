"""Offline ZIP smoke: python -I -B <script> <extracted plugin> <workspace>.

Requires plugin core dependencies; --host-envelope additionally requires WorkSwarm.
Use an interpreter without the standalone package installed to prove ZIP isolation.
"""
import argparse
import ast
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from openjiuwen.core.foundation.llm import AssistantMessage, ToolCall
from openjiuwen.core.single_agent import AgentCard
from openjiuwen.core.session.agent import create_agent_session
from openjiuwen.core.single_agent.interrupt.state import INTERRUPTION_KEY, ToolInterruptEntry, ToolInterruptionState
from openjiuwen.core.sys_operation.cwd import init_cwd, set_original_cwd, set_workspace
from openjiuwen.harness import DeepAgent, DeepAgentConfig, Workspace
from openjiuwen.harness.schema.interaction import RoundWorkItem


class Model:
    def __init__(self, moved_workspace=None):
        self.moved_workspace = moved_workspace
        self.open_requested = False
        self.tool_result = None
        self.calls = 0

    async def invoke(self, **kwargs):
        self.calls += 1
        if self.moved_workspace is not None:
            set_workspace(str(self.moved_workspace))
            set_original_cwd(str(self.moved_workspace / 'work'))
        if self.open_requested:
            self.open_requested = False
            return AssistantMessage(tool_calls=[ToolCall(id='open-evaluation', type='function',
                name='open_dialogue_evaluation', arguments='{}')], finish_reason='tool_calls')
        tool_messages = [m for m in kwargs.get('messages', []) if m.role == 'tool']
        if tool_messages:
            self.tool_result = tool_messages[-1].content
        return AssistantMessage(content='agent complete',finish_reason='stop')


class Provider(BaseHTTPRequestHandler):
    def log_message(self,*_):
        pass
    def do_POST(self):
        self.rfile.read(int(self.headers['Content-Length']))
        response={'id':'offline','object':'chat.completion','created':1,'model':'fake',
                  'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'{"annotations":[{"step_id":"1","failures":[]}]}'}}],
                  'usage':{'prompt_tokens':1,'completion_tokens':1,'total_tokens':2}}
        data=json.dumps(response).encode()
        self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)


def api(service,path,data=None):
    request=Request(f'http://127.0.0.1:{service.port}'+path,
                    data=json.dumps(data).encode() if data is not None else None,
                    headers={'Authorization':'Bearer '+service.token,'Content-Type':'application/json'})
    with urlopen(request,timeout=5) as response:
        return json.load(response)


def copied_package(name, directory):
    # SDK loader aliases are temporary and cannot be imported after load_plugin.
    # Load the copied package explicitly, as its independent service entry does.
    spec = importlib.util.spec_from_file_location(name, directory / '__init__.py',
                                                submodule_search_locations=[str(directory)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


async def main(package,workspace,*,interaction=False,move_workspace=False,host_envelope=False):
    for name in ('ojw_evaluation', 'ojw_observability', 'ojw_agentic_applications'):
        assert importlib.util.find_spec(name) is None, f'Test must not use an installed {name}'
    workspace.mkdir(parents=True,exist_ok=True)
    init_cwd(str(workspace),workspace=str(workspace))
    agent=DeepAgent(AgentCard(id='evaluation-smoke',name='evaluation-smoke'))
    agent.configure(DeepAgentConfig(workspace=Workspace(root_path=str(workspace)),auto_create_workspace=True,enable_task_loop=False))
    moved = workspace / 'chat-workspace'
    if move_workspace:
        (moved / 'work').mkdir(parents=True)
    model = Model(moved if move_workspace else None)
    def query(text):
        if not host_envelope:
            return text
        from jiuwenswarm.server.runtime.agent_adapter.user_turn import UserTurn
        return UserTurn(text=text, channel='web', language='en', files={},
                        origin_kind='external_user_authored').render()
    agent.react_agent.set_llm(model)
    record=await agent.load_plugin(str(package))
    rail=agent.find_rail_by_name('EvaluationRail')
    observer=agent.find_rail_by_name('ObservabilityRail')
    assert observer is not None
    assert Path(observer.before_invoke.__func__.__code__.co_filename).resolve().is_relative_to(package)
    assert Path(rail.after_invoke.__func__.__code__.co_filename).resolve().is_relative_to(package)
    provider=ThreadingHTTPServer(('127.0.0.1',0),Provider)
    thread=threading.Thread(target=provider.serve_forever,daemon=True);thread.start()
    try:
        urls = []
        sources = []
        with patch('webbrowser.open',return_value=False) as browser_open:
            session=create_agent_session(session_id='eval-session',card=agent.card)
            if interaction:
                await session.pre_run(inputs={})
                await agent._ensure_initialized()
                # JiuwenSwarm starts the scheduler before BEFORE_INVOKE.
                await agent.start(session=session)
            for round_number in range(2):
                model.open_requested = round_number == 1
                inputs={'query':query('offline test'),'conversation_id':'eval-session'}
                if interaction:
                    outcome = await agent.run_one_round(
                        RoundWorkItem.user(request_id=f'request-{round_number}', inputs=inputs),
                        f'task-{round_number}', session)
                    assert outcome.error_code is None, outcome
                else:
                    await agent.invoke(inputs,session)
                assert rail.last_error is None, rail.last_error
                service=next(iter(rail._services.values()))
                params=parse_qs(urlsplit(rail.last_url).fragment)
                route='/api/config?context='+params['context'][0]
                assert 'invocation' not in params
                deadline=time.monotonic()+15
                while time.monotonic()<deadline:
                    config=api(service,route)
                    if config['dialogue']['status']=='ready' and config['dialogue']['turn_count']==round_number+1:
                        break
                    await asyncio.sleep(.1)
                assert config['dialogue']['turn_count']==round_number+1, config['dialogue']
                selected=config['default_file']
                assert selected and selected['path']=='dialogue.json'
                metadata=json.loads(next((workspace/'observability_traces').glob('*/metadata.json')).read_text())
                sources=[Path(item['path']) for item in metadata['sources']]
                urls.append(route)
            assert not browser_open.called, 'Default dialogue Rail must never open a browser'
        tool_result=ast.literal_eval(model.tool_result) if isinstance(model.tool_result,str) else model.tool_result
        assert tool_result and tool_result['success'], tool_result
        assert tool_result['context_id']==params['context'][0]
        assert Path(tool_result['metadata_path']).is_relative_to(workspace/'observability_traces')
        assert service.token not in Path(metadata['trajectory_path']).read_text(), 'UI bearer token must not reach judge input'
        assert len(list((workspace/'observability_traces').glob('*/agent_*.json')))==2
        calls_before_command = model.calls
        receipts_before_command = set((workspace/'evaluation_ui/invocations').glob('*.json'))
        pending_call = ToolCall(id='pending', type='function', name='awaiting_approval', arguments='{}')
        pending = ToolInterruptionState(ai_message=AssistantMessage(tool_calls=[pending_call]), iteration=0,
            original_query='Original task', interrupted_tools={'pending': ToolInterruptEntry(tool_call=pending_call)})
        session.update_state({INTERRUPTION_KEY: pending})
        with patch('webbrowser.open', return_value=False) as browser_open, patch.object(
                agent.react_agent, '_execute_tool_call', wraps=agent.react_agent._execute_tool_call) as execute_tool:
            with patch.object(session, 'write_stream', wraps=session.write_stream) as stream:
                inputs = {'query': query('/evaluate'), 'conversation_id': 'eval-session'}
                if interaction:
                    outcome = await agent.run_one_round(
                        RoundWorkItem.user(request_id='evaluation-link', inputs=inputs),
                        'evaluation-link', session)
                    assert outcome.error_code is None, outcome
                    answers = [call.args[0].payload for call in stream.await_args_list
                               if getattr(call.args[0], 'type', None) == 'answer']
                    assert answers and answers[-1]['output'] == rail.last_url
                    if host_envelope:
                        from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
                        final_chunk = next(call.args[0] for call in reversed(stream.await_args_list)
                                           if getattr(call.args[0], 'type', None) == 'answer')
                        event = JiuWenSwarmDeepAdapter._parse_stream_chunk(final_chunk)
                        assert event == {'event_type': 'chat.final', 'content': rail.last_url}
                else:
                    answer = await agent.invoke(inputs, session)
                    assert answer['output'] == rail.last_url and answer['result_type'] == 'answer'
            assert not browser_open.called
            assert execute_tool.await_count == 0, '/evaluate must not resume a pending tool'
        assert model.calls == calls_before_command, '/evaluate must bypass inference'
        assert session.get_state(INTERRUPTION_KEY) == pending, '/evaluate must preserve the pending tool'
        session.update_state({INTERRUPTION_KEY: None})
        assert set((workspace/'evaluation_ui/invocations').glob('*.json')) == receipts_before_command
        assert parse_qs(urlsplit(rail.last_url).fragment)['context'] == params['context']
        assert api(service, urls[0])['dialogue']['revision'] == metadata['revision']
        assert service.token not in Path(metadata['trajectory_path']).read_text()
        assert len(rail._services)==1, rail.last_error
        assert service.output_dir == workspace / 'evaluation_ui'
        assert api(service,urls[0])['dialogue']['turn_count'] == 2
        assert not api(service, '/api/runs')['runs'], 'after_invoke must not start analysis'
        params = parse_qs(urlsplit(rail.last_url).fragment)
        config = api(service, '/api/config?context=' + params['context'][0])
        selected = config['default_file']
        assert selected, 'Current invocation trace must be selected automatically'
        source = Path(next(r['path'] for r in config['file_roots'] if r['id'] == selected['root'])) / selected['path']
        assert source.is_file()
        job=api(service,'/api/runs',{'client':'openai','api_key':'offline/key','model':'fake',
            'base_url':f'http://127.0.0.1:{provider.server_port}/v1','strategy':'zero-shot',
            'context_id':params['context'][0], 'server_file':selected})
        deadline=time.monotonic()+45
        while time.monotonic()<deadline:
            result=api(service,'/api/runs/'+job['id'])
            if result['status']!='running':
                break
            await asyncio.sleep(.1)
        assert result['status']=='completed',result
        assert result['input']['dialogue']['turn_count']==2
        assert Path(result['output_dir']).parent == source.parent / 'evaluation'
        assert result['annotations']['annotations'][0]['step_id']=='1'
        assert not any(b'offline/key' in p.read_bytes() for p in workspace.rglob('*') if p.is_file())
        # Standalone workflow handoff also works entirely from the copied package.
        exporter = copied_package('_copied_observability', package / 'rails/observability')
        evaluation = copied_package('_copied_evaluation', package / 'rails/evaluation')
        merged = exporter.merge_workflow(sources, workspace / 'agentic_trajectory.json')
        workflow_url = await evaluation.open_evaluation_ui(
            merged, workspace=workspace, port=0, open_browser=False, name='Packaged workflow')
        assert urlsplit(workflow_url).port == service.port
        workflow_params = parse_qs(urlsplit(workflow_url).fragment)
        workflow_config = api(service, '/api/config?context=' + workflow_params['context'][0] +
                              '&invocation=' + workflow_params['invocation'][0])
        assert workflow_config['context']['kind'] == 'workflow'
        assert workflow_config['invocation']['source_path'] == str(merged)
        preview = api(service, '/api/inspect', {'context_id': workflow_params['context'][0],
                      'server_file': workflow_config['default_file']})
        assert preview['format'] == 'Agent workflow' and preview['step_count'] >= 2
        assert api(service, '/api/runs?context=' + workflow_params['context'][0])['runs'] == []
        assert not any(name.split('.')[0] == 'ojw_agentic_applications' for name in sys.modules)
        await agent.unload_extension(record)
        assert api(service, '/api/health')
        assert list(workspace.rglob('launch.json'))
        (workspace/'integration-result.json').write_text(json.dumps({'passed':True,'run':result},indent=2))
        print('Combined plugin: dialogue traces + native Tool + /evaluate response + workspace change + worker snapshot + workflow UI + unload: PASS')
    finally:
        if interaction:
            await agent.stop()
        handles = dict(rail._services)
        if 'service' in locals():
            handles[str(service.output_dir)] = service
        for handle in handles.values():
            api(handle, '/api/shutdown', {})
        rail.uninit(agent)
        provider.shutdown();provider.server_close();thread.join()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('package',type=Path);parser.add_argument('workspace',type=Path)
    parser.add_argument('--interaction',action='store_true')
    parser.add_argument('--move-workspace',action='store_true')
    parser.add_argument('--host-envelope',action='store_true')
    args=parser.parse_args();asyncio.run(main(args.package.resolve(),args.workspace.resolve(),
                                             interaction=args.interaction,move_workspace=args.move_workspace,
                                             host_envelope=args.host_envelope))
