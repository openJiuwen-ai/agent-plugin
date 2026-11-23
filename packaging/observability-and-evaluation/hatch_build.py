"""Build the public packages from the plugin or a self-contained sdist snapshot."""

import json
from pathlib import Path
import tempfile

from hatchling.builders.hooks.plugin.interface import BuildHookInterface
from hatchling.metadata.plugin.interface import MetadataHookInterface


PACKAGE_NAMES = {"observability": "ojw_observability", "evaluation": "ojw_evaluation"}
EXCLUDED_DIRS = {
    "__pycache__", "tests", "outputs", "observability_traces", "evaluation_ui",
    "generated_agentic_trajectories", "ojw_evaluation_outputs", "build", "dist",
}
EXCLUDED_FILES = {"access.json", "launch.json", "credentials.json", "token.json", "tokens.json"}


def runtime_root(root):
    snapshot = root / "runtime"
    package = snapshot if snapshot.is_dir() else root.parents[1] / "plugins/observability-and-evaluation"
    if package.is_symlink() or (package / "rails").is_symlink():
        raise ValueError("Symlink runtime directories are not allowed")
    if not (package / "manifest.json").is_file():
        raise ValueError("Runtime package is missing; build from the repository or a complete source distribution")
    return package


def runtime_files(package):
    """Select Python modules and bundled UI/taxonomy assets, never runtime state."""
    for module, public_name in PACKAGE_NAMES.items():
        directory = package / "rails" / module
        if directory.is_symlink():
            raise ValueError(f"Symlink runtime directory: {directory}")
        if not (directory / "__init__.py").is_file():
            raise ValueError(f"Missing runtime package: {directory}")
        for path in sorted(directory.rglob("*")):
            relative = path.relative_to(directory)
            if path.is_symlink():
                raise ValueError(f"Symlink runtime file: {relative}")
            if (not path.is_file() or path.name in EXCLUDED_FILES
                    or any(p.startswith(".") or p in EXCLUDED_DIRS for p in relative.parts)):
                continue
            if path.suffix == ".py" or (
                relative.parts[0] == "assets" and path.suffix in {".html", ".js", ".txt", ".json"}
            ):
                yield path, Path(public_name) / relative


class CustomMetadataHook(MetadataHookInterface):
    def update(self, metadata):
        package = runtime_root(Path(self.root))
        metadata["version"] = json.loads((package / "manifest.json").read_text(encoding="utf-8"))["version"]
        # One tested declaration keeps wheel and host requirements consistent.
        metadata["dependencies"] = [
            line.split("#", 1)[0].strip()
            for line in (package / "requirements.txt").read_text(encoding="utf-8").splitlines()
            if line.split("#", 1)[0].strip()
        ]


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        root = Path(self.root)
        package = runtime_root(root)
        files = list(runtime_files(package))
        included = build_data.setdefault("force_include", {})
        if self.target_name == "sdist":
            for path, _ in files:
                included[str(path)] = "runtime/" + path.relative_to(package).as_posix()
            for name in ("manifest.json", "requirements.txt"):
                path = package / name
                if path.is_symlink():
                    raise ValueError(f"Symlink metadata file: {name}")
                included[str(path)] = "runtime/" + name
        elif version == "editable":
            self._temporary = tempfile.TemporaryDirectory(prefix="ojw-editable-")
            aliases = {public: str(package / "rails" / module) for module, public in PACKAGE_NAMES.items()}
            pth = Path(self._temporary.name) / "_ojw_editable_aliases.pth"
            pth.write_text(f"import _ojw_editable; _ojw_editable.install({aliases!r})\n", encoding="utf-8")
            included[str(root / "editable_imports.py")] = "_ojw_editable.py"
            included[str(pth)] = pth.name
        else:
            included.update({str(path): target.as_posix() for path, target in files})

    def finalize(self, version, build_data, artifact_path):
        if hasattr(self, "_temporary"):
            self._temporary.cleanup()
