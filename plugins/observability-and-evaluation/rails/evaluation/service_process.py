"""Entry point that also works from a copied plugin without installing it."""
import importlib.util
from pathlib import Path
import sys

if not __package__:
    directory = Path(__file__).resolve().parent
    __package__ = '_ojw_evaluation_service'
    spec = importlib.util.spec_from_file_location(__package__, directory / '__init__.py',
                                                  submodule_search_locations=[str(directory)])
    package = importlib.util.module_from_spec(spec)
    sys.modules[__package__] = package
    spec.loader.exec_module(package)

from .server import main

if __name__ == '__main__':
    main()
