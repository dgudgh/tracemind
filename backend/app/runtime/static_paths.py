import os


def resolve_static_file(static_dir: str, requested_path: str, fallback: str = "index.html") -> str:
    root = os.path.realpath(static_dir)
    relative = (requested_path or "").replace("\\", "/").lstrip("/")
    candidate = os.path.realpath(os.path.join(root, relative))
    try:
        inside_root = os.path.commonpath([root, candidate]) == root
    except ValueError:
        inside_root = False
    if not inside_root:
        raise ValueError("static path escapes configured root")
    if relative and os.path.isfile(candidate):
        return candidate

    fallback_path = os.path.realpath(os.path.join(root, fallback))
    if os.path.commonpath([root, fallback_path]) != root:
        raise ValueError("static fallback escapes configured root")
    return fallback_path
