"""Flat workflow exports keep the shared_context architecture label."""
import json
from pathlib import Path

import pytest

from ojw_observability.model import SequentialWorkflowTraceV2
from ojw_observability.workflow import merge_workflow


@pytest.mark.parametrize('architecture', ['sequential', 'shared_context', 'looping'])
def test_flat_workflow_export_preserves_architecture(architecture, tmp_path):
    agent = json.loads((Path(__file__).parents[1] / 'fixtures/weather_agent.json').read_text(encoding='utf-8'))
    agent['schema_version'] = '2.0.0'
    source = tmp_path / 'agent.json'
    source.write_text(json.dumps(agent), encoding='utf-8')
    original = source.read_bytes()
    output = merge_workflow([source], tmp_path / 'workflow.json', scenario_meta={'system_architecture': architecture})
    trace = json.loads(output.read_text(encoding='utf-8'))
    SequentialWorkflowTraceV2.model_validate(trace)
    assert trace['trace_metadata']['system_architecture'] == architecture
    assert trace['trace_metadata']['schema_version'] == '2.0.0'
    assert source.read_bytes() == original


def test_flat_workflow_export_still_rejects_unknown_architecture(tmp_path):
    with pytest.raises(ValueError, match='scenario metadata'):
        merge_workflow([], tmp_path / 'workflow.json', scenario_meta={'system_architecture': 'typo'})
    assert not (tmp_path / 'workflow.json').exists()
