"""Exercise distribution builds without relying on the framework checkout."""

from email.parser import BytesParser
import json
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig
import tarfile
from zipfile import ZipFile

import pytest


ROOT = Path(__file__).resolve().parents[3]
NAME = "observability-and-evaluation"


def run(*args, cwd):
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    return result


@pytest.fixture
def checkout(tmp_path):
    packaging = ROOT / "packaging" / NAME
    repo = tmp_path / "checkout"
    repo.mkdir()
    if not packaging.is_dir():
        return repo
    for relative in (Path("packaging") / NAME, Path("plugins") / NAME, Path("scripts")):
        shutil.copytree(ROOT / relative, repo / relative,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "build", "dist"))
    return repo


def build(project, output, kind):
    pytest.importorskip("hatchling")
    output.mkdir(parents=True, exist_ok=True)
    run(sys.executable, "-I", "-c",
        f"from hatchling.build import build_{kind}; import sys; print(build_{kind}(sys.argv[1]))",
        str(output), cwd=project)
    suffix = "*.tar.gz" if kind == "sdist" else "*.whl"
    return next(output.glob(suffix))


def wheel_files(wheel):
    with ZipFile(wheel) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def test_wheel_and_rebuilt_sdist_have_identical_runtime(checkout, tmp_path):
    project = checkout / "packaging" / NAME
    assert (project / "pyproject.toml").is_file(), "Standalone packaging is missing"
    plugin = checkout / "plugins" / NAME
    sentinel = "synthetic-private-runtime-sentinel"
    for relative in (
        "rails/evaluation/.env", "rails/evaluation/access.json",
        "rails/evaluation/assets/access.json", "rails/evaluation/assets/launch.json",
        "rails/evaluation/private.key", "rails/evaluation/__pycache__/cache.pyc",
        "rails/evaluation/outputs/private.py", "rails/outputs/trace.json",
        "rails/evaluation/ojw_evaluation_outputs/annotations.json",
    ):
        path = plugin / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(sentinel)
    (project / "private.json").write_text(sentinel)
    direct = wheel_files(build(project, tmp_path / "wheel", "wheel"))
    sdist = build(project, tmp_path / "sdist", "sdist")
    with tarfile.open(sdist) as archive:
        contents = {m.name: archive.extractfile(m).read() for m in archive.getmembers() if m.isfile()}
        assert all(sentinel.encode() not in data for data in contents.values())
        archive.extractall(tmp_path / "unpacked", filter="data")
    # The original checkout is unavailable when the archived sources are rebuilt.
    checkout.rename(tmp_path / "hidden-checkout")
    extracted = next((tmp_path / "unpacked").iterdir())
    rebuilt = wheel_files(build(extracted, tmp_path / "rebuilt", "wheel"))
    runtime = lambda files: {k: v for k, v in files.items() if k.startswith(("ojw_observability/", "ojw_evaluation/"))}
    assert runtime(direct) == runtime(rebuilt)
    for name in (
        "ojw_observability/rail.py", "ojw_observability/decorator.py",
        "ojw_evaluation/cli.py", "ojw_evaluation/server.py",
        "ojw_evaluation/assets/evaluation_v4.js",
        "ojw_evaluation/assets/html-demo_v4.html",
        "ojw_evaluation/assets/taxonomies/ours.txt",
        "ojw_evaluation/assets/taxonomies/mast.txt",
    ):
        assert direct[name]
    assert all(sentinel.encode() not in data for data in direct.values())
    assert not any("ojw_agentic_applications" in name for name in direct)
    metadata = BytesParser().parsebytes(next(v for k, v in direct.items() if k.endswith("/METADATA")))
    assert metadata["Version"] == json.loads((ROOT / "plugins" / NAME / "manifest.json").read_text())["version"]
    deps = metadata.get_all("Requires-Dist")
    assert "openjiuwen==0.1.19" in deps
    from packaging.requirements import Requirement
    core_dependencies = [Requirement(dep).name for dep in deps
                         if Requirement(dep).marker is None or Requirement(dep).marker.evaluate({"extra": ""})]
    assert not {"pandas", "pypdf", "pyarrow", "python-dotenv"}.intersection(core_dependencies)
    assert not {"arxiv", "autogen-core", "chromadb", "pyarrow"}.intersection(core_dependencies)
    entries = next(v.decode() for k, v in direct.items() if k.endswith("/entry_points.txt"))
    assert "ojw_evaluation = ojw_evaluation.cli:main" in entries
    assert "ojw-evaluation-ui = ojw_evaluation.server:main" in entries
    assert "ojw-run" not in entries


def test_plugin_wrapper_checks_and_builds_from_another_directory(checkout, tmp_path):
    script = checkout / "packaging" / NAME / "build_plugin.py"
    assert script.is_file(), "Plugin build wrapper is missing"
    checked = run(sys.executable, str(script), "--check", cwd=tmp_path)
    assert "PARTIAL" in checked.stdout  # --check explicitly runs L0/L1 only.
    assert not (checkout / "dist").exists()
    run(sys.executable, str(script), "--zip", cwd=tmp_path)
    archive = next((checkout / "dist").glob("*.zip"))
    with ZipFile(archive) as zipped:
        assert f"{NAME}/rails/observability_rail.py" in zipped.namelist()
    manifest = checkout / "plugins" / NAME / "manifest.json"
    data = json.loads(manifest.read_text())
    data["rails"][0]["file"] = "./rails/missing.py"
    manifest.write_text(json.dumps(data))
    rejected = subprocess.run([sys.executable, str(script), "--check"], cwd=tmp_path,
                              capture_output=True, text=True, timeout=90)
    assert rejected.returncode != 0


def test_sdist_build_rejects_linked_runtime_files(checkout, tmp_path):
    assert (checkout / "packaging" / NAME / "pyproject.toml").is_file(), "Standalone packaging is missing"
    pytest.importorskip("hatchling")
    secret = tmp_path / "external.py"
    secret.write_text("private = True\n")
    (checkout / "plugins" / NAME / "rails/evaluation/linked.py").symlink_to(secret)
    result = subprocess.run([sys.executable, "-I", "-c",
                             "from hatchling.build import build_sdist; build_sdist('dist')"],
                            cwd=checkout / "packaging" / NAME,
                            capture_output=True, text=True, timeout=90)
    assert result.returncode != 0
    assert "Symlink" in result.stderr


@pytest.mark.parametrize("kind", ["wheel", "editable", "sdist"])
def test_installed_public_imports_and_commands_outside_checkout(checkout, tmp_path, kind):
    project = checkout / "packaging" / NAME
    assert (project / "pyproject.toml").is_file(), "Standalone packaging is missing"
    pytest.importorskip("openjiuwen")
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is needed to install the built wheel without network access")
    artifact = None if kind == "editable" else build(project, tmp_path / "dist", kind)
    environment = tmp_path / "environment"
    run(sys.executable, "-m", "venv", "--without-pip", str(environment), cwd=tmp_path)
    python = environment / "bin/python"
    # Reuse installed third-party dependencies without processing the parent
    # environment's .pth files (which might expose a framework checkout).
    site = Path(run(str(python), "-I", "-c",
                    "import sysconfig; print(sysconfig.get_paths()['purelib'])",
                    cwd=tmp_path).stdout.strip())
    (site / "test_dependencies.pth").write_text(sysconfig.get_paths()["purelib"] + "\n")
    install_args = ["-e", str(project)] if kind == "editable" else [str(artifact)]
    if kind == "sdist":
        checkout.rename(tmp_path / "hidden-checkout")
    run(uv, "pip", "install", "--offline", "--no-deps", "--no-build-isolation",
        "--python", str(python), *install_args, cwd=tmp_path)
    expected = checkout / "plugins" / NAME / "rails" if kind == "editable" else site
    run(str(python), "-I", "-c", '''
from importlib.util import find_spec
from importlib.metadata import distribution
from pathlib import Path
import sys
import ojw_observability, ojw_evaluation
from ojw_observability import observe, ObservabilityRail
from ojw_observability.rail import observe as rail_observe
from ojw_evaluation import EvaluationRail, open_evaluation_ui
from ojw_evaluation.core.config import TAXONOMIES
from ojw_evaluation.merger import sequential_merger
assert observe is rail_observe
assert callable(sequential_merger())
assert all(path.is_file() for path in TAXONOMIES.values())
for module in (ojw_observability, ojw_evaluation):
    assert Path(module.__file__).is_relative_to(Path(sys.argv[1])), module.__file__
assert find_spec('ojw_agentic_applications') is None
assert {e.name for e in distribution('openjiuwen-observability-and-evaluation').entry_points} == {'ojw_evaluation', 'ojw-evaluation-ui'}
''', str(expected), cwd=tmp_path)
    for command in ("ojw_evaluation", "ojw-evaluation-ui"):
        result = run(str(environment / "bin" / command), "--help", cwd=tmp_path)
        assert "usage:" in result.stdout
    if kind == "editable":
        (expected / "evaluation/editable_probe.py").write_text("VALUE = 42\n")
        run(str(python), "-I", "-c",
            "from ojw_evaluation.editable_probe import VALUE; assert VALUE == 42", cwd=tmp_path)
