"""Fixed Host-owned loader; executable/module names never come from requests."""
from __future__ import annotations
import argparse
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload", required=True)
    parser.add_argument("--launch-fd", type=int, required=True)
    args = parser.parse_args()
    root = Path(args.payload)
    if not root.is_absolute() or root.is_symlink():
        raise SystemExit("entrypoint_invalid")
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(root))
    from sdk_runtime.provider import main as provider_main
    return provider_main(args.launch_fd, root)
