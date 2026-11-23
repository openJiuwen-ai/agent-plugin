"""The distributed plugin contains its runtime without the scenario project."""

import ast
from pathlib import Path
from zipfile import ZipFile

import build
import validate


PACKAGE = Path(__file__).resolve().parents[3] / "plugins" / "observability-and-evaluation"


def test_runtime_plugin_passes_static_validation():
    layers = validate.validate_static(PACKAGE)
    assert all(not layer.errors for layer in layers), "\n".join(
        layer.render() for layer in layers
    )


def test_plugin_archive_contains_entry_points_and_evaluation_assets(tmp_path):
    assert PACKAGE.is_dir(), "Runtime plugin has not been imported"
    archive_path = build.build_zip(PACKAGE, tmp_path)
    with ZipFile(archive_path) as archive:
        prefix = "observability-and-evaluation/"
        required = {
            "manifest.json", "README.md", "requirements.txt",
            "rails/observability_rail.py", "rails/evaluation_rail.py",
            "rails/observability/rail.py", "rails/evaluation/rail.py",
            "tools/open_evaluation_tool.py",
            "rails/evaluation/service_process.py", "rails/evaluation/worker.py",
            "rails/evaluation/assets/html-demo_v4.html",
            "rails/evaluation/assets/evaluation_v4.js",
            "rails/evaluation/assets/taxonomies/ours.txt",
            "rails/evaluation/assets/taxonomies/mast.txt",
        }
        names = set(archive.namelist())
        assert {prefix + name for name in required} <= names
        for name in required:
            assert archive.read(prefix + name), name
        forbidden = {
            "ojw_agentic_applications", "Dataset", "tests", "__pycache__",
            "generated_agentic_trajectories", "ojw_evaluation_outputs",
            "evaluation_ui", ".venv", ".env", "access.json", "launch.json",
            "hatch_build.py", "pyproject.toml",
        }
        for name in names:
            assert name.startswith(prefix)
            assert not (set(Path(name).parts) & forbidden), name
            assert not name.endswith((".pyc", ".pyo", ".key", ".pem")), name


def test_runtime_imports_do_not_require_source_project_packages():
    assert PACKAGE.is_dir(), "Runtime plugin has not been imported"
    forbidden = {"ojw_agentic_applications", "ojw_observability", "ojw_evaluation"}
    violations = []
    for path in PACKAGE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                roots = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                roots = {node.module.split(".")[0]}
            else:
                continue
            if roots & forbidden:
                violations.append(f"{path.relative_to(PACKAGE)}:{node.lineno}")
    assert not violations, "Source-project imports: " + ", ".join(violations)
