"""Evaluate snapshots through the CLI with an offline judge."""
import pytest


@pytest.mark.parametrize('strategy', ['zero-shot', 'taxonomy-guided'])
@pytest.mark.parametrize('override_output', [False, True])
def test_evaluation_cli_writes_annotations_offline(tmp_path, monkeypatch, strategy, override_output):
    import json
    from pathlib import Path
    from types import SimpleNamespace
    from ojw_evaluation import cli

    trajectory = tmp_path / 'trajectory.json'
    trajectory.write_text(json.dumps({'steps': [{'step_id': '1', 'content': 'offline marker'}]}))

    class Judge:
        async def create(self, **kwargs):
            assert 'offline marker' in kwargs['messages'][-1]['content']
            content = json.dumps({'annotations': [{'step_id': '1', 'failures': []}]})
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

    client = SimpleNamespace(chat=SimpleNamespace(completions=Judge()))
    monkeypatch.setattr(cli, 'build_client', lambda provider: (client, 'offline-judge'))
    monkeypatch.chdir(tmp_path)
    output = tmp_path / ('results' if override_output else 'evaluation')
    arguments = ['run', 'failure-detector', '--strategy', strategy,
                 '--taxonomy', 'ours', '--client', 'openrouter',
                 '--trajectory', trajectory.name]
    if override_output:
        arguments += ['--output-dir', str(output)]
    for _ in range(2):
        assert cli.main(arguments) == 0
    folders = list(output.iterdir())
    assert len(folders) == 2
    import re
    assert all(re.fullmatch(r'\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_terminal(?:_[2-9][0-9]*)?', folder.name) for folder in folders)
    assert all(list(folder.rglob('run.log')) and list(folder.rglob('prompts/*.txt')) for folder in folders)
    assert not (tmp_path / 'ojw_evaluation_outputs').exists()
    for folder in folders:
        metadata = json.loads((folder / 'evaluation.json').read_text())
        result_record = json.loads((folder / 'failure_detector_result.json').read_text())
        assert metadata['origin'] == 'terminal'
        assert metadata['status'] == result_record['status'] == 'completed'
        assert metadata['parameters']['model'] == 'offline-judge'
        assert metadata['parameters']['strategy'] == strategy
        assert metadata['parameters']['taxonomy'] == ('ours' if strategy == 'taxonomy-guided' else None)
        assert metadata['input']['source_path'] == str(trajectory)
        assert (folder / 'trajectory.json').read_bytes() == trajectory.read_bytes()
        assert json.loads(Path(result_record['annotations_file']).read_text()) == result_record['annotations']
    artifacts = list(output.rglob('annotations.json'))
    assert len(artifacts) == 2
    result = json.loads(artifacts[0].read_text())
    steps = result['annotations'] if strategy == 'zero-shot' else result
    assert len(steps) == 1
    assert steps[0]['step_id'] == '1'
    assert steps[0]['failures'] == []


@pytest.mark.parametrize('strategy', ['zero-shot', 'taxonomy-guided'])
def test_cli_provider_error_does_not_persist_credentials(tmp_path, monkeypatch, capfd, strategy):
    import json
    from types import SimpleNamespace
    import httpx
    from openai import AuthenticationError
    from ojw_evaluation import cli

    marker = 'provider/key/must-not-be-saved'
    trajectory = tmp_path / 'trajectory.json'
    trajectory.write_text('{"steps": [{"step_id": "1", "content": "offline"}]}')

    class Judge:
        async def create(self, **kwargs):
            request = httpx.Request('POST', 'https://example.invalid/v1/chat/completions')
            body = {'error': {'message': 'Rejected credential: ' + marker}}
            response = httpx.Response(401, request=request, json=body)
            raise AuthenticationError(body['error']['message'], response=response, body=body)

    client = SimpleNamespace(chat=SimpleNamespace(completions=Judge()))
    monkeypatch.setattr(cli, 'build_client', lambda provider: (client, 'offline-judge'))
    code = cli.main(['run', '--strategy', strategy, '--trajectory', str(trajectory)])
    assert code == 1
    folder, = (tmp_path / 'evaluation').iterdir()
    assert json.loads((folder / 'evaluation.json').read_text())['status'] == 'failed'
    logs = list(folder.rglob('run.log'))
    assert logs and 'AuthenticationError' in logs[0].read_text()
    for artifact in folder.rglob('*'):
        if artifact.is_file():
            assert marker.encode() not in artifact.read_bytes(), artifact
    captured = capfd.readouterr()
    assert marker not in captured.out + captured.err
