"""UTC evaluation folder names remain unique even for simultaneous runs."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from ojw_evaluation.core import artifacts


@pytest.fixture
def frozen_time(monkeypatch):
    class Clock:
        @classmethod
        def now(cls, tz=None):
            assert tz is timezone.utc
            return datetime(2026, 10, 28, 14, 32, 5, tzinfo=tz)
    monkeypatch.setattr(artifacts, 'datetime', Clock)


@pytest.mark.parametrize('origin', ['terminal', 'ui'])
def test_directory_uses_utc_start_time(tmp_path, frozen_time, origin):
    directory = artifacts.create_evaluation_directory(tmp_path, origin)
    assert directory.name == f'2026-10-28_14-32-05_{origin}'
    assert directory.is_dir()


@pytest.mark.parametrize('origin', ['terminal', 'ui'])
def test_same_second_keeps_all_evaluations(tmp_path, frozen_time, origin):
    first = artifacts.create_evaluation_directory(tmp_path, origin)
    marker = first / 'annotations.json'
    marker.write_text('original results')
    second = artifacts.create_evaluation_directory(tmp_path, origin)
    third = artifacts.create_evaluation_directory(tmp_path, origin)
    assert second.name == f'2026-10-28_14-32-05_{origin}_2'
    assert third.name == f'2026-10-28_14-32-05_{origin}_3'
    assert marker.read_text() == 'original results'
    assert list(second.iterdir()) == []


@pytest.mark.parametrize('origin', ['terminal', 'ui'])
def test_concurrent_runs_create_separate_directories(tmp_path, frozen_time, origin):
    with ThreadPoolExecutor(max_workers=4) as pool:
        paths = list(pool.map(lambda _: artifacts.create_evaluation_directory(tmp_path, origin), range(4)))
    assert len(set(paths)) == 4
    assert all(path.is_dir() for path in paths)
