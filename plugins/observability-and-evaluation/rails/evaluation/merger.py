"""Load the bundled observer's exporter in both plugin and mapped wheel layouts."""
from functools import lru_cache
from hashlib import sha256
import importlib.util
import importlib
from pathlib import Path
import sys
import threading

_load_lock = threading.Lock()


def sequential_merger():
    # lru_cache alone allows concurrent initial calls to execute its body.
    with _load_lock:
        return _load_observer().merge_workflow


def artifact_helpers():
    with _load_lock:
        package = _load_observer()
        return importlib.import_module(package.__name__ + '.artifacts')


@lru_cache(maxsize=1)
def _load_observer():
    parent = Path(__file__).resolve().parent.parent
    directory = parent / 'observability'
    if not directory.is_dir():
        directory = parent / 'ojw_observability'
    if not (directory / 'workflow.py').is_file():
        raise RuntimeError('The observation exporter is missing from this installation')
    name = '_dialogue_observer_' + sha256(str(directory).encode()).hexdigest()[:16]
    # Plugin loader aliases disappear after loading. This private namespace
    # belongs to this exact package location and stays available to its exporter.
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, directory / '__init__.py',
                                                    submodule_search_locations=[str(directory)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(name, None)
            raise
    return sys.modules[name]
