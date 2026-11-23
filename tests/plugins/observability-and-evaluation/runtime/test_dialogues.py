"""Dialogue progression through real SDK agents and the independent UI service."""
import asyncio
import json
from pathlib import Path
import time
from urllib.parse import parse_qs, urlsplit
import webbrowser
from uuid import uuid4

import pytest
from openjiuwen.core.foundation.llm import AssistantMessage
from openjiuwen.core.single_agent import AgentCard
from openjiuwen.core.single_agent.agents.react_agent import ReActAgent, ReActAgentConfig
from openjiuwen.core.sys_operation.cwd import init_cwd

from ojw_evaluation import EvaluationRail
from ojw_evaluation.runtime import running_service
from ojw_observability import ObservabilityRail


class Model:
    async def invoke(self, *, messages, **kwargs):
        query = next(m.content for m in reversed(messages) if m.role == 'user')
        return AssistantMessage(content='Answer: ' + str(query), finish_reason='stop')


@pytest.fixture
def dialogue_agent(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(webbrowser, 'open', lambda url: opened.append(url) or True)
    init_cwd(str(tmp_path), workspace=str(tmp_path))
    agent = ReActAgent(AgentCard(id=uuid4().hex, name='dialogue-agent'))
    agent.configure(ReActAgentConfig(max_iterations=3))
    agent.set_llm(Model())
    observer = ObservabilityRail().configure(output_dir=tmp_path / 'raw')
    evaluator = EvaluationRail().configure(output_dir=tmp_path, port=0)

    async def attach():
        await agent.register_rail(observer)
        await agent.register_rail(evaluator)
    asyncio.run(attach())
    yield agent, evaluator, opened
    handle = running_service(tmp_path / 'evaluation_ui')
    if handle:
        handle.api('/api/shutdown', {})

    async def detach():
        await agent.unregister_rail(evaluator)
        await agent.unregister_rail(observer)
    asyncio.run(detach())


def metadata_path(workspace, sid='dialogue-a'):
    return workspace / 'observability_traces' / sid / 'metadata.json'


def wait_metadata(path, *, revision=None, status=None, turns=None):
    deadline = time.monotonic() + 10
    value = {}
    while time.monotonic() < deadline:
        if path.exists():
            value = json.loads(path.read_text())
            if ((revision is None or value.get('revision') == revision)
                    and (status is None or value.get('status') == status)
                    and (turns is None or value.get('turn_count') == turns)):
                return value
        time.sleep(.05)
    pytest.fail(f'Dialogue did not reach revision={revision}, status={status}; keys={list(value)}')


def test_dialogue_updates_without_browser_or_evaluation(tmp_path, dialogue_agent):
    agent, evaluator, opened = dialogue_agent
    asyncio.run(agent.invoke({'query': 'first', 'conversation_id': 'dialogue-a'}))
    first = wait_metadata(metadata_path(tmp_path), revision=1, status='ready')
    original = {p: p.read_bytes() for p in (tmp_path / 'raw').glob('agent_*.json')}
    asyncio.run(agent.invoke({'query': 'second', 'conversation_id': 'dialogue-a'}))
    latest = wait_metadata(metadata_path(tmp_path), revision=2, status='ready')
    assert latest['id'] == first['id']
    assert latest['turn_count'] == latest['trace_count'] == 2
    assert all(path.read_bytes() == content for path, content in original.items())
    merged = json.loads(Path(latest['trajectory_path']).read_text())
    assert merged['trace_metadata']['system_architecture'] == 'shared_context'
    assert [a['result']['agent_output'] for a in merged['agents'].values()] == ['Answer: first', 'Answer: second']
    assert opened == []
    params = parse_qs(urlsplit(evaluator.last_url).fragment)
    assert params['context'] == [latest['id']]
    assert 'invocation' not in params
    handle = running_service(tmp_path / 'evaluation_ui')
    config = handle.api('/api/config?context=' + latest['id'])
    assert config['default_file']['path'] == 'dialogue.json'
    assert config['dialogue']['revision'] == 2
    assert handle.api('/api/runs?context=' + latest['id'])['runs'] == []


def test_dialogues_do_not_mix_sessions_and_repeated_sync_is_idempotent(tmp_path, dialogue_agent):
    agent, _, _ = dialogue_agent
    for sid, query in [('dialogue-a', 'a1'), ('dialogue-b', 'b1'), ('dialogue-a', 'a2')]:
        asyncio.run(agent.invoke({'query': query, 'conversation_id': sid}))
    first = wait_metadata(metadata_path(tmp_path), turns=2, status='ready')
    second = wait_metadata(metadata_path(tmp_path, 'dialogue-b'), revision=1, status='ready')
    assert first['id'] != second['id']
    assert first['turn_count'] == 2 and second['turn_count'] == 1
    handle = running_service(tmp_path / 'evaluation_ui')
    for _ in range(3):
        assert handle.api('/api/config?context=' + first['id'])['dialogue']['revision'] == first['revision']
    choices = handle.api('/api/dialogues')['dialogues']
    assert {item['id'] for item in choices} == {first['id'], second['id']}
    merged = Path(first['trajectory_path']).read_text()
    assert 'Answer: b1' not in merged


def test_open_saved_dialogue_restarts_service_without_agent(tmp_path, dialogue_agent):
    from ojw_evaluation.opening import open_saved_dialogue
    agent, _, opened = dialogue_agent
    asyncio.run(agent.invoke({'query': 'saved', 'conversation_id': 'dialogue-a'}))
    metadata = wait_metadata(metadata_path(tmp_path), turns=1, status='ready')
    handle = running_service(tmp_path / 'evaluation_ui')
    handle.api('/api/shutdown', {})
    deadline = time.monotonic() + 10
    while running_service(tmp_path / 'evaluation_ui') and time.monotonic() < deadline:
        time.sleep(.1)
    import subprocess
    import sys
    import ojw_evaluation.opening as opening
    command = [sys.executable, '-I', str(Path(opening.__file__).with_name('open_dialogue.py')),
               str(metadata_path(tmp_path)), '--port', '0']
    cli = subprocess.run(command, capture_output=True, text=True, timeout=25)
    assert cli.returncode == 0, cli.stderr
    url = next(line for line in cli.stdout.splitlines() if line.startswith('http://127.0.0.1:'))
    assert parse_qs(urlsplit(url).fragment)['context'] == [metadata['id']]
    result = open_saved_dialogue(metadata_path(tmp_path), port=0)
    assert result['success'] and result['context_id'] == metadata['id']
    assert result['revision'] == metadata['revision']
    assert opened == []
    handle = running_service(tmp_path / 'evaluation_ui')
    assert handle.api('/api/config?context=' + metadata['id'])['default_file']['path'] == 'dialogue.json'
    assert handle.api('/api/runs?context=' + metadata['id'])['runs'] == []


def test_tool_requires_actual_session_and_keeps_workspace(tmp_path, dialogue_agent):
    from ojw_evaluation.opening import open_current_dialogue
    from types import SimpleNamespace
    from ojw_evaluation.dialogues import SESSION_POINTER
    agent, _, opened = dialogue_agent
    assert not asyncio.run(open_current_dialogue(None))['success']
    asyncio.run(agent.invoke({'query': 'saved', 'conversation_id': 'dialogue-a'}))
    metadata = wait_metadata(metadata_path(tmp_path), turns=1, status='ready')
    pointer = {'session_id': 'dialogue-a', 'context_id': metadata['id'], 'workspace': str(tmp_path)}
    other = tmp_path / 'changed'
    other.mkdir()
    init_cwd(str(other), workspace=str(other))
    session = SimpleNamespace(get_session_id=lambda: 'dialogue-a', get_state=lambda key: pointer if key == SESSION_POINTER else None)
    result = asyncio.run(open_current_dialogue(session))
    assert result['success'] and result['metadata_path'] == str(metadata_path(tmp_path))
    assert opened == [] and not (other / 'evaluation_ui').exists()
    session.get_session_id = lambda: 'unrelated'
    assert not asyncio.run(open_current_dialogue(session))['success']


from test_ui import provider, payload, wait_job, request


def test_evaluation_pins_dialogue_revision_and_manual_override(tmp_path, dialogue_agent, provider):
    agent, _, _ = dialogue_agent
    asyncio.run(agent.invoke({'query': 'first', 'conversation_id': 'dialogue-a'}))
    first = wait_metadata(metadata_path(tmp_path), turns=1, status='ready')
    service = running_service(tmp_path / 'evaluation_ui')
    config = service.api('/api/config?context=' + first['id'])
    selection = {'context_id': first['id'], 'server_file': config['default_file']}
    preview = service.api('/api/inspect', selection)
    assert preview['dialogue']['revision'] == first['revision']
    data = payload(provider[0]); data.pop('trajectory')
    data.update(selection, expected_sha256=preview['sha256'])
    job = service.api('/api/runs', data)
    snapshot = Path(job['output_dir']) / 'trajectory.json'
    original = snapshot.read_bytes()
    asyncio.run(agent.invoke({'query': 'second', 'conversation_id': 'dialogue-a'}))
    wait_metadata(metadata_path(tmp_path), turns=2, status='ready')
    result = wait_job(service, job)
    assert result['status'] == 'completed', result
    assert result['input']['dialogue']['turn_count'] == 1
    assert result['input']['dialogue']['revision'] == first['revision']
    assert snapshot.read_bytes() == original
    assert json.loads((snapshot.parent / 'source.json').read_text())['dialogue']['revision'] == first['revision']
    assert 'Answer: second' not in original.decode()
    # Another selected file keeps its own output folder and is not labelled the whole dialogue.
    raw = Path(first['sources'][0]['path'])
    listing = service.api('/api/config?context=' + first['id'])
    workspace_root = next(r['id'] for r in listing['file_roots'] if r['path'] == str(tmp_path))
    data.update(server_file={'root': workspace_root, 'path': str(raw.relative_to(tmp_path))})
    data.pop('expected_sha256')
    override = wait_job(service, service.api('/api/runs', data))
    assert override['status'] == 'completed'
    assert override['input']['dialogue'] is None
    assert Path(override['output_dir']).parent == raw.parent / 'evaluation'


def test_missing_trace_never_labels_old_merge_as_complete(tmp_path, dialogue_agent):
    from urllib.error import HTTPError
    agent, _, _ = dialogue_agent
    asyncio.run(agent.invoke({'query': 'first', 'conversation_id': 'dialogue-a'}))
    first = wait_metadata(metadata_path(tmp_path), turns=1, status='ready')
    service = running_service(tmp_path / 'evaluation_ui')
    selection = service.api('/api/config?context=' + first['id'])['default_file']
    target = Path(first['trajectory_path']); original = target.read_bytes()
    raw = Path(first['sources'][0]['path']); saved = raw.read_bytes(); raw.unlink()
    failed = wait_metadata(metadata_path(tmp_path), status='incomplete')
    assert failed['revision'] == first['revision'] and target.read_bytes() == original
    assert service.api('/api/config?context=' + first['id'])['default_file'] is None
    with pytest.raises(HTTPError):
        service.api('/api/inspect', {'context_id': first['id'], 'server_file': selection})
    raw.write_bytes(saved)
    recovered = wait_metadata(metadata_path(tmp_path), status='ready')
    assert recovered['turn_count'] == 1


def test_multiple_traces_and_legacy_receipts_rebuild_after_restart(tmp_path, dialogue_agent):
    from ojw_evaluation.dialogues import register_dialogue, sync_dialogue
    from ojw_evaluation.state import write_json
    agent, _, _ = dialogue_agent
    for query in ['one', 'two']:
        asyncio.run(agent.invoke({'query': query, 'conversation_id': 'dialogue-a'}))
    original = wait_metadata(metadata_path(tmp_path), turns=2, status='ready')
    root = tmp_path / 'restored/evaluation_ui'
    context = register_dialogue(root, 'dialogue-a', launch_dir=tmp_path)
    paths = [s['path'] for s in original['sources']]
    receipt = root / 'invocations' / ('a'*32 + '.json')
    record = {'id': 'a'*32, 'session_id': 'dialogue-a', 'context_id': 'b'*32,
              'created_at': 1, 'source_paths': paths, 'source_path': paths[-1], 'closed': False}
    write_json(receipt, record)
    merged = sync_dialogue(root, context['id'])
    assert merged['turn_count'] == 1 and merged['trace_count'] == 2
    assert merged['pending_turn_count'] == 1 and merged['status'] == 'ready'
    record['closed'] = True; write_json(receipt, record)
    closed = sync_dialogue(root, context['id'])
    assert closed['revision'] == merged['revision'] and closed['pending_turn_count'] == 0
    target = Path(merged['trajectory_path']); target.unlink()
    rebuilt = sync_dialogue(root, context['id'])
    assert rebuilt['status'] == 'ready' and rebuilt['trace_count'] == 2
    assert len(json.loads(target.read_text())['agents']) == 2
    # Old receipts contain only source_path; duplicated paths are not merged twice.
    write_json(root / 'invocations' / ('c'*32 + '.json'),
               {'id': 'c'*32, 'session_id': 'dialogue-a', 'created_at': 2, 'source_path': paths[0]})
    assert sync_dialogue(root, context['id'])['revision'] == rebuilt['revision']
    # A malformed original keeps the last good combined file but marks it incomplete.
    raw = Path(paths[0]); saved = raw.read_bytes(); before = target.read_bytes()
    raw.write_text('{broken')
    failed = sync_dialogue(root, context['id'])
    assert failed['status'] == 'incomplete' and target.read_bytes() == before
    raw.write_bytes(saved)
    assert sync_dialogue(root, context['id'])['status'] == 'ready'


def test_workflow_folder_name_cannot_join_dialogue(tmp_path, dialogue_agent):
    from ojw_evaluation import open_evaluation_ui
    from ojw_evaluation.dialogues import sync_dialogue
    agent, _, _ = dialogue_agent
    asyncio.run(agent.invoke({'query': 'one', 'conversation_id': 'dialogue-a'}))
    original = wait_metadata(metadata_path(tmp_path), turns=1, status='ready')
    source = tmp_path / 'dialogue-a/workflow.json'; source.parent.mkdir()
    source.write_bytes(Path(original['trajectory_path']).read_bytes())
    asyncio.run(open_evaluation_ui(source, workspace=tmp_path, port=0, open_browser=False))
    result = sync_dialogue(tmp_path / 'evaluation_ui', original['id'])
    assert result['status'] == 'ready' and result['revision'] == original['revision']
    assert result['trace_count'] == 1


@pytest.mark.parametrize('previous_good_file', [False, True])
def test_transient_rebuild_failure_retries_without_changing_sources(tmp_path, dialogue_agent, monkeypatch, previous_good_file):
    from ojw_evaluation import dialogues
    from ojw_evaluation.state import write_json
    agent, _, _ = dialogue_agent
    for query in ['one', 'two']:
        asyncio.run(agent.invoke({'query': query, 'conversation_id': 'dialogue-a'}))
    original = wait_metadata(metadata_path(tmp_path), turns=2, status='ready')
    root = tmp_path / 'retry/evaluation_ui'
    context = dialogues.register_dialogue(root, 'dialogue-a', launch_dir=tmp_path)
    receipt = root / 'invocations' / ('a'*32 + '.json')
    record = {'id': 'a'*32, 'session_id': 'dialogue-a', 'source_paths': [original['sources'][0]['path']]}
    write_json(receipt, record)
    first = dialogues.sync_dialogue(root, context['id']) if previous_good_file else {'revision': 0}
    record['source_paths'].append(original['sources'][1]['path']); write_json(receipt, record)
    real = dialogues.sequential_merger()
    calls = []
    def flaky(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise OSError('temporary publication failure')
        return real(*args, **kwargs)
    monkeypatch.setattr(dialogues, 'sequential_merger', lambda: flaky)
    failed = dialogues.sync_dialogue(root, context['id'])
    assert failed['status'] == 'incomplete' and failed['revision'] == first['revision']
    # Background retries are bounded, but reopening explicitly retries immediately.
    assert dialogues.sync_dialogue(root, context['id'])['status'] == 'incomplete'
    assert len(calls) == 1
    recovered = dialogues.sync_dialogue(root, context['id'], force=True)
    assert recovered['status'] == 'ready' and recovered['trace_count'] == 2
    assert len(calls) == 2


def test_malformed_dialogue_cannot_block_other_dialogues(tmp_path, dialogue_agent):
    from ojw_evaluation.dialogues import register_dialogue, sync_dialogues, read_dialogue
    from ojw_evaluation.state import write_json
    agent, _, _ = dialogue_agent
    asyncio.run(agent.invoke({'query': 'one', 'conversation_id': 'dialogue-a'}))
    original = wait_metadata(metadata_path(tmp_path), turns=1, status='ready')
    root = tmp_path / 'isolation/evaluation_ui'
    healthy = register_dialogue(root, 'healthy', launch_dir=tmp_path)
    broken = register_dialogue(root, 'broken', launch_dir=tmp_path)
    for key, sid, paths in [('a', 'healthy', [original['sources'][0]['path']]), ('b', 'broken', [None])]:
        write_json(root / 'invocations' / (key*32+'.json'),
                   {'id': key*32, 'session_id': sid, 'source_paths': paths})
    sync_dialogues(root)
    assert read_dialogue(root, healthy['id'])['status'] == 'ready'
    failure = read_dialogue(root, broken['id'])
    assert failure['status'] == 'incomplete' and failure['error']


def test_concurrent_first_merger_load_is_safe(runtime_bootstrap):
    import subprocess
    import sys
    script = '''
from concurrent.futures import ThreadPoolExecutor
from importlib.machinery import SourceFileLoader
import threading
from ojw_evaluation.merger import sequential_merger
entered = threading.Event(); release = threading.Event()
original = SourceFileLoader.exec_module

def gated(self, module):
    if module.__name__.startswith('_dialogue_observer_') and module.__name__.count('.') == 0:
        entered.set()
        assert release.wait(10)
    return original(self, module)

SourceFileLoader.exec_module = gated
with ThreadPoolExecutor(max_workers=2) as pool:
    first = pool.submit(sequential_merger)
    assert entered.wait(10)
    second = pool.submit(sequential_merger)
    threading.Timer(.2, release.set).start()
    assert first.result(15) is second.result(15)
'''
    result = subprocess.run([sys.executable, '-I', '-c', runtime_bootstrap + script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
