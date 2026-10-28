"""Run or recover a guarded single-node platform/Provider update."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from _bootstrap import add_backend_to_path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("apply", "recover", "status"))
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--target-generation", choices=("A", "B"))
    parser.add_argument("--target-provider")
    parser.add_argument("--crash-after", help="fixture-only forced process exit for recovery tests")
    args = parser.parse_args(argv)
    config = json.loads(args.config.read_text(encoding="utf-8"))
    if args.crash_after and (config.get("scope") != "fixture" or config.get("allow_fixture") is not True):
        parser.error("forced crash is permitted only in fixture scope")
    if args.command == "apply" and (not args.target_generation or not args.target_provider):
        parser.error("apply requires both target generation and Provider digest")
    url_env = config.get("database_url_env", "DATABASE_URL")
    if not os.environ.get(url_env, "").strip():
        parser.error(f"{url_env} is required in the process environment")
    os.environ["DATABASE_URL"] = os.environ[url_env]
    add_backend_to_path()
    from app.runtime.platform_update import PlatformUpdate, UpdateRefused

    try:
        update = PlatformUpdate(config)
        if args.command == "status":
            result = {"runtime": update.actual(), "checkpoint": update.checkpoint.read()}
        elif args.command == "recover":
            result = update.recover(crash_after=args.crash_after)
        else:
            result = update.start((args.target_generation, args.target_provider),
                                  crash_after=args.crash_after)
        print(json.dumps({"ok": True, "result": result}, sort_keys=True))
        return 0
    except (UpdateRefused, ValueError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
