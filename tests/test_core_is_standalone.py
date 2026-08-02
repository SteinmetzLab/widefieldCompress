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
