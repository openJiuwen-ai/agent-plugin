"""The evaluation command bypasses inference and never becomes an evaluated turn."""
import asyncio
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock

import pytest
from openjiuwen.core.foundation.llm import AssistantMessage, ToolCall
from openjiuwen.core.session.agent import create_agent_session
from openjiuwen.core.single_agent.interrupt.state import INTERRUPTION_KEY, ToolInterruptEntry, ToolInterruptionState

from ojw_evaluation.runtime import running_service
from test_dialogues import dialogue_agent, metadata_path, wait_metadata


class CountingModel:
    def __init__(self):
        self.calls = 0

    async def invoke(self, **kwargs):
        self.calls += 1
        return AssistantMessage(content='Ordinary answer', finish_reason='stop')


def invoke(agent, query, sid='dialogue-a'):
    return asyncio.run(agent.invoke({'query': query, 'conversation_id': sid}))


def host_message(content, *, language='en', origin='external_user_authored', kind='user input'):
    prefix = 'You receive a new message:\n' if language == 'en' else '\u4f60\u6536\u5230\u4e00\u6761\u6d88\u606f\uff1a\n'
    return prefix + json.dumps({'source': 'web', 'timezone': 'Asia/Shanghai',
        'timestamp': '2026-10-21 20:33:43', 'preferred_response_language': language,
        'content': content, 'type': kind, 'files_updated_by_user': '{}', 'origin_kind': origin})


@pytest.mark.parametrize('query', [' /evaluate\n', host_message('/evaluate'), host_message('/evaluate', language='zh')],
                         ids=['standalone', 'swarm-en', 'swarm-zh'])
def test_command_returns_full_link_without_model_or_extra_turn(tmp_path, dialogue_agent, query):
    agent, rail, opened = dialogue_agent
    model = CountingModel()
    agent.set_llm(model)
    invoke(agent, 'first task')
    before = wait_metadata(metadata_path(tmp_path), turns=1, status='ready')
    receipts = set((tmp_path / 'evaluation_ui/invocations').glob('*.json'))
    trajectory = Path(before['trajectory_path']).read_bytes()

    result = invoke(agent, query)
    assert model.calls == 1, 'Opening evaluation must not call the model'
    assert result['result_type'] == 'answer'
    url = result['output']
    params = parse_qs(urlsplit(url).fragment)
    assert params['context'] == [before['id']]
    assert params['token'] and url == rail.last_url
    service = running_service(tmp_path / 'evaluation_ui')
    config = service.api('/api/config?context=' + before['id'])
    assert config['dialogue']['turn_count'] == 1
    assert config['dialogue']['revision'] == before['revision']
    assert Path(before['trajectory_path']).read_bytes() == trajectory
    assert set((tmp_path / 'evaluation_ui/invocations').glob('*.json')) == receipts
    assert service.api('/api/runs')['runs'] == []
    assert opened == []

    invoke(agent, 'continue the task')
    after = wait_metadata(metadata_path(tmp_path), turns=2, status='ready')
    assert after['trace_count'] == 2 and model.calls == 2


def test_command_can_open_empty_dialogue_and_does_not_reuse_other_session(tmp_path, dialogue_agent):
    agent, _, opened = dialogue_agent
    model = CountingModel()
    agent.set_llm(model)
    invoke(agent, 'first task')
    first = wait_metadata(metadata_path(tmp_path), turns=1, status='ready')
    result = invoke(agent, '/evaluate', 'dialogue-b')
    assert model.calls == 1
    second = json.loads(metadata_path(tmp_path, 'dialogue-b').read_text())
    assert second['turn_count'] == 0 and second['id'] != first['id']
    assert parse_qs(urlsplit(result['output']).fragment)['context'] == [second['id']]
    assert not Path(second['trajectory_path']).exists()
    assert opened == []


@pytest.mark.parametrize('failure', ['service', 'context'])
def test_command_returns_exact_failure_without_falling_through_to_model(tmp_path, dialogue_agent, monkeypatch, failure):
    import ojw_evaluation.opening as opening
    agent, rail, opened = dialogue_agent
    model = CountingModel()
    agent.set_llm(model)

    def unavailable(*args, **kwargs):
        raise OSError('The evaluation directory is unavailable')

    if failure == 'service':
        monkeypatch.setattr(opening, 'ensure_service', unavailable)
    else:
        monkeypatch.setattr(rail, '_context', unavailable)
    result = invoke(agent, '/evaluate')
    assert model.calls == 0
    assert result['result_type'] == 'error'
    assert result['output'] == 'The evaluation directory is unavailable'
    assert rail.last_error == result['output'] and opened == []
    assert not list((tmp_path / 'evaluation_ui/invocations').glob('*.json'))


def test_command_only_matches_a_standalone_command(dialogue_agent):
    agent, _, _ = dialogue_agent
    model = CountingModel()
    agent.set_llm(model)
    queries = ['Explain /evaluate', '/evaluate another task',
        host_message('Explain /evaluate'), host_message('/evaluate', origin='internal_dispatch'),
        host_message('/evaluate', kind='cross_session_message'), host_message(host_message('/evaluate')),
        'You receive a new message:\n{"content": "/evaluate"',
        host_message('/evaluate') + '\nAdditional instructions']
    for query in queries:
        assert invoke(agent, query)['output'] == 'Ordinary answer'
    assert model.calls == len(queries)


@pytest.mark.parametrize('service_failure', [False, True])
def test_command_preserves_pending_tool_without_resuming_it(dialogue_agent, monkeypatch, service_failure):
    import ojw_evaluation.opening as opening
    agent, _, _ = dialogue_agent
    model = CountingModel()
    agent.set_llm(model)
    # A resumed batch could have side effects; keep that boundary harmless.
    execute = AsyncMock(return_value=[])
    monkeypatch.setattr(agent, '_execute_tool_call', execute)
    call = ToolCall(id='pending', type='function', name='awaiting_approval', arguments='{}')
    pending = ToolInterruptionState(ai_message=AssistantMessage(tool_calls=[call]), iteration=0,
        original_query='Original task', interrupted_tools={'pending': ToolInterruptEntry(tool_call=call)})
    if service_failure:
        def unavailable(*args, **kwargs):
            raise OSError('UI unavailable')
        monkeypatch.setattr(opening, 'ensure_service', unavailable)

    async def run():
        session = create_agent_session(session_id='dialogue-a', card=agent.card)
        await session.pre_run(inputs={})
        session.update_state({INTERRUPTION_KEY: pending})
        result = await agent.invoke({'query': '/evaluate', 'conversation_id': 'dialogue-a'}, session)
        assert result['result_type'] == ('error' if service_failure else 'answer')
        assert execute.await_count == 0, '/evaluate must not resume a pending tool'
        assert model.calls == 0
        assert session.get_state(INTERRUPTION_KEY) == pending
        # The user's subsequent response must still reach the original tool.
        await agent.invoke({'query': 'Approved', 'conversation_id': 'dialogue-a'}, session)
        assert execute.await_count == 1
        assert session.get_state(INTERRUPTION_KEY) is None
    asyncio.run(run())
