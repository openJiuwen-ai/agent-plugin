"""Terminal discovery is scoped, live, read-only, and serves saved snapshots."""
import json
from pathlib import Path
from urllib.error import HTTPError

import pytest
from ojw_evaluation.state import write_json
from test_ui import service, request


def terminal_run(source, *, job_id='a'*32):
    directory = source.parent / 'evaluation' / '2026-10-28_12-00-00_terminal'
    directory.mkdir(parents=True, exist_ok=True)
    artifact = directory / 'artifacts' / 'annotations.json'
    write_json(artifact, [{'step_id': '3.1', 'failures': []}])
    write_json(directory / 'trajectory.json', {'steps': [{'step_id': '3.1'}]})
    job = {'id': job_id, 'origin': 'terminal', 'status': 'completed', 'created_at': 1,
           'output_dir': str(directory), 'input': {'source_path': str(source), 'name': source.name},
           'parameters': {'model': 'judge', 'strategy': 'zero-shot', 'taxonomy': None},
           'annotations_file': str(artifact), 'annotations': json.loads(artifact.read_text())}
    write_json(directory / 'evaluation.json', job)
    return directory, job


def context(service, source, key):
    write_json(service.output_dir / 'contexts' / (key + '.json'),
               {'id': key, 'kind': 'workflow', 'latest_invocation': key})
    write_json(service.output_dir / 'invocations' / (key + '.json'),
               {'id': key, 'context_id': key, 'source_path': str(source)})


def test_terminal_history_scoping_refresh_and_download(service, tmp_path):
    first = tmp_path / 'first' / 'agentic_trajectory.json'
    other = tmp_path / 'other' / 'agentic_trajectory.json'
    key, other_key = 'b'*32, 'c'*32
    context(service, first, key)
    context(service, other, other_key)
    assert request(service, '/api/runs?context=' + key)['runs'] == []
    folder, job = terminal_run(first)
    terminal_run(other, job_id='d'*32)
    before = (folder / 'evaluation.json').read_bytes()
    runs = request(service, '/api/runs?context=' + key)['runs']
    assert [j['id'] for j in runs] == [job['id']]
    assert request(service, '/api/runs')['runs'] == []
    assert [j['id'] for j in request(service, '/api/runs?context=' + other_key)['runs']] == ['d'*32]
    saved = request(service, '/api/runs/' + job['id'])
    assert saved['trajectory_step_ids'] == ['3.1']
    assert request(service, '/api/runs/' + job['id'] + '/annotations') == job['annotations']
    assert job['id'] not in service.jobs
    assert not (service.output_dir / 'jobs' / (job['id'] + '.json')).exists()
    assert (folder / 'evaluation.json').read_bytes() == before
    job['status'] = 'partial'
    write_json(folder / 'evaluation.json', job)
    assert request(service, '/api/runs/' + job['id'])['status'] == 'partial'
    (folder / 'evaluation.json').unlink()
    assert request(service, '/api/runs?context=' + key)['runs'] == []
    with pytest.raises(HTTPError) as exc:
        request(service, '/api/runs/' + job['id'])
    assert exc.value.code == 404


@pytest.mark.parametrize('damage', ['malformed', 'foreign_source', 'external_artifact', 'symlink'])
def test_invalid_terminal_records_are_ignored(service, tmp_path, damage):
    source = tmp_path / 'input' / 'agentic_trajectory.json'
    key = 'b'*32
    context(service, source, key)
    folder, job = terminal_run(source)
    if damage == 'foreign_source': job['input']['source_path'] = str(tmp_path / 'different.json')
    if damage == 'external_artifact': job['annotations_file'] = str(tmp_path / 'secret.json')
    write_json(folder / 'evaluation.json', job)
    if damage == 'malformed': (folder / 'evaluation.json').write_text('[]')
    if damage == 'symlink':
        target = folder.with_name('outside')
        folder.rename(target)
        folder.symlink_to(target, target_is_directory=True)
    assert request(service, '/api/runs?context=' + key)['runs'] == []
