"""Durable terminal outcomes without paid model calls."""
import json
from hashlib import sha256
from pathlib import Path

import pytest
from ojw_evaluation import cli
from ojw_evaluation.core import tracing


@pytest.mark.parametrize('outcome,status,exit_code', [
    ('completed', 'completed', 0), ('partial', 'partial', 1),
    ('failed', 'failed', 1), ('error', 'failed', 1),
    ('interrupt', 'interrupted', 130), ('invalid', 'failed', 2),
])
def test_terminal_records(tmp_path, monkeypatch, outcome, status, exit_code):
    trajectory = tmp_path / 'agentic_trajectory.json'
    original = b'{"steps": [{"step_id": "3.1", "text": "original"}]}'
    trajectory.write_bytes(original)
    monkeypatch.setenv('OPENROUTER_API_KEY', 'never-persist-this-key')
    monkeypatch.setattr(cli, 'build_client', lambda _: (object(), 'resolved-judge'))
    class Observer:
        def __init__(self, **kwargs): pass
        def init(self): pass
        def shutdown(self): pass
    monkeypatch.setattr(tracing, 'Observability', Observer)

    async def evaluate(args, obs, run_dir, **kwargs):
        record = json.loads((Path(args.trajectory).parent / 'evaluation.json').read_text())
        assert record['status'] == 'running'
        assert record['parameters']['model'] == 'resolved-judge'
        assert Path(args.trajectory) != trajectory
        trajectory.write_text('{"steps": []}')
        assert Path(args.trajectory).read_bytes() == original
        if outcome == 'error': raise RuntimeError('never-persist-this-key')
        if outcome == 'interrupt': raise KeyboardInterrupt
        if outcome == 'invalid': raise SystemExit(2)
        if outcome in ('completed', 'partial'):
            run_dir.write_json('annotations.json', [{'step_id': '3.1', 'failures': []}])
        return 0 if outcome == 'completed' else 1
    monkeypatch.setattr(cli, 'run_failure_detector', evaluate)
    argv = ['run', 'failure-detector', '--trajectory', str(trajectory), '--client', 'openrouter']
    if outcome == 'interrupt':
        with pytest.raises(KeyboardInterrupt): cli.main(argv)
    elif outcome == 'invalid':
        with pytest.raises(SystemExit) as exc: cli.main(argv)
        assert exc.value.code == 2
    else:
        assert cli.main(argv) == exit_code
    folder, = (tmp_path / 'evaluation').iterdir()
    metadata = json.loads((folder / 'evaluation.json').read_text())
    result = json.loads((folder / 'failure_detector_result.json').read_text())
    source = json.loads((folder / 'source.json').read_text())
    params = json.loads((folder / 'parameters.json').read_text())
    assert metadata['origin'] == 'terminal'
    assert metadata['status'] == result['status'] == status
    assert metadata['exit_code'] == result['exit_code'] == exit_code
    assert metadata['parameters'] == params
    assert params['model'] == 'resolved-judge'
    assert metadata['input']['source_path'] == source['source_path'] == str(trajectory)
    assert source['snapshot_sha256'] == source['source_sha256'] == sha256(original).hexdigest()
    assert (folder / 'trajectory.json').read_bytes() == original
    assert metadata['finished_at'] >= metadata['created_at']
    assert len(metadata['id']) == 32
    if outcome in ('completed', 'partial'):
        assert Path(result['annotations_file']).is_file()
        assert result['annotations'] == metadata['annotations']
    else:
        assert result['annotations_file'] is None
    for artifact in folder.rglob('*'):
        if artifact.is_file():
            assert b'never-persist-this-key' not in artifact.read_bytes(), artifact


def test_terminal_records_effective_sampling_and_provider_failure(tmp_path, monkeypatch):
    trajectory = tmp_path / 'agentic_trajectory.json'
    trajectory.write_text('{"steps": [{"step_id": "1"}]}')
    def rejected(_):
        raise ValueError('credential-value-that-must-not-be-saved')
    monkeypatch.setattr(cli, 'build_client', rejected)
    code = cli.main(['run', '--trajectory', str(trajectory), '--strategy', 'zero-shot',
                     '--client', 'openrouter', '--model-id', 'openai/gpt-5.5',
                     '--temperature', '0.7', '--top-p', '0.8'])
    assert code == 1
    folder, = (tmp_path / 'evaluation').iterdir()
    metadata = json.loads((folder / 'evaluation.json').read_text())
    assert metadata['status'] == 'failed'
    params = metadata['parameters']
    assert params['model'] == 'openai/gpt-5.5'
    assert params['temperature'] is None and params['top_p'] is None
    assert params['taxonomy'] is None and params['dedup'] == 'none'
    for artifact in folder.rglob('*'):
        if artifact.is_file():
            assert b'credential-value-that-must-not-be-saved' not in artifact.read_bytes(), artifact
