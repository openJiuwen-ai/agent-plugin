"""Read terminal outcomes beside a known trajectory without taking ownership."""
import math
from pathlib import Path
import re

from .state import read_json


def read_terminal_run(directory, source):
    """Return a validated record, or ignore incomplete/unrelated artifacts."""
    directory, source = Path(directory), Path(source).resolve()
    try:
        if directory.is_symlink() or directory.parent.is_symlink():
            return None
        directory = directory.resolve()
        if directory.parent != source.parent / 'evaluation':
            return None
        record = directory / 'evaluation.json'
        if record.is_symlink():
            return None
        job = read_json(record)
        if not isinstance(job, dict) or job.get('origin') != 'terminal':
            return None
        if not re.fullmatch(r'[0-9a-f]{32}', job.get('id', '')):
            return None
        if job.get('status') not in {'running', 'completed', 'partial', 'failed', 'interrupted', 'cancelled'}:
            return None
        if not isinstance(job.get('input'), dict) or not isinstance(job.get('parameters'), dict):
            return None
        if Path(job['input']['source_path']).resolve() != source or Path(job['output_dir']).resolve() != directory:
            return None
        if not isinstance(job.get('created_at'), (int, float)) or not math.isfinite(job['created_at']):
            return None
        snapshot = directory / 'trajectory.json'
        if snapshot.is_symlink():
            return None
        if job.get('annotations_file'):
            artifact = Path(job['annotations_file']).resolve()
            if not artifact.is_relative_to(directory) or not artifact.is_file():
                return None
            job['annotations_file'] = str(artifact)
            job['annotations'] = read_json(artifact)
        job['output_dir'] = str(directory)
        return job
    except (OSError, ValueError, TypeError, KeyError):
        return None


def terminal_runs(source):
    source = Path(source).resolve()
    parent = source.parent / 'evaluation'
    if parent.is_symlink():
        return
    for directory in parent.glob('*_terminal*'):
        job = read_terminal_run(directory, source)
        if job is not None:
            yield job
