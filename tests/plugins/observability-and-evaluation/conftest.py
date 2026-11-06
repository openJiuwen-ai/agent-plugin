"""Optional dependency checks for the lightweight gate and required runtime CI."""
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
import shutil

import pytest


def pytest_addoption(parser):
    parser.addoption('--require-ojw-runtime', action='store_true',
                     help='Fail instead of skipping when plugin runtime/build dependencies are missing')


def pytest_configure(config):
    if not config.getoption('--require-ojw-runtime'):
        return
    try:
        for name in ('openjiuwen', 'openai', 'pydantic', 'httpx', 'hatchling',
                     'opentelemetry.instrumentation.openai',
                     'opentelemetry.instrumentation.httpx', 'opentelemetry.instrumentation.requests'):
            import_module(name)
        version('build')
        if not shutil.which('uv'):
            raise ImportError('uv executable is required for installation tests')
    except (ImportError, PackageNotFoundError) as exc:
        raise pytest.UsageError(f'Plugin runtime tests require the core requirements, build, hatchling, and uv: {exc}') from exc
