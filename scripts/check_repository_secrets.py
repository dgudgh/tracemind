from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAX_TEXT_BYTES = 5 * 1024 * 1024
ALLOWED_ENV_FILES = {"backend/.env.example"}
SENSITIVE_SUFFIXES = {".key", ".pem", ".p12", ".pfx"}
SENSITIVE_NAMES = {"license.json", "gen_license.py", "license_tool.py"}
PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----"),
    "credential in URL": re.compile(r"[a-z][a-z0-9+.-]*://[^/\s:@]+:[^@\s/]+@", re.IGNORECASE),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "GitHub token": re.compile(r"\b(?:ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b"),
    "Slack token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    "OpenAI-style key": re.compile(r"\bsk-[A-Za-z0-9_-]{32,}\b"),
}


def is_safe_placeholder(label: str, line: str) -> bool:
    if label != "credential in URL":
        return False
    return bool(
        re.search(r"<[A-Z][A-Z0-9_]*>", line)
        or "replace-with-" in line.lower()
        or ("{" in line and "}" in line)
    )


def git_paths(include_untracked: bool) -> list[str]:
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
    paths = {path for path in tracked if path}
    if include_untracked:
        untracked = subprocess.check_output(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=ROOT
        ).decode().split("\0")
        paths.update(path for path in untracked if path)
    return sorted(paths)


def sensitive_path_reason(relative_path: str) -> str | None:
    path = Path(relative_path)
    lowered_parts = {part.lower() for part in path.parts}
    if relative_path in ALLOWED_ENV_FILES:
        return None
    if path.name == ".env" or path.name.startswith(".env.") or path.suffix.lower() == ".env":
        return "environment configuration"
    if path.suffix.lower() in SENSITIVE_SUFFIXES:
        return "private credential file"
    if path.name.lower() in SENSITIVE_NAMES or "license_tool" in lowered_parts:
        return "license generation material"
    return None


def scan(include_untracked: bool = False) -> list[tuple[str, int, str]]:
    findings: list[tuple[str, int, str]] = []
    for relative_path in git_paths(include_untracked):
        path_reason = sensitive_path_reason(relative_path)
        if path_reason:
            findings.append((relative_path, 0, path_reason))
            continue
        path = ROOT / relative_path
        if not path.is_file() or path.stat().st_size > MAX_TEXT_BYTES:
            continue
        content = path.read_bytes()
        if b"\0" in content:
            continue
        text = content.decode("utf-8", errors="ignore")
        for line_number, line in enumerate(text.splitlines(), start=1):
            if "secret-scan: allow" in line:
                continue
            for label, pattern in PATTERNS.items():
                if pattern.search(line) and not is_safe_placeholder(label, line):
                    findings.append((relative_path, line_number, label))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description="Reject high-confidence secrets and sensitive files from Git.")
    parser.add_argument("--include-untracked", action="store_true", help="Also inspect untracked, non-ignored files.")
    args = parser.parse_args()
    findings = scan(args.include_untracked)
    print(f"Secret scan: files={len(git_paths(args.include_untracked))} findings={len(findings)}")
    for path, line, reason in findings:
        location = f"{path}:{line}" if line else path
        print(f"- {location}: {reason}")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
