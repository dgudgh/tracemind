"""Run the stable local router or request a guarded A/B route transition."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from _bootstrap import add_backend_to_path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("serve", "status", "switch", "drain"))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--state", type=Path, help="durable route CAS state; defaults beside control socket")
    parser.add_argument("--update-lease-db", type=Path,
                        help="require current persistent update fence for control-socket switches")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--target")
    parser.add_argument("--expected")
    parser.add_argument("--generation")
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args(argv)
    add_backend_to_path()
    from app.runtime.platform_router import Backend, RouterState, control_request, serve_router

    if args.command == "serve":
        if args.config is None or not 0 < args.port <= 65535:
            parser.error("serve requires --config and nonzero --port")
        config = json.loads(args.config.read_text(encoding="utf-8"))
        backends = {item["generation"]: Backend(**item) for item in config["backends"]}
        state_path = args.state or args.socket.with_suffix(".state.json")
        serve_router(RouterState(backends, config["active"], state_path,
                                 automatic_failover=config.get("automatic_failover") is True),
                     args.host, args.port,
                     args.socket, update_lease_db=args.update_lease_db)
        return 0
    request = {"command": args.command}
    if args.command == "switch":
        if not args.target or not args.expected:
            parser.error("switch requires --target and --expected")
        request.update(target=args.target, expected=args.expected)
    if args.command == "drain":
        if not args.generation:
            parser.error("drain requires --generation")
        request.update(generation=args.generation, timeout=args.timeout)
    print(json.dumps(control_request(args.socket, request), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
