#!/usr/bin/env python3
"""Ordinary platform login and capability-plan client; no management IPC access."""
from __future__ import annotations

import argparse
import getpass
import json
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def request(base, path, *, token=None, payload=None, key=None):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if key:
        headers["Idempotency-Key"] = key
    data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = Request(base.rstrip("/") + "/api/v1" + path, data=data, headers=headers,
                  method="POST" if data is not None else "GET")
    try:
        with urlopen(req, timeout=30) as response:
            return json.load(response)
    except HTTPError as exc:
        # Never print credentials, bearer tokens, raw exceptions or server traces.
        try:
            body = json.loads(exc.read(4096))
            detail = body.get("detail", {})
            code = detail.get("code") if isinstance(detail, dict) else None
        except (ValueError, UnicodeError):
            code = None
        raise RuntimeError(f"platform_http_{exc.code}:{code or 'request_failed'}") from None
    except URLError:
        raise RuntimeError("platform_unavailable") from None


def login(base, username):
    password = getpass.getpass("Platform password: ")
    reply = request(base, "/auth/login", payload={"username": username, "password": password})
    del password
    if reply.get("mfa_required"):
        code = getpass.getpass("MFA code: ")
        reply = request(base, "/auth/mfa/verify", payload={"challenge_token": reply["challenge_token"],
                                                              "code": code})
    token = reply.get("token")
    if not isinstance(token, str) or not token:
        raise RuntimeError("platform_login_incomplete")
    return token


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--username", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    submit = commands.add_parser("submit", help="Create and enqueue a pinned read plan")
    submit.add_argument("--instance-id", type=int, required=True)
    submit.add_argument("--request-id", required=True)
    read = commands.add_parser("read-result", help="Read a persisted plan and core-decoded result")
    read.add_argument("--plan-id", required=True)
    args = parser.parse_args()
    try:
        token = login(args.base_url, args.username)
        if args.command == "submit":
            plan = request(args.base_url, "/assistant/plans", token=token,
                           key=args.request_id, payload={"capability_id": "operations.session.read",
                           "definition_version": 2, "instance_id": args.instance_id, "params": {}})
            if plan.get("status") != "validated":
                raise RuntimeError("plan_not_validated:" + str(plan.get("outcome_kind")))
            receipt = request(args.base_url, f"/assistant/plans/{plan['plan_id']}/execute",
                              token=token, payload={})
            print(json.dumps({"plan_id": plan["plan_id"], "status": receipt.get("status"),
                              "batch_id": receipt.get("execution_batch_id")}, ensure_ascii=False))
        else:
            value = request(args.base_url, f"/assistant/plans/{args.plan_id}", token=token)
            print(json.dumps({"plan_id": args.plan_id, "status": value.get("status"),
                              "outcome": value.get("outcome"),
                              "engine_result": value.get("engine_result")}, ensure_ascii=False))
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
