"""One independently owned UI process per workspace (Linux/macOS file locks)."""
import fcntl
import json
import logging
from pathlib import Path
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

from .state import read_json
from .dependencies import require_evaluation_dependencies

logger = logging.getLogger(__name__)


class ServiceHandle:
    def __init__(self, root, launch):
        self.output_dir = Path(root)
        self.url = launch['url']
        self.port = launch['port']
        self.token = launch['token']

    def api(self, path, data=None):
        request = Request(f'http://127.0.0.1:{self.port}' + path,
                          data=json.dumps(data).encode() if data is not None else None,
                          headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'})
        with urlopen(request, timeout=3) as response:
            return json.load(response)


def announce_service(handle):
    """Expose the listener to terminal-based port discovery without its token."""
    # Announce from the host: the independent service intentionally has no
    # terminal attached. This reports remote readiness, not SSH tunnel readiness.
    logger.warning(
        "Evaluation UI listening at http://127.0.0.1:%d/ "
        "(remote service ready; use /evaluate for the full link)", handle.port)


def running_service(root):
    launch = read_json(Path(root) / 'launch.json')
    if not isinstance(launch, dict):
        return None
    try:
        handle = ServiceHandle(root, launch)
        health = handle.api('/api/health')
        if health.get('output_dir') == str(Path(root).resolve()):
            return handle
    except (OSError, URLError, ValueError, KeyError):
        pass
    return None


def ensure_service(root, *, workspace=None, port=8765):
    require_evaluation_dependencies()
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / 'launch.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        existing = running_service(root)
        if existing:
            return existing
        command = [sys.executable, '-I', str(Path(__file__).with_name('service_process.py')),
                   '--output-dir', str(root), '--port', str(port)]
        if workspace is not None:
            command += ['--workspace', str(workspace)]
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            handle = running_service(root)
            if handle:
                return handle
            if process.poll() is not None:
                raise RuntimeError('Evaluation UI process could not start; run ojw-evaluation-ui in the foreground for details')
            time.sleep(.1)
        process.terminate()
        process.wait(timeout=5)
        raise RuntimeError('Evaluation UI startup timed out')
