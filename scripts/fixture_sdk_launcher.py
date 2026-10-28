#!/usr/bin/env python3
"""Fixed SDK executable entry; loading the backend SDK itself has no side effects."""
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.runtime.engine_sdk.launcher import main

if __name__ == "__main__":
    raise SystemExit(main())
