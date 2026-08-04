"""The core must not depend on the lab layer, or on any hard-coded server path.

This is the thing that keeps `wfcompress` useful to anyone who has a tar of camera frames. The
dependency runs one way: `wfcompress.lab` -> `wfcompress`, never the reverse.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

CORE = Path(__file__).resolve().parents[1] / "src" / "wfcompress"
CORE_MODULES = sorted(p for p in CORE.glob("*.py"))


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(("." * node.level) + (node.module or ""))
    return names


def test_core_never_imports_lab():
    for module in CORE_MODULES:
        for name in _imports(module):
            assert "lab" not in name.split("."), f"{module.name} imports the lab layer: {name}"


def test_core_has_no_unc_or_drive_paths():
    # A UNC path or a bare drive letter in the core would make it site-specific.
    # The hostname must start with a letter, so escape sequences like \\0\\ in a docstring
    # showing the container's magic bytes do not trip this.
    unc = re.compile(r"\\\\[A-Za-z][A-Za-z0-9._-]{2,}\\")
    drive = re.compile(r"\b[A-Za-z]:[\\/]")
    for module in CORE_MODULES:
        text = module.read_text(encoding="utf-8")
        assert not unc.search(text), f"{module.name} contains a UNC server path"
        assert not drive.search(text), f"{module.name} contains an absolute drive path"


def test_core_has_no_sys_path_manipulation():
    for module in CORE_MODULES:
        text = module.read_text(encoding="utf-8")
        assert "sys.path.insert" not in text, f"{module.name} manipulates sys.path"
        assert "sys.path.append" not in text, f"{module.name} manipulates sys.path"


def test_core_is_importable_without_the_lab_layer():
    import wfcompress

    assert hasattr(wfcompress, "compress")
    assert hasattr(wfcompress, "WfzReader")


def test_every_module_imports():
    """Import each module, including the lab layer.

    The lab layer has no unit tests of its own because it only makes sense against the lab share,
    so nothing exercised it -- and a syntax error in `census.py` shipped and survived a green test
    run. Importing is a low bar, but it is the bar that was missing.
    """
    import importlib
    import pkgutil

    import wfcompress

    failures = []
    for mod in pkgutil.walk_packages(wfcompress.__path__, prefix="wfcompress."):
        try:
            importlib.import_module(mod.name)
        except Exception as e:  # noqa: BLE001 - report all of them, not just the first
            failures.append(f"{mod.name}: {type(e).__name__}: {e}")
    assert not failures, "modules failed to import:\n  " + "\n  ".join(failures)
