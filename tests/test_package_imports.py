"""Every module in the package must import, and every relative import must land.

The split of ``client.py`` into concern modules moved code between packages.
A relative import keeps its text but not its depth, so ``from .containers
import x`` that used to mean ``protobot.containers`` means
``protobot.play.containers`` once the line lives in ``protobot/play/router.py``
-- and because such an import usually sits inside a function, nothing notices
until that function runs on a live server.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "protobot"


def module_name(path: Path) -> str:
    relative = path.relative_to(PACKAGE.parent).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def resolve(current: str, level: int, module: str | None) -> str:
    parts = current.split(".")
    if current.endswith(".__init__"):
        parts = parts[:-1]
    package = parts[:-1] if not current.endswith("__init__") else parts
    if level > 1:
        package = package[: len(package) - (level - 1)]
    return ".".join(filter(None, [".".join(package), module or ""]))


class PackageImportTests(unittest.TestCase):
    def test_every_module_imports(self) -> None:
        for path in sorted(PACKAGE.rglob("*.py")):
            if "examples" in path.parts:
                continue
            with self.subTest(module=module_name(path)):
                importlib.import_module(module_name(path))

    def test_relative_imports_resolve(self) -> None:
        problems: list[str] = []
        for path in sorted(PACKAGE.rglob("*.py")):
            if "examples" in path.parts:
                continue
            current = module_name(path)
            if path.name == "__init__.py":
                current += ".__init__"
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom) or not node.level:
                    continue
                target = resolve(current, node.level, node.module)
                if importlib.util.find_spec(target) is None:
                    problems.append(
                        f"{path.relative_to(PACKAGE.parent)}:{node.lineno}: {target}"
                    )
        self.assertEqual(problems, [], "relative imports that do not resolve")
