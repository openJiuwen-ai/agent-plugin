"""Load only the destination plugin; application packages must never be imported."""
import importlib.abc
from pathlib import Path
import sys

import pytest

pytest.importorskip('openjiuwen')
PACKAGE = Path(__file__).resolve().parents[4] / 'plugins/observability-and-evaluation'

# This bootstrap is also used by child-process tests, which cannot inherit
# pytest's imports and must not fall back to an unrelated editable installation.
BOOTSTRAP = f'''
import importlib.util
from pathlib import Path
import sys
for public, folder in (("ojw_observability", "observability"), ("ojw_evaluation", "evaluation")):
    directory = Path({str(PACKAGE)!r}) / "rails" / folder
    spec = importlib.util.spec_from_file_location(public, directory / "__init__.py",
                                                submodule_search_locations=[str(directory)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[public] = module
    spec.loader.exec_module(module)
'''


class NoApplications(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] == 'ojw_agentic_applications':
            raise AssertionError('Plugin tests must not import the application package')


sys.meta_path.insert(0, NoApplications())
exec(BOOTSTRAP)


@pytest.fixture
def runtime_bootstrap():
    return BOOTSTRAP
