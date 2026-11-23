"""Expose public package names directly from the plugin during editable installs."""

from importlib.abc import MetaPathFinder
from importlib.util import spec_from_file_location
from pathlib import Path
import sys


class PackageAliases(MetaPathFinder):
    def __init__(self, paths):
        self.paths = paths

    def find_spec(self, fullname, path=None, target=None):
        directory = self.paths.get(fullname)
        if directory is None:
            return None
        return spec_from_file_location(
            fullname, Path(directory) / "__init__.py",
            submodule_search_locations=[directory],
        )


def install(paths):
    sys.meta_path.insert(0, PackageAliases(paths))
