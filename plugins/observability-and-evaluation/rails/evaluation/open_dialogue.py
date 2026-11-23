"""Reopen a saved dialogue from an installed module or a copied plugin."""
import argparse
import importlib.util
from pathlib import Path
import sys

if not __package__:
    directory = Path(__file__).resolve().parent
    __package__ = '_ojw_evaluation_open'
    spec = importlib.util.spec_from_file_location(__package__, directory / '__init__.py',
                                                submodule_search_locations=[str(directory)])
    package = importlib.util.module_from_spec(spec)
    sys.modules[__package__] = package
    spec.loader.exec_module(package)

from .opening import open_saved_dialogue


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('dialogue', help='Saved dialogue folder or metadata.json')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    try:
        result = open_saved_dialogue(args.dialogue, port=args.port)
    except (ValueError, OSError, KeyError, RuntimeError) as exc:
        parser.exit(1, f'Cannot open dialogue: {exc}\n')
    print(result['url'])
    print(f"Revision {result['revision']} · {result['turn_count']} turns · {result['status']}")


if __name__ == '__main__':
    main()
