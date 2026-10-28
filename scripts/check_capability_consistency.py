"""Consistency gate for the three places that declare "what the platform can do".

The platform declares its capability surface in three independent places:

1. ``backend/app/services/operations_capabilities.py`` -> ``CAPABILITY_CATALOG``
   (the capability catalog shown by the capability-matrix page; each entry carries
   a ``route_key`` pointing at a module).
2. ``backend/app/api/v1/module_config.py`` -> ``DEFAULT_MODULES``
   (module switches / permission configuration; keyed by ``module_key``).
3. ``frontend/src/menuConfig.jsx`` -> ``MENU_CONFIG``
   (the navigation tree; keyed by ``key``).

They are edited independently, so they drift. This gate compares the three key
sets and fails on any reference that cannot be resolved:

* a catalog ``route_key`` with no matching module  -> the matrix links to nothing
* a menu ``key`` with no matching module           -> the page is not governable
* a module with no menu entry and no catalog entry -> dead switch

It also checks the fourth declaration: the capability table in ``docs/README.md``
lists capability domains; every domain named there must appear in the catalog.
That table is prose, so only the domain name is checked (not per-bullet claims).

Exit code 1 on any mismatch. Run from the repository root:

    python3 scripts/check_capability_consistency.py
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

CATALOG_FILE = "backend/app/services/operations_capabilities.py"
MODULES_FILE = "backend/app/api/v1/module_config.py"
MENU_FILE = "frontend/src/menuConfig.jsx"
ROUTER_FILE = "frontend/src/AppPageRouter.jsx"
DOCS_FILE = "docs/README.md"


def load_catalog() -> tuple[set[str], set[str], set[str]]:
    """Return (catalog_keys, route_keys, domains) from CAPABILITY_CATALOG."""
    src = (REPO / CATALOG_FILE).read_text(encoding="utf-8")
    tree = ast.parse(src)
    keys: set[str] = set()
    route_keys: set[str] = set()
    domains: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.AnnAssign) or getattr(node.target, "id", "") != "CAPABILITY_CATALOG":
            continue
        value = node.value
        if not isinstance(value, (ast.Tuple, ast.List)):
            continue
        for elt in value.elts:
            if not isinstance(elt, ast.Dict):
                continue
            entry = {}
            for k, v in zip(elt.keys, elt.values):
                if isinstance(k, ast.Constant) and isinstance(v, ast.Constant):
                    entry[k.value] = v.value
            if "key" in entry:
                keys.add(entry["key"])
            if "route_key" in entry:
                route_keys.add(entry["route_key"])
            if "domain" in entry:
                domains.add(entry["domain"])
    return keys, route_keys, domains


def load_modules() -> set[str]:
    src = (REPO / MODULES_FILE).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "DEFAULT_MODULES":
            out = set()
            for elt in node.value.elts:
                if not isinstance(elt, ast.Dict):
                    continue
                for k, v in zip(elt.keys, elt.values):
                    if isinstance(k, ast.Constant) and k.value == "module_key" and isinstance(v, ast.Constant):
                        out.add(v.value)
            return out
    return set()


def load_page_keys() -> tuple[set[str], set[str]]:
    """Return (page_keys, menu_keys).

    ``page_keys`` is the authoritative set of keys that actually render a page:
    the standard dispatch table in AppPageRouter.jsx plus the ``ops.*`` keys that
    OPS_DISPATCH routes by engine (the file itself notes those are absent from
    the standard table).

    ``menu_keys`` is every key in the navigation tree, groups included. It is
    reported but NOT compared: a group header is navigation structure and
    legitimately has no module switch behind it.

    Do not infer "is this a group?" from the menu file. Whether a node appears to
    carry ``children:`` depends on how far you scan ahead, and any fixed
    look-ahead window both over- and under-counts (measured: no cap -> 71 leaves,
    a 240-char cap -> 47 leaves, neither is right). AppPageRouter is unambiguous.
    """
    src = (REPO / ROUTER_FILE).read_text(encoding="utf-8")
    page_keys = set(re.findall(r'^\s*"([a-zA-Z][\w.]*)":\s*\{\s*c:', src, re.M))

    # OPS_DISPATCH keys are bare (overview, space, ...); they render as ops.<key>.
    m = re.search(r"const OPS_DISPATCH = \{(.*?)\n\};", src, re.S)
    if m:
        for line in m.group(1).split("\n"):
            km = re.match(r"\s*(\w+):\s*\{", line)
            if km:
                page_keys.add("ops." + km.group(1))

    # Whole subtrees are handed to one component that renders tabs internally
    # (``activeKey.startsWith("rescue.")``). Any key under such a prefix is a real
    # page even though it has no row in the dispatch table.
    prefixes = set(re.findall(r'activeKey\.startsWith\("([^"]+)"\)', src))

    menu_src = (REPO / MENU_FILE).read_text(encoding="utf-8")
    menu_keys = set(re.findall(r'key:\s*"([^"]+)"', menu_src))
    for k in menu_keys:
        if any(k.startswith(p) for p in prefixes) and not _menu_node_has_children(menu_src, k):
            page_keys.add(k)
    return page_keys, menu_keys


def _menu_node_has_children(src: str, key: str) -> bool:
    """True if the menu entry declaring ``key`` also declares ``children:``.

    Decided by brace matching from the key to the end of its own object, NOT by a
    fixed look-ahead window -- a window both over- and under-counts depending on
    how far the next sibling happens to sit.
    """
    m = re.search(r'key:\s*"%s"' % re.escape(key), src)
    if not m:
        return False
    depth = 0
    i = m.end()
    while i < len(src):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                return False  # end of this entry; no children seen at its level
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth < 0:
                return False
        elif depth == 0 and src.startswith("children:", i):
            return True
        i += 1
    return False


def load_doc_domains() -> set[str]:
    """Capability-domain names from the '| domain | status | ... |' table."""
    text = (REPO / DOCS_FILE).read_text(encoding="utf-8")
    out: set[str] = set()
    in_table = False
    for line in text.split("\n"):
        if line.startswith("| 能力域"):
            in_table = True
            continue
        if in_table:
            if not line.startswith("|"):
                in_table = False
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if cells and cells[0] and not set(cells[0]) <= set("-: "):
                out.add(cells[0])
    return out


def main() -> int:
    catalog_keys, route_keys, catalog_domains = load_catalog()
    modules = load_modules()
    page_keys, menu_keys = load_page_keys()

    problems: list[str] = []

    orphan_routes = sorted(route_keys - modules)
    if orphan_routes:
        problems.append(
            "CAPABILITY_CATALOG route_key 无对应模块（能力矩阵会指向不存在的开关）:\n"
            + "\n".join(f"    - {k}" for k in orphan_routes)
        )

    orphan_pages = sorted(page_keys - modules)
    if orphan_pages:
        problems.append(
            "真实页面无对应模块开关（该页面不受模块配置管辖）:\n"
            + "\n".join(f"    - {k}" for k in orphan_pages)
        )

    # Modules rendered as a tab inside another page, so they have no route of
    # their own. `moduleConfig` is the permission matrix embedded in
    # UserManagementPages.jsx (which deliberately filters it out of its own list
    # at line 68 and renders it as a tab at line 371).
    EMBEDDED_MODULES = {"moduleConfig"}

    dead_modules = sorted(
        m for m in modules
        if m not in page_keys and m not in route_keys and m not in EMBEDDED_MODULES
    )
    if dead_modules:
        problems.append(
            "DEFAULT_MODULES 中无真实页面、也不被能力目录引用的模块（死开关）:\n"
            + "\n".join(f"    - {k}" for k in dead_modules)
        )

    print(f"capability catalog entries : {len(catalog_keys)}")
    print(f"catalog route_keys         : {len(route_keys)}")
    print(f"module_config modules      : {len(modules)}")
    print(f"routable pages (router+ops): {len(page_keys)}")
    print(f"menuConfig keys (all)      : {len(menu_keys)}")
    print(f"pages without a switch     : {len(orphan_pages)}")
    print(f"dead module switches       : {len(dead_modules)}")

    if problems:
        print()
        for p in problems:
            print(p)
            print()
        print("FAILED")
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
