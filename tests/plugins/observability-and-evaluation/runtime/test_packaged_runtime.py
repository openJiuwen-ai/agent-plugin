"""Exercise the actual ZIP with read-only files and no standalone package aliases."""
from hashlib import sha256
from pathlib import Path
import subprocess
import sys
import sysconfig
from zipfile import ZipFile

import build

from ojw_evaluation.runtime import running_service

ROOT = Path(__file__).resolve().parents[4]
PACKAGE = ROOT / 'plugins/observability-and-evaluation'
RUNNER = Path(__file__).resolve().parents[1] / 'packaged_smoke.py'


def test_readonly_zip_runs_complete_dialogue_and_offline_evaluation(tmp_path):
    archive = build.build_zip(PACKAGE, tmp_path / 'dist')
    extracted = tmp_path / 'extracted'
    with ZipFile(archive) as zipped:
        zipped.extractall(extracted)
    package = extracted / PACKAGE.name
    paths = [*package.rglob('*'), package]
    original = {p.relative_to(package): sha256(p.read_bytes()).hexdigest() for p in paths if p.is_file()}
    # Inherit dependencies without executing this interpreter's .pth files.
    # Even a developer's editable install must not satisfy copied-plugin imports.
    environment = tmp_path / 'runtime'
    subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(environment)], check=True)
    python = environment / 'bin/python'
    site = subprocess.check_output([str(python), '-c',
                                    'import sysconfig; print(sysconfig.get_paths()["purelib"])'], text=True).strip()
    (Path(site) / 'test_dependencies.pth').write_text(sysconfig.get_paths()['purelib'] + '\n')
    workspace = tmp_path / 'workspace'
    try:
        for path in paths:
            path.chmod(0o555 if path.is_dir() else 0o444)
        result = subprocess.run([str(python), '-I', '-B', str(RUNNER), str(package), str(workspace),
                                 '--interaction', '--move-workspace'], cwd=tmp_path,
                                capture_output=True, text=True, timeout=180)
        (tmp_path / 'packaged-smoke.log').write_text(result.stdout + result.stderr)
        assert result.returncode == 0, result.stdout + result.stderr
        assert 'unload: PASS' in result.stdout
        final = {p.relative_to(package): sha256(p.read_bytes()).hexdigest()
                 for p in package.rglob('*') if p.is_file()}
        assert final == original, 'The installed plugin was modified at runtime'
    finally:
        for launch in workspace.rglob('launch.json'):
            handle = running_service(launch.parent)
            if handle:
                handle.api('/api/shutdown', {})
        for path in paths:
            path.chmod(0o755 if path.is_dir() else 0o644)
