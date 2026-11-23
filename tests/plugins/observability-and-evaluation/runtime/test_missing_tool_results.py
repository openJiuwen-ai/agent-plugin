"""Interrupted tool requests remain valid evidence without invented results."""
import json
from pathlib import Path

import pytest

from ojw_observability.autogen_shim import (
    TaskResult, ToolCallExecutionEvent, ToolCallPayload, ToolCallRequestEvent,
)
from ojw_observability.emit.extract import extract_tool_activities_from_result_messages
from ojw_observability.emit.tools import make_tool_result
from ojw_observability.model.common import ToolCallResult
from ojw_observability.model.sequential import SequentialWorkflowTraceV2
from ojw_observability.workflow import merge_workflow


def test_request_without_execution_has_unknown_result():
    request = ToolCallRequestEvent('agent', [ToolCallPayload(id='call-1', name='ask_user')])
    activity, = extract_tool_activities_from_result_messages(TaskResult([request]))
    result = activity['tool_call_result']
    ToolCallResult.model_validate(dict(result, step_id='2.2', timestamp_start='',
                                       timestamp_end='', duration_seconds=0))
    assert result['call_id'] == 'call-1'
    assert result['tool_name'] == 'ask_user'
    assert result['tool_call_result_count'] == 0
    assert result['raw_output'] == result['result_preview'] == ''
    assert result['parsed_output']['status'] == 'unknown'
    assert result['parsed_output']['data'] is None
    assert result['parsed_output']['metadata'] == {'result_captured': False}
    assert activity['tool_calling']['tool_success'] is False


def test_captured_execution_retains_its_output():
    request = ToolCallRequestEvent('agent', [ToolCallPayload(id='call-1', name='ask_user')])
    output = make_tool_result(tool_called=True, tool_success=True, status='success',
                              data={'answer': 'yes'}, error=None, metadata={})
    execution = ToolCallExecutionEvent('agent', [ToolCallPayload(
        id='call-1', name='ask_user', content=output,
    )])
    activity, = extract_tool_activities_from_result_messages(TaskResult([request, execution]))
    result = activity['tool_call_result']
    assert result['tool_call_result_count'] == 1
    assert result['parsed_output']['status'] == 'success'
    assert result['parsed_output']['data'] == {'answer': 'yes'}
    assert 'result_captured' not in result['parsed_output']['metadata']


@pytest.fixture
def legacy_agent():
    path = Path(__file__).parents[1] / 'fixtures/weather_agent.json'
    agent = json.loads(path.read_text())
    agent['schema_version'] = '2.0.0'
    agent['agent_metadata']['status'] = 'partial'
    activity = agent['tool_execution_activity_1']
    activity['tool_calling'].update(status='unknown', tool_success=False)
    activity['tool_call_result'] = {
        'step_id': activity['tool_call_result']['step_id'],
        'timestamp_start': '', 'timestamp_end': '', 'duration_seconds': 0.0,
        'http_attempts': [],
    }
    return agent


def test_legacy_placeholder_merges_without_changing_source(tmp_path, legacy_agent):
    source = tmp_path / 'agent.json'
    source.write_text(json.dumps(legacy_agent))
    original = source.read_bytes()
    output = merge_workflow([source], tmp_path / 'merged.json')
    merged = json.loads(output.read_text())
    SequentialWorkflowTraceV2.model_validate(merged)
    agent, = merged['agents'].values()
    activity = agent['tool_execution_activity_1']
    result = activity['tool_call_result']
    assert result['call_id'] == activity['tool_calling']['call_id']
    assert result['tool_name'] == activity['tool_calling']['tool_name']
    assert result['tool_call_result_count'] == 0
    assert result['parsed_output']['status'] == 'unknown'
    assert result['raw_output'] == ''
    assert result['timestamp_start'] == result['timestamp_end'] == ''
    assert merged['trace_metadata']['status'] == 'partial'
    assert source.read_bytes() == original


@pytest.mark.parametrize('field,value', [
    ('raw_output', 'actual output'), ('parsed_output', {'status': 'success'}),
    ('tool_call_result_count', 1),
])
def test_partial_payload_is_still_rejected(tmp_path, legacy_agent, field, value):
    legacy_agent['tool_execution_activity_1']['tool_call_result'][field] = value
    source = tmp_path / 'partial.json'
    source.write_text(json.dumps(legacy_agent))
    destination = tmp_path / 'merged.json'
    with pytest.raises(ValueError, match='Invalid agent artifact'):
        merge_workflow([source], destination)
    assert not destination.exists()
