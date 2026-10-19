"""Report module-level imports that nothing in the module refers to.

Moving code between modules leaves imports behind, and a dead import is also a
wrong hint about what a module depends on.  This check reads the syntax tree
instead of the text, so names that only appear inside strings (``__all__``) or
at the top of a re-exporting ``__init__`` do not count as uses.

Skips, on purpose:

* ``__init__.py`` -- re-export modules import names they never mention again.
* names listed in ``__all__``.
* an import line or block that carries an ``# re-exported`` or ``# noqa``
  comment, for the few modules that re-export on purpose (``client.py`` keeps
  the names it inherited from ``support`` importable from where they used to be).

Usage:
    python tools/check_imports.py [root ...]     # default: protobot tests tools

Exits 1 when anything is unused, so CI can gate on it.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Iterable
from pathlib import Path

DEFAULT_ROOTS = ("protobot", "tests", "tools")


def imported_names(node: ast.AST) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    if isinstance(node, ast.Import):
        for alias in node.names:
            found.append((alias.asname or alias.name.split(".")[0], node.lineno))
    elif isinstance(node, ast.ImportFrom):
        for alias in node.names:
            if alias.name == "*":
                continue
            found.append((alias.asname or alias.name, node.lineno))
    return found


def used_names(tree: ast.AST) -> set[str]:
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            current: ast.AST = node
            while isinstance(current, ast.Attribute):
                current = current.value
            if isinstance(current, ast.Name):
                used.add(current.id)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            used.add(node.value)
    return used


def exported_names(tree: ast.AST) -> set[str]:
    exported: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "__all__"
            for target in node.targets
        ):
            continue
        if isinstance(node.value, (ast.List, ast.Tuple)):
            for element in node.value.elts:
                if isinstance(element, ast.Constant) and isinstance(element.value, str):
                    exported.add(element.value)
    return exported


def check(path: Path) -> list[str]:
    # ``utf-8-sig`` so an editor that saved a byte-order mark does not turn
    # into a syntax error here: Python accepts the mark, ``ast.parse`` does not.
    source = path.read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    if path.name == "__init__.py":
        return []
    lines = source.splitlines()
    used = used_names(tree) | exported_names(tree)
    problems: list[str] = []
    for node in tree.body:
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            continue
        block = lines[node.lineno - 1 : (node.end_lineno or node.lineno)]
        if any("re-exported" in line or "noqa" in line for line in block):
            continue
        for name, line in imported_names(node):
            if name not in used:
                problems.append(f"{path}:{line}: unused import {name}")
    return problems


def python_files(roots: Iterable[str]) -> Iterable[Path]:
    for root in roots:
        directory = Path(root)
        if directory.is_file():
            yield directory
        elif directory.is_dir():
            yield from sorted(directory.rglob("*.py"))


def main(argv: list[str]) -> int:
    roots = argv[1:] or list(DEFAULT_ROOTS)
    problems: list[str] = []
    for path in python_files(roots):
        problems.extend(check(path))
    if not problems:
        print(f"no unused imports in {', '.join(roots)}")
        return 0
    for problem in problems:
        print(problem)
    print(f"{len(problems)} unused import(s) in {', '.join(roots)}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
