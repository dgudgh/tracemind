from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]

TEXT_SUFFIXES = {
    ".bat",
    ".cjs",
    ".css",
    ".html",
    ".js",
    ".json",
    ".jsx",
    ".md",
    ".mjs",
    ".ps1",
    ".py",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".yaml",
    ".yml",
}

SEARCH_ROOTS = [
    ROOT / "backend" / "app",
    ROOT / "docs",
    ROOT / "frontend" / "src",
    ROOT / "scripts",
    ROOT / "work",
]

EXTRA_FILES = [
    ROOT / "backend" / "requirements.txt",
    ROOT / "backend" / "run.py",
    ROOT / "frontend" / "index.html",
    ROOT / "frontend" / "package-lock.json",
    ROOT / "frontend" / "package.json",
    ROOT / "frontend" / "serve-3000.cjs",
    ROOT / "frontend" / "vite.config.js",
    ROOT / "run.py",
]

EXCLUDED_DIR_NAMES = {
    ".git",
    ".idea",
    "__pycache__",
    "dist",
    "node_modules",
}

SUSPICIOUS_TEXT_PATTERNS = {
    "replacement-character": re.compile("\ufffd"),
    "mojibake": re.compile("\u951f"),
    "common-mojibake": re.compile(r"[\u9427\u93ba\u9422\u7487\u95bf\u935a\u7035\u6ae5\u7d8d\u7e43\u93c8\u71c2\u7d1d\u5678\u93c2\u626e\u8930\u7e3a\u5a32]"),
    "question-placeholder": re.compile(r"\?{4,}"),
}


def is_excluded(path: Path) -> bool:
    try:
        relative_parts = path.relative_to(ROOT).parts
    except ValueError:
        relative_parts = path.parts
    return any(part in EXCLUDED_DIR_NAMES for part in relative_parts)


def iter_targets() -> list[Path]:
    targets: set[Path] = set()
    for root in SEARCH_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file() and not is_excluded(path) and path.suffix.lower() in TEXT_SUFFIXES:
                targets.add(path)
    for path in EXTRA_FILES:
        if path.exists() and path.is_file() and not is_excluded(path):
            targets.add(path)
    return sorted(targets, key=lambda item: item.as_posix().lower())


def main() -> int:
    failed = []
    suspicious = []
    targets = iter_targets()
    for path in targets:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            failed.append((path, exc))
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for name, pattern in SUSPICIOUS_TEXT_PATTERNS.items():
                if pattern.search(line):
                    suspicious.append((path, lineno, name, line.strip()[:160]))
    if failed:
        for path, exc in failed:
            print(f"ENCODING_ERROR {path}: {exc}")
        return 1
    if suspicious:
        for path, lineno, name, text in suspicious:
            print(f"SUSPICIOUS_TEXT {path}:{lineno} {name}: {text}")
        return 1
    print(f"UTF-8 OK: {len(targets)} files checked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
