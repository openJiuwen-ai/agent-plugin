"""Validate or ZIP the plugin with agent-plugin's shared repository tooling."""

import argparse
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Run L0/L1 validation without building a ZIP")
    mode.add_argument("--zip", action="store_true", help="Validate and build a ZIP (the default)")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    scripts = root / "scripts"
    if not (scripts / "validate.py").is_file() or not (scripts / "build.py").is_file():
        parser.error("Run this repository helper from an agent-plugin checkout")
    checked = subprocess.run([
        sys.executable, str(scripts / "validate.py"),
        "--only", "observability-and-evaluation", "--no-hot-load",
    ], cwd=root)
    if checked.returncode or args.check:
        return checked.returncode
    return subprocess.run([
        sys.executable, str(scripts / "build.py"), "observability-and-evaluation",
    ], cwd=root).returncode


if __name__ == "__main__":
    raise SystemExit(main())
