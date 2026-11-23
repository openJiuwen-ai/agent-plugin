"""Real HTTP + isolated worker, with only the paid provider replaced locally."""
import asyncio
import json
import re
from pathlib import Path
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from urllib.parse import parse_qs, urlsplit
from ojw_evaluation.runtime import running_service

import pytest

from ojw_evaluation.server import EvaluationService, validate_request
from ojw_evaluation.rail import EvaluationRail


@pytest.fixture(autouse=True)
def stop_workspace_service(tmp_path):
    yield
    handle = running_service(tmp_path / 'evaluation_ui')
    if handle:
        handle.api('/api/shutdown', {})


def rail_config(rail):
    params = parse_qs(urlsplit(rail.last_url).fragment)
    service = next(iter(rail._services.values()))
    return service, request(service, '/api/config?context=' + params['context'][0] +
                            ('&invocation=' + params['invocation'][0] if 'invocation' in params else ''))


@pytest.fixture
def provider():
    calls = []
    counter_lock = threading.Lock()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            with counter_lock:
                calls.append(data)
                index = len(calls)
            if data['model'] == 'fail-model':
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b'{"error":{"message":"Invalid key"}}')
                return
            content = {'annotations': [{'step_id': '1', 'failures': []}]}
            if data['model'] == 'partial-model' and index == 1:
                content = {'annotations': 'invalid-output-shape'}
            response = {'id':'test', 'object':'chat.completion', 'created':1, 'model':data['model'],
                        'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':json.dumps(content)}}],
                        'usage':{'prompt_tokens':1,'completion_tokens':1,'total_tokens':2}}
            payload = json.dumps(response).encode()
            self.send_response(200)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
    server = ThreadingHTTPServer(('127.0.0.1',0), Handler)
    thread = threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{server.server_port}/v1', calls
    server.shutdown(); server.server_close(); thread.join()


@pytest.fixture
def service(tmp_path):
    value = EvaluationService(tmp_path, port=0)
    yield value
    value.close()


def request(service, path, payload=None, *, token=None, origin=None):
    headers = {'Authorization':'Bearer '+(service.token if token is None else token)}
    if origin:
        headers['Origin']=origin
    if payload is not None:
        headers['Content-Type']='application/json'
    req=Request(f'http://127.0.0.1:{service.port}'+path,
                data=json.dumps(payload).encode() if payload is not None else None, headers=headers)
    with urlopen(req, timeout=5) as response:
        return json.load(response)


def payload(base_url, **extra):
    return dict(client='openai', api_key='test/key/not-secret', model='fake-model',
                base_url=base_url, strategy='zero-shot', taxonomy='ours', dedup='none',
                trajectory={'steps':[{'step_id':'1','text':'hello'}]}, **extra)


def wait_job(service, job):
    deadline=time.monotonic()+45
    while time.monotonic()<deadline:
        result=request(service,'/api/runs/'+job['id'])
        if result['status']!='running':
            return result
        time.sleep(.1)
    pytest.fail('Worker did not finish')


@pytest.mark.parametrize("filename", ["agentic_trajectory.json", "agentic_tajectory.json", "merged_workflow_1.json", "workflow.json"])
def test_workflow_ui_exact_selection_and_manual_evaluation(tmp_path, provider, monkeypatch, filename):
    from ojw_evaluation import open_evaluation_ui
    import webbrowser

    opened = []
    monkeypatch.setattr(webbrowser, 'open', lambda url: opened.append(url) or True)
    paths = [tmp_path / run / filename for run in ('first', 'second')]
    for path in paths:
        path.parent.mkdir()
        path.write_text(json.dumps({'trace_metadata': {}, 'agents': [
            {'steps': [{'step_id': '1', 'text': path.parent.name}]}]}))
    first_url = asyncio.run(open_evaluation_ui(paths[0], workspace=tmp_path, port=0, name='Math scenario 1'))
    second_url = asyncio.run(open_evaluation_ui(paths[1], workspace=tmp_path, port=0, name='Math scenario 1'))
    assert opened == [first_url, second_url]
    assert urlsplit(first_url).port == urlsplit(second_url).port
    service = running_service(tmp_path / 'evaluation_ui')
    configs = []
    for url, path in zip(opened, paths):
        params = parse_qs(urlsplit(url).fragment)
        config = service.api('/api/config?context=' + params['context'][0] + '&invocation=' + params['invocation'][0])
        assert config['context']['kind'] == 'workflow'
        assert config['context']['name'] == 'Math scenario 1'
        assert config['invocation']['source_path'] == str(path)
        preview = service.api('/api/inspect', {'context_id': config['context']['id'],
            'invocation_id': config['invocation']['id'], 'server_file': config['default_file']})
        assert preview['source_path'] == str(path)
        assert preview['format'] == 'Agent workflow'
        configs.append(config)
    assert configs[0]['context']['id'] != configs[1]['context']['id']
    assert service.api('/api/runs')['runs'] == []
    assert provider[1] == []
    # Manual selection is authoritative; outputs follow it, not the opening run.
    data = payload(provider[0])
    data.pop('trajectory')
    data.update(context_id=configs[0]['context']['id'], server_file=configs[1]['default_file'])
    result = wait_job(service, request(service, '/api/runs', data))
    assert result['status'] == 'completed', result
    outcome = Path(result['output_dir']) / 'failure_detector_result.json'
    assert json.loads(outcome.read_text())['status'] == 'completed'
    assert not (outcome.parent / 'result.json').exists()
    assert Path(result['output_dir']).parent == paths[1].parent / 'evaluation'
    assert re.fullmatch(r'\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_ui(?:_\d+)?', Path(result['output_dir']).name)
    assert Path(result['annotations_file']).is_file()
    assert 'second' in json.dumps(provider[1][0]['messages'])
    assert service.api('/api/runs?context=' + configs[1]['context']['id'])['runs'] == []


def test_workflow_ui_missing_file_does_not_start_service(tmp_path):
    from ojw_evaluation import open_evaluation_ui

    with pytest.raises(FileNotFoundError):
        asyncio.run(open_evaluation_ui(tmp_path / 'absent.json', workspace=tmp_path, port=0))
    assert not (tmp_path / 'evaluation_ui').exists()


def test_workflow_ui_can_return_link_without_browser(tmp_path, monkeypatch):
    from ojw_evaluation import open_evaluation_ui
    import webbrowser

    def unexpected(*args):
        pytest.fail('Browser must not open when disabled')

    monkeypatch.setattr(webbrowser, 'open', unexpected)
    path = tmp_path / 'workflow.json'
    path.write_text(json.dumps({'steps': [{'step_id': '1'}]}))
    url = asyncio.run(open_evaluation_ui(path, port=0, open_browser=False))
    service = running_service(tmp_path / 'evaluation_ui')
    assert urlsplit(url).port == service.port
    assert service.api('/api/runs')['runs'] == []


@pytest.mark.parametrize('strategy,expected_calls',[('zero-shot',1),('taxonomy-guided',7)])
def test_real_worker_roundtrip(service, provider, strategy, expected_calls):
    endpoint,calls=provider
    data=payload(endpoint,temperature=.3,top_p=.8)
    data['strategy']=strategy
    result=wait_job(service, request(service,'/api/runs',data))
    assert result['status']=='completed', result
    assert result['annotations'] is not None
    assert result['trajectory_step_ids'] == ['1']
    assert len(calls)==expected_calls
    assert all(c['temperature']==.3 and c['top_p']==.8 for c in calls)
    assert all(c['model']=='fake-model' for c in calls)
    assert list(service.output_dir.rglob('annotations.json'))
    artifact = Path(result['annotations_file'])
    with urlopen(Request(f'http://127.0.0.1:{service.port}/api/runs/{result["id"]}/annotations',
                         headers={'Authorization': 'Bearer ' + service.token})) as response:
        assert response.read() == artifact.read_bytes()
        assert 'annotations.json' in response.headers['Content-Disposition']
    assert json.loads(artifact.read_text()) != data['trajectory']
    uploaded = Path(result['output_dir']) / 'trajectory.json'
    assert json.loads(uploaded.read_text()) == data['trajectory']
    assert 'hello' in json.dumps(calls[0]['messages'])
    assert not any(b'test/key/not-secret' in p.read_bytes() for p in service.output_dir.rglob('*') if p.is_file())


def test_failure_is_not_success(service, provider):
    data=payload(provider[0]);data['model']='fail-model'
    result=wait_job(service,request(service,'/api/runs',data))
    assert result['status']=='failed'
    assert 'annotations' not in result
    assert 'test-secret' not in json.dumps(result)


def test_auth_origin_and_invalid_upload(service):
    for path in ('/api/config','/api/runs/no-such-run'):
        with pytest.raises(HTTPError) as error:
            request(service,path,token='wrong')
        assert error.value.code==403
    with pytest.raises(HTTPError) as error:
        request(service,'/api/runs',payload('http://localhost/v1'),origin='https://other.invalid')
    assert error.value.code==403
    data=payload('http://localhost/v1');data['trajectory']={'wrong':'shape'}
    with pytest.raises(HTTPError) as error:
        request(service,'/api/runs',data)
    assert error.value.code==400
    assert not service.jobs
    with pytest.raises(ValueError):
        validate_request(payload('http://localhost/v1',output_subdir='../escape'))
    with pytest.raises(ValueError):
        validate_request(payload('http://localhost/v1',temperature=float('nan')))


def test_rail_lifecycle_session_isolation(tmp_path):
    rail=EvaluationRail().configure(scope='invocation', output_dir=tmp_path,port=0,open_browser=False)
    agent=object();rail.init(agent)
    async def run():
        for sid in ('a','a','b'):
            ctx=SimpleNamespace(session=SimpleNamespace(get_session_id=lambda: sid))
            await rail.after_invoke(ctx)
    asyncio.run(run())
    assert len(rail._services)==1
    service = next(iter(rail._services.values()))
    assert len(list((service.output_dir / 'contexts').glob('*.json'))) == 2
    assert request(service, '/api/runs')['runs'] == []
    assert len(list(tmp_path.rglob('launch.json'))) == 1
    with pytest.raises(HTTPError):
        request(service, '/api/config', token='wrong')
    rail.uninit(agent)
    assert request(service, '/api/health')['output_dir'] == str(service.output_dir)
    assert (service.output_dir / 'launch.json').is_file()


def test_partial_categories_are_marked_incomplete(service, provider):
    data=payload(provider[0]);data.update(model='partial-model',strategy='taxonomy-guided')
    result=wait_job(service,request(service,'/api/runs',data))
    assert result['status']=='partial',result
    assert result['exit_code']==1
    assert result['annotations'] is not None


def test_worker_timeout_is_reported(tmp_path, provider):
    service=EvaluationService(tmp_path,port=0,worker_timeout=.001)
    try:
        result=wait_job(service,request(service,'/api/runs',payload(provider[0])))
        assert result['status']=='failed'
        assert 'timed out' in result['error']
    finally:
        service.close()


def test_no_parallel_jobs_and_shutdown_cancels(tmp_path):
    service=EvaluationService(tmp_path,port=0)
    job=service.submit(validate_request(payload('http://127.0.0.1:1/v1')))
    with pytest.raises(RuntimeError,match='already running'):
        service.submit(validate_request(payload('http://127.0.0.1:1/v1')))
    service.close()
    assert all(not thread.is_alive() for thread in service._threads.values())
    assert service.jobs[job['id']]['status']=='cancelled'


def test_explicit_rail_on_real_react_agent(tmp_path):
    from openjiuwen.core.foundation.llm import AssistantMessage
    from openjiuwen.core.single_agent import AgentCard
    from openjiuwen.core.single_agent.agents.react_agent import ReActAgent, ReActAgentConfig
    class Model:
        async def invoke(self, **kwargs):
            return AssistantMessage(content='done',finish_reason='stop')
    async def run():
        agent=ReActAgent(AgentCard(id='eval-sdk',name='eval-sdk'))
        agent.configure(ReActAgentConfig(max_iterations=2));agent.set_llm(Model())
        rail=EvaluationRail().configure(scope='invocation', output_dir=tmp_path,port=0,open_browser=False)
        await agent.register_rail(rail)
        try:
            await agent.invoke({'query':'test','conversation_id':'sdk-session'})
            assert len(rail._services)==1,rail.last_error
            assert not request(next(iter(rail._services.values())), '/api/runs')['runs']
        finally:
            await agent.unregister_rail(rail)
    asyncio.run(run())


def test_defaults_match_cli_and_config(service, monkeypatch):
    from ojw_evaluation.core.config import DEFAULT_PARAMETERS, evaluation_defaults
    from ojw_evaluation.cli import build_parser
    monkeypatch.setitem(DEFAULT_PARAMETERS, 'client', 'mlops')
    monkeypatch.setitem(DEFAULT_PARAMETERS, 'taxonomy', 'mast')
    monkeypatch.setitem(DEFAULT_PARAMETERS, 'temperature', .4)
    config = request(service, '/api/config')
    expected = evaluation_defaults(); expected.pop('output_subdir')
    assert config['defaults'] == expected
    assert 'api_key' not in json.dumps(config)
    assert config['defaults']['client'] == 'mlops'
    assert config['defaults']['taxonomy'] == 'mast'
    assert config['defaults']['temperature'] == .4
    cli = build_parser().parse_args(['run'])
    for field in ('client', 'strategy', 'taxonomy', 'dedup', 'temperature', 'top_p'):
        assert getattr(cli, field) == config['defaults'][field]
    parsed = validate_request({'api_key': 'test', 'trajectory': {'steps': [{'step_id': '1'}]}})
    for field, value in config['defaults'].items():
        assert parsed[field] == value
    other = validate_request({'client': 'openrouter', 'api_key': 'test', 'trajectory': {'steps': [{'step_id': '1'}]}})
    assert other['model'] == config['providers']['openrouter']['model']


def test_generated_file_requires_session_token_and_job(service, provider):
    result = wait_job(service, request(service, '/api/runs', payload(provider[0])))
    route = f'/api/runs/{result["id"]}/annotations'
    with pytest.raises(HTTPError) as error:
        request(service, route, token='wrong')
    assert error.value.code == 403
    with pytest.raises(HTTPError) as error:
        request(service, '/api/runs/' + '0' * 32 + '/annotations')
    assert error.value.code == 404


def test_server_file_selection_runs_evaluator(service, provider):
    folder = service.output_dir / 'trajectories'
    folder.mkdir()
    source = folder / 'input.json'
    original = json.dumps({'steps': [{'step_id': '1', 'text': 'server selected input'}]}, indent=2)
    source.write_text(original)
    roots = request(service, '/api/config')['file_roots']
    assert any(root['id'] == 'results' for root in roots)
    listing = request(service, '/api/files?root=results&path=trajectories')
    assert listing['entries'][0]['path'] == 'trajectories/input.json'
    assert 'server selected input' not in json.dumps(listing)
    data = payload(provider[0])
    del data['trajectory']
    data['server_file'] = {'root': 'results', 'path': 'trajectories/input.json'}
    result = wait_job(service, request(service, '/api/runs', data))
    assert result['status'] == 'completed', result
    assert source.read_text() == original
    assert json.loads((Path(result['output_dir']) / 'trajectory.json').read_text()) == json.loads(original)
    assert 'server selected input' in json.dumps(provider[1][0]['messages'])
    assert Path(result['annotations_file']).is_file()


def test_picker_defaults_to_launch_directory(tmp_path, monkeypatch):
    from contextvars import Context
    launch = tmp_path / 'application'
    launch.mkdir()
    (launch / 'input.json').write_text('{"steps": [{"step_id": "1"}]}')
    monkeypatch.chdir(launch)
    service = Context().run(EvaluationService, tmp_path / 'ui', port=0)
    monkeypatch.chdir(tmp_path)
    try:
        config = request(service, '/api/config')
        directory = config['default_directory']
        root = next(r for r in config['file_roots'] if r['id'] == directory['root'])
        assert Path(root['path']) / directory['path'] == launch
        listing = request(service, '/api/files?root=' + directory['root'])
        assert [entry['name'] for entry in listing['entries']] == ['input.json']
        assert config['default_file'] is None
    finally:
        service.close()


@pytest.mark.parametrize('trace_state', ['pending', 'deleted', 'available'])
def test_rail_picker_uses_session_launch_directory(tmp_path, trace_state):
    from openjiuwen.core.sys_operation.cwd import init_cwd, set_cwd
    launch = tmp_path / 'application'
    launch.mkdir()
    moved = tmp_path / 'other-working-directory'
    moved.mkdir()
    trace = tmp_path / 'traces' / 'input.json'
    if trace_state == 'available':
        trace.parent.mkdir()
        trace.write_text('{"steps": [{"step_id": "1"}]}')
    agent = SimpleNamespace(card=SimpleNamespace(id='test', name='Test'))
    rail = EvaluationRail().configure(scope='invocation', output_dir=tmp_path, port=0, open_browser=False)
    rail.init(agent)
    async def run():
        init_cwd(str(launch), workspace=str(tmp_path))
        ctx = SimpleNamespace(session=SimpleNamespace(get_session_id=lambda: 'current-session'))
        await rail.before_invoke(ctx)
        receipt = agent._ojw_trace_handoff.get()
        if trace_state != 'pending':
            record = json.loads(receipt.read_text())
            record['source_path'] = str(trace)
            receipt.write_text(json.dumps(record))
        set_cwd(str(moved))
        await rail.after_invoke(ctx)
    try:
        asyncio.run(run())
        service, config = rail_config(rail)
        directory = config['default_directory']
        root = next(r for r in config['file_roots'] if r['id'] == directory['root'])
        expected = trace.parent if trace_state == 'available' else launch
        assert Path(root['path']) / directory['path'] == expected
        assert bool(config['default_file']) == (trace_state == 'available')
        assert config['context']['launch_dir'] == str(launch)
        assert request(service, '/api/runs')['runs'] == []
    finally:
        rail.uninit(agent)


def test_deleted_standalone_default_falls_back_to_launch_directory(tmp_path):
    service = EvaluationService(tmp_path / 'ui', port=0)
    try:
        source = tmp_path / 'trace.json'
        source.write_text('{"steps": [{"step_id": "1"}]}')
        service.set_default_file(source)
        source.unlink()
        config = request(service, '/api/config')
        assert config['default_file'] is None
        assert config['default_directory'] == {'root': 'launch', 'path': ''}
    finally:
        service.close()


@pytest.mark.parametrize('pinned', [False, True])
def test_missing_invocation_keeps_manual_file_selection(service, provider, pinned):
    from ojw_evaluation.state import write_json
    context_id, invocation_id = 'a' * 32, 'b' * 32
    folder = service.output_dir / 'application'
    folder.mkdir()
    source = folder / 'input.json'
    source.write_text('{"steps": [{"step_id": "1", "text": "manual input"}]}')
    write_json(service.output_dir / 'contexts' / f'{context_id}.json',
               {'id': context_id, 'session_id': 'current', 'launch_dir': str(folder),
                'latest_invocation': invocation_id})
    route = '/api/config?context=' + context_id
    if pinned:
        route += '&invocation=' + invocation_id
    config = request(service, route)
    assert config['default_file'] is None
    assert config['invocation'] is None
    directory = config['default_directory']
    listing = request(service, '/api/files?root=' + directory['root'])
    assert [entry['name'] for entry in listing['entries']] == ['input.json']
    selection = {'root': directory['root'], 'path': 'input.json'}
    info = request(service, '/api/inspect', {'context_id': context_id, 'server_file': selection})
    assert info['source_path'] == str(source)
    data = payload(provider[0])
    del data['trajectory']
    data.update(context_id=context_id, server_file=selection)
    result = wait_job(service, request(service, '/api/runs', data))
    assert result['status'] == 'completed', result
    assert Path(result['output_dir']).parent == folder / 'evaluation'
    assert len(provider[1]) == 1


def test_missing_pinned_invocation_does_not_select_another_trace(service):
    from ojw_evaluation.state import write_json
    context_id, current_id, missing_id = 'a' * 32, 'b' * 32, 'c' * 32
    source = service.output_dir / 'trace.json'
    source.write_text('{"steps": [{"step_id": "1"}]}')
    write_json(service.output_dir / 'contexts' / f'{context_id}.json',
               {'id': context_id, 'session_id': 'current', 'latest_invocation': current_id})
    write_json(service.output_dir / 'invocations' / f'{current_id}.json',
               {'id': current_id, 'context_id': context_id, 'source_path': str(source)})
    config = request(service, f'/api/config?context={context_id}&invocation={missing_id}')
    assert config['default_file'] is None
    assert config['latest_file']['path'] == 'trace.json'
    assert config['default_directory'] == {'root': 'launch', 'path': ''}
    # An existing receipt from another context remains invalid.
    write_json(service.output_dir / 'invocations' / f'{missing_id}.json',
               {'id': missing_id, 'context_id': 'd' * 32, 'source_path': str(source)})
    with pytest.raises(HTTPError) as error:
        request(service, f'/api/config?context={context_id}&invocation={missing_id}')
    assert error.value.code == 400


def test_server_picker_blocks_escape_and_links(service, tmp_path):
    from ojw_evaluation.server_files import ServerFileError
    folder = service.output_dir
    outside = tmp_path.parent / 'outside-picker.json'
    outside.write_text('{"step_id":"1"}')
    (folder / 'linked.json').symlink_to(outside)
    (folder / 'linked-dir').symlink_to(tmp_path.parent, target_is_directory=True)
    (folder / '.hidden.json').write_text('{}')
    (folder / 'notes.txt').write_text('not JSON')
    (folder / 'visible.json').write_text('{"step_id":"1"}')
    listing = request(service, '/api/files?root=results')
    assert [entry['name'] for entry in listing['entries']] == ['visible.json']
    with pytest.raises(HTTPError) as error:
        request(service, '/api/files?root=results', token='wrong')
    assert error.value.code == 403
    for path in ('../outside-picker.json', str(outside), 'linked.json',
                 'linked-dir/outside-picker.json', '.hidden.json', 'notes.txt'):
        data = payload('http://localhost/v1')
        del data['trajectory']
        data['server_file'] = {'root': 'results', 'path': path}
        with pytest.raises(HTTPError) as error:
            request(service, '/api/runs', data)
        assert error.value.code == 400
    with pytest.raises(ServerFileError):
        service.files.read_json({'root': 'unknown', 'path': 'visible.json'}, max_bytes=100)
    assert not service.jobs


def test_server_file_errors_and_pagination(service):
    root = service.output_dir
    for number in range(201):
        (root / f'{number:03}.json').write_text('{"step_id":"1"}')
    first = request(service, '/api/files?root=results')
    assert len(first['entries']) == 200
    assert first['next_offset'] == 200
    second = request(service, '/api/files?root=results&offset=200')
    assert len(second['entries']) == 1 and second['next_offset'] is None
    data = payload('http://localhost/v1')
    data['server_file'] = {'root': 'results', 'path': '000.json'}
    with pytest.raises(HTTPError) as error:
        request(service, '/api/runs', data)  # two input sources
    assert error.value.code == 400
    del data['trajectory']
    (root / '000.json').unlink()
    with pytest.raises(HTTPError) as error:
        request(service, '/api/runs', data)
    assert error.value.code == 400
    (root / '000.json').write_text('invalid json')
    with pytest.raises(HTTPError) as error:
        request(service, '/api/runs', data)
    assert error.value.code == 400
    from ojw_evaluation.server_files import ServerFileError
    with pytest.raises(ServerFileError, match='exceeds'):
        service.files.read_json({'root': 'results', 'path': '001.json'}, max_bytes=1)


def test_adjacent_results_outside_service_root(tmp_path, provider):
    from hashlib import sha256
    folder = tmp_path / 'traces'
    folder.mkdir()
    source = folder / 'trace.json'
    original = json.dumps({'steps': [{'step_id': '1', 'text': 'chosen trace'}]}, indent=2).encode()
    source.write_bytes(original)
    service = EvaluationService(tmp_path / 'ui', port=0, browse_roots={'traces': folder})
    try:
        service.set_default_file(source)
        assert request(service, '/api/config')['default_file']['path'] == source.name
        data = payload(provider[0]); del data['trajectory']
        data['server_file'] = {'root': 'traces', 'path': source.name}
        outputs = []
        for _ in range(2):
            result = wait_job(service, request(service, '/api/runs', data))
            assert result['status'] == 'completed', result
            output = Path(result['output_dir'])
            assert output.parent == folder / 'evaluation'
            assert re.fullmatch(r'\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_ui(?:_\d+)?', output.name)
            metadata = json.loads((output / 'source.json').read_text())
            assert metadata['source_path'] == str(source)
            assert metadata['source_sha256'] == sha256(original).hexdigest()
            assert metadata['snapshot_sha256'] == sha256((output / 'trajectory.json').read_bytes()).hexdigest()
            assert request(service, f'/api/runs/{result["id"]}/annotations') == result['annotations']
            outputs.append(output)
        assert outputs[0] != outputs[1]
        assert source.read_bytes() == original
        (folder / 'other.json').write_bytes(original)
        data['server_file']['path'] = 'other.json'
        result = wait_job(service, request(service, '/api/runs', data))
        assert json.loads((Path(result['output_dir']) / 'source.json').read_text())['source_path'] == str(folder / 'other.json')
    finally:
        service.close()


def test_combined_react_current_trace(tmp_path):
    from openjiuwen.core.foundation.llm import AssistantMessage
    from openjiuwen.core.single_agent import AgentCard
    from openjiuwen.core.single_agent.agents.react_agent import ReActAgent, ReActAgentConfig
    from ojw_observability.rail import ObservabilityRail
    class Model:
        async def invoke(self, **kwargs):
            return AssistantMessage(content='done', finish_reason='stop')
    async def run():
        agent = ReActAgent(AgentCard(id='combined', name='combined'))
        agent.configure(ReActAgentConfig(max_iterations=2)); agent.set_llm(Model())
        observer = ObservabilityRail().configure(output_dir=tmp_path / 'traces')
        evaluator = EvaluationRail().configure(scope='invocation', output_dir=tmp_path, port=0, open_browser=False)
        await agent.register_rail(observer)
        await agent.register_rail(evaluator)
        selected = []
        try:
            for _ in range(2):
                await agent.invoke({'query': 'test', 'conversation_id': 'same-session'})
                service, config = rail_config(evaluator)
                selection = config['default_file']
                assert selection, config
                root = next(r['path'] for r in config['file_roots'] if r['id'] == selection['root'])
                path = Path(root) / selection['path']
                assert path.is_file() and path.parent == tmp_path / 'traces'
                selected.append(path)
            assert selected[0] != selected[1]
        finally:
            await agent.unregister_rail(evaluator)
            await agent.unregister_rail(observer)
    asyncio.run(run())


def test_preflight_rejects_annotations_and_unwritable_output(service):
    data = {'trajectory': {'annotations': [{'step_id': '1', 'failures': []}]}}
    with pytest.raises(HTTPError) as error:
        request(service, '/api/inspect', data)
    assert 'not an evaluation' in json.load(error.value)['error']
    folder = service.output_dir / 'blocked'
    folder.mkdir(); (folder / 'evaluation').write_text('not a directory')
    (folder / 'input.json').write_text(json.dumps({'steps': [{'step_id': '1'}]}))
    with pytest.raises(HTTPError) as error:
        request(service, '/api/inspect', {'server_file': {'root': 'results', 'path': 'blocked/input.json'}})
    assert 'Cannot write' in json.load(error.value)['error']
    assert not service.jobs


@pytest.mark.parametrize('path', ['/api/inspect', '/api/runs'])
@pytest.mark.parametrize('metadata', [None, [], 'agent', 7, True],
                         ids=['null', 'list', 'string', 'number', 'boolean'])
def test_malformed_agent_metadata_returns_json_bad_request(service, path, metadata):
    data = payload('http://127.0.0.1:1/v1')
    data['trajectory']['agent_metadata'] = metadata
    with pytest.raises(HTTPError) as error:
        request(service, path, data)
    assert error.value.code == 400
    assert error.value.headers['Content-Type'].startswith('application/json')
    message = json.load(error.value)['error']
    assert 'agent_metadata' in message and 'object' in message
    assert not service.jobs
    assert request(service, '/api/health')['output_dir'] == str(service.output_dir)


@pytest.mark.parametrize('metadata', [{}, {'agent_id': 'agent-1', 'agent_name': 'Agent one'}])
def test_optional_agent_metadata_inspects_successfully(service, metadata):
    trajectory = {'steps': [{'step_id': '1'}]}
    without_metadata = request(service, '/api/inspect', {'trajectory': trajectory})
    assert without_metadata['format'] == 'Step trajectory'
    assert without_metadata['step_count'] == 1
    assert without_metadata['agent_id'] is None
    assert without_metadata['agent_name'] is None
    trajectory['agent_metadata'] = metadata
    with_metadata = request(service, '/api/inspect', {'trajectory': trajectory})
    assert with_metadata['format'] == 'OpenJiuwen agent trace'
    assert with_metadata['step_count'] == 1
    assert with_metadata['agent_id'] == metadata.get('agent_id')
    assert with_metadata['agent_name'] == metadata.get('agent_name')


def test_changed_input_requires_review(service, provider):
    source = service.output_dir / 'input.json'
    source.write_text(json.dumps({'steps': [{'step_id': '1', 'text': 'before'}]}))
    selection = {'root': 'results', 'path': source.name}
    info = request(service, '/api/inspect', {'server_file': selection})
    assert info['step_count'] == 1
    source.write_text(json.dumps({'steps': [{'step_id': '1', 'text': 'after'}]}))
    data = payload(provider[0]); del data['trajectory']
    data.update(server_file=selection, expected_sha256=info['sha256'])
    with pytest.raises(HTTPError) as error:
        request(service, '/api/runs', data)
    assert 'file changed' in json.load(error.value)['error']
    assert not service.jobs and not provider[1]


def test_completed_job_restored_after_restart(tmp_path, provider):
    first = EvaluationService(tmp_path, port=0)
    try:
        result = wait_job(first, request(first, '/api/runs', payload(provider[0])))
        token = first.token
        assert result['status'] == 'completed'
    finally:
        first.close()
    second = EvaluationService(tmp_path, port=0)
    try:
        assert second.token == token
        assert request(second, '/api/runs')['runs'][0]['id'] == result['id']
        restored = request(second, '/api/runs/' + result['id'])
        assert restored['input'] == result['input']
        assert request(second, '/api/runs/' + result['id'] + '/annotations') == result['annotations']
        assert len(provider[1]) == 1
    finally:
        second.close()


def test_two_agents_share_service_and_keep_contexts(tmp_path):
    rails = []
    async def run():
        for agent_id in ('first', 'second'):
            agent = SimpleNamespace(card=SimpleNamespace(id=agent_id, name=agent_id))
            rail = EvaluationRail().configure(scope='invocation', output_dir=tmp_path, port=0, open_browser=False)
            rail.init(agent); rails.append((rail, agent))
            ctx = SimpleNamespace(session=SimpleNamespace(get_session_id=lambda: 'same-session'))
            await rail.before_invoke(ctx)
            await rail.after_invoke(ctx)
    asyncio.run(run())
    first, first_context = rail_config(rails[0][0]); second, second_context = rail_config(rails[1][0])
    assert first.port == second.port
    assert first_context['context']['id'] != second_context['context']['id']
    assert first_context['context']['agent_id'] == 'first'
    assert second_context['context']['agent_id'] == 'second'
    assert first_context['invocation']['id'] != second_context['invocation']['id']
    rails[0][0].uninit(rails[0][1])
    assert request(second, '/api/health')
    assert (second.output_dir / 'launch.json').exists()
    rails[1][0].uninit(rails[1][1])
    assert request(first, '/api/health')


def test_standalone_process_can_exit_before_evaluation(tmp_path, provider, runtime_bootstrap):
    import subprocess
    import sys
    code = '''
import asyncio
from pathlib import Path
from types import SimpleNamespace
from ojw_evaluation.rail import EvaluationRail
import sys
async def main():
    rail = EvaluationRail().configure(scope='invocation', output_dir=Path(sys.argv[1]), port=0, open_browser=False)
    agent = SimpleNamespace(card=SimpleNamespace(id='script-agent', name='script-agent'))
    rail.init(agent)
    ctx = SimpleNamespace(session=SimpleNamespace(get_session_id=lambda:'script-session'))
    await rail.before_invoke(ctx)
    await rail.after_invoke(ctx)
    rail.uninit(agent)
asyncio.run(main())
'''
    child = subprocess.run([sys.executable, '-I', '-c', runtime_bootstrap + code, str(tmp_path)], capture_output=True, text=True, timeout=30)
    assert child.returncode == 0, child.stderr
    handle = running_service(tmp_path / 'evaluation_ui')
    assert handle is not None
    result = wait_job(handle, request(handle, '/api/runs', payload(provider[0])))
    assert result['status'] == 'completed'


def test_effective_parameters_and_provider_errors(service, provider):
    data = payload(provider[0]); data.update(model='openai/gpt-5.5', temperature=.4, top_p=.8)
    result = wait_job(service, request(service, '/api/runs', data))
    assert result['status'] == 'completed'
    assert result['parameters']['taxonomy'] is None
    assert result['parameters']['dedup'] == 'none'
    assert result['parameters']['temperature'] is None
    assert result['parameters']['top_p'] is None
    assert 'temperature' not in provider[1][0] and 'top_p' not in provider[1][0]
    data['model'] = 'fail-model'
    result = wait_job(service, request(service, '/api/runs', data))
    assert 'API key or access' in result['error']


def test_retry_submission_does_not_duplicate_paid_request(service, provider):
    data = payload(provider[0]); data['request_id'] = 'a' * 32
    first = request(service, '/api/runs', data)
    second = request(service, '/api/runs', data)
    assert first['id'] == second['id'] == data['request_id']
    assert wait_job(service, first)['status'] == 'completed'
    third = request(service, '/api/runs', data)
    assert third['status'] == 'completed'
    assert len(provider[1]) == 1


def test_interrupted_state_is_restored_without_resubmission(tmp_path):
    from ojw_evaluation.state import write_json
    output = tmp_path / 'evaluation' / ('b' * 32)
    output.mkdir(parents=True)
    write_json(tmp_path / 'jobs' / ('b' * 32 + '.json'), {'id': 'b' * 32, 'status': 'running',
        'context_id': None, 'output_dir': str(output), 'created_at': 0, 'parameters': {}})
    service = EvaluationService(tmp_path, port=0)
    try:
        restored = request(service, '/api/runs/' + 'b' * 32)
        assert restored['status'] == 'interrupted'
        assert not service._processes
        assert not service._threads
    finally:
        service.close()


@pytest.mark.parametrize('filename', ['failure_detector_result.json', 'result.json'])
def test_saved_worker_outcome_is_restored(tmp_path, filename):
    from ojw_evaluation.state import write_json
    output = tmp_path / 'evaluation' / '2026-10-28_14-32-05_ui'
    output.mkdir(parents=True)
    job_id = 'c' * 32
    write_json(tmp_path / 'jobs' / f'{job_id}.json', {
        'id': job_id, 'status': 'running', 'context_id': None,
        'output_dir': str(output), 'created_at': 0, 'parameters': {}})
    write_json(output / filename, {'status': 'completed', 'annotations': []})
    service = EvaluationService(tmp_path, port=0)
    try:
        restored = request(service, '/api/runs/' + job_id)
        assert restored['status'] == 'completed'
        assert restored['annotations'] == []
        assert not service._processes
    finally:
        service.close()
