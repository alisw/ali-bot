#!/usr/bin/env python3
"""Run every test module here. No pytest, no dependencies beyond the proxy's own venv:

    ./venv/bin/python tests/run_all.py

Each module is also runnable on its own and exits non-zero on failure.
"""
import importlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

MODULES = sorted(p.stem for p in HERE.glob("test_*.py"))


def main() -> int:
    failures = []
    for name in MODULES:
        module = importlib.import_module(name)
        print(f"\n== {name}: {module.__doc__.splitlines()[0]}")
        failures += [f"{name}: {f}" for f in module.run()]

    print(f"\n{'=' * 60}")
    if failures:
        print(f"{len(failures)} failure(s) across {len(MODULES)} module(s):")
        for f in failures:
            print(f"  {f}")
        return 1
    print(f"all {len(MODULES)} module(s) passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
