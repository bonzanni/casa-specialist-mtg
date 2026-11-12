"""Where Casa's own source is, for the tests that judge this component with it.

`CASA_ROOT` set (CI's `casa` job): it must be Casa's code root, and Casa's
modules must import there. Anything else FAILS, so a misconfigured job cannot
turn the Casa arms into silent passes.

`CASA_ROOT` unset: a sibling `../ha-casa-app` checkout is used when its
modules import in this interpreter, and the Casa arms skip otherwise. That
keeps a plain local run green while CI holds the strict line.
"""
import importlib
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_SIBLING = ROOT.parent / "ha-casa-app" / "casa" / "rootfs" / "opt" / "casa"
_MARKER = "specialist_component.py"
_MODULES = ("specialist_component", "specialist_install", "persona_pack",
            "prompt_compiler")


def casa_root() -> "Path | None":
    """Casa's code root, or None when there is none to use (see module doc)."""
    env = os.environ.get("CASA_ROOT")
    if env:
        root = Path(env)
        assert (root / _MARKER).is_file(), (
            f"CASA_ROOT={env} is not Casa's code root (no {_MARKER}); point it "
            "at ha-casa-app/casa/rootfs/opt/casa")
        return root
    return _SIBLING if (_SIBLING / _MARKER).is_file() else None


def casa_modules():
    """Import Casa's modules from casa_root(), or skip when none is usable."""
    root = casa_root()
    if root is None:
        pytest.skip("no Casa checkout (set CASA_ROOT)")
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        return {name: importlib.import_module(name) for name in _MODULES}
    except ImportError as exc:
        if os.environ.get("CASA_ROOT"):
            raise AssertionError(
                f"Casa's modules do not import under CASA_ROOT ({exc}); install "
                "Casa's pinned pyyaml, jsonschema, rfc8785, markdown-it-py") from exc
        pytest.skip(f"Casa's modules do not import here ({exc})")
