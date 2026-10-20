# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""hot_load_worker.py 单测：包内相对导入与同目录绝对导入（对齐运行时加载链）。

直接测 _load_module（不触发 openjiuwen 探测），因此无需 openjiuwen 环境。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

import pytest

import hot_load_worker


@pytest.fixture(autouse=True)
def namespace_without_optional_sdk(monkeypatch):
    """These loader unit tests do not require the optional openjiuwen runtime.

    Relative imports need the ancestors of the synthetic extension namespace.
    Production run() imports the real SDK first; provide only missing parents
    here and restore them after each test rather than masking a real SDK.
    """
    for name in ("openjiuwen", "openjiuwen.extensions"):
        if name not in sys.modules:
            module = ModuleType(name)
            module.__path__ = []
            monkeypatch.setitem(sys.modules, name, module)


def _make_pkg(tmp_path: Path, name: str) -> Path:
    # 包级命名空间是进程级缓存（与一次性 worker 进程一致），
    # 测试之间须用不同包名避免复用旧 __path__
    pkg = tmp_path / name
    (pkg / "tools").mkdir(parents=True)
    (pkg / "rails").mkdir()
    return pkg


def test_relative_import_within_package(tmp_path: Path):
    """from .helper import VALUE 必须可加载（与运行时行为一致）。"""
    pkg = _make_pkg(tmp_path, "demo-plugin-rel")
    (pkg / "tools" / "helper.py").write_text("VALUE = 42\n", encoding="utf-8")
    (pkg / "tools" / "greeter_tool.py").write_text(
        "from .helper import VALUE\n\nANSWER = VALUE + 1\n", encoding="utf-8"
    )

    package_name = hot_load_worker._ensure_package_namespace(pkg)
    module = hot_load_worker._load_module(pkg, "tools/greeter_tool.py", package_name)
    assert module.ANSWER == 43


def test_absolute_sibling_import_within_package(tmp_path: Path):
    """import helper（同目录绝对导入）在加载窗口内可用。"""
    pkg = _make_pkg(tmp_path, "demo-plugin-abs")
    (pkg / "tools" / "helper.py").write_text("VALUE = 7\n", encoding="utf-8")
    (pkg / "tools" / "greeter_tool.py").write_text(
        "import helper\n\nANSWER = helper.VALUE\n", encoding="utf-8"
    )

    package_name = hot_load_worker._ensure_package_namespace(pkg)
    module = hot_load_worker._load_module(pkg, "tools/greeter_tool.py", package_name)
    assert module.ANSWER == 7


def test_same_name_siblings_do_not_cross_contaminate(tmp_path: Path):
    """tools/helper.py 与 rails/helper.py 同名模块经绝对导入不串扰。"""
    pkg = _make_pkg(tmp_path, "demo-plugin-x")
    (pkg / "tools" / "helper.py").write_text("VALUE = 'tools'\n", encoding="utf-8")
    (pkg / "rails" / "helper.py").write_text("VALUE = 'rails'\n", encoding="utf-8")
    (pkg / "tools" / "a_tool.py").write_text(
        "import helper\n\nWHO = helper.VALUE\n", encoding="utf-8"
    )
    (pkg / "rails" / "b_rail.py").write_text(
        "import helper\n\nWHO = helper.VALUE\n", encoding="utf-8"
    )

    package_name = hot_load_worker._ensure_package_namespace(pkg)
    tool_mod = hot_load_worker._load_module(pkg, "tools/a_tool.py", package_name)
    rail_mod = hot_load_worker._load_module(pkg, "rails/b_rail.py", package_name)
    assert tool_mod.WHO == "tools"
    assert rail_mod.WHO == "rails"


def test_kebab_case_package_name_supported(tmp_path: Path):
    """kebab-case 目录名不是合法标识符，但命名空间加载必须容忍。"""
    pkg = _make_pkg(tmp_path, "demo-plugin-kebab")
    assert "-" in pkg.name
    package_name = hot_load_worker._ensure_package_namespace(pkg)
    (pkg / "tools" / "x_tool.py").write_text("OK = True\n", encoding="utf-8")
    module = hot_load_worker._load_module(pkg, "tools/x_tool.py", package_name)
    assert module.OK is True
    assert module.__name__.startswith(f"openjiuwen.extensions.harness.{pkg.name}.")


def test_load_rejects_path_outside_package(tmp_path: Path):
    pkg = _make_pkg(tmp_path, "demo-plugin-esc")
    package_name = hot_load_worker._ensure_package_namespace(pkg)
    try:
        hot_load_worker._load_module(pkg, "../escape.py", package_name)
    except ValueError as exc:
        assert "越出包根目录" in str(exc) or "文件不存在" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError for path escape")


def test_temporary_sys_path_entries_removed(tmp_path: Path):
    """加载后 sys.path 不残留包内路径。"""
    pkg = _make_pkg(tmp_path, "demo-plugin-path")
    (pkg / "tools" / "x_tool.py").write_text("OK = True\n", encoding="utf-8")
    package_name = hot_load_worker._ensure_package_namespace(pkg)
    before = list(sys.path)
    hot_load_worker._load_module(pkg, "tools/x_tool.py", package_name)
    assert sys.path == before
