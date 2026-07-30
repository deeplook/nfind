"""Guard against imports that aren't backed by a declared runtime dependency.

Regression test for the 0.3.0 release, where `cli.py` imported `click` directly
but only `typer` was declared in `[project.dependencies]`. `uv sync` (used by CI
and local dev) pulls in the whole `dev` group too, and `mkdocs` (a docs dev
dependency) happens to depend on `click`, so the import silently worked in every
environment that ran the test suite. A plain `uv tool install .` or `pip install
nfind`, which resolves only the declared runtime dependencies, then failed with
`ModuleNotFoundError: No module named 'click'`. Comparing actual top-level
imports against `[project.dependencies]` (plus optional-dependencies) catches
that class of drift regardless of what else happens to be installed.
"""

from __future__ import annotations

import ast
import sys
import tomllib
from pathlib import Path

SRC = Path(__file__).parent.parent / "src" / "nfind"
PYPROJECT = Path(__file__).parent.parent / "pyproject.toml"


def _normalize(name: str) -> str:
    return name.lower().replace("-", "_")


def _top_level_imports() -> set[str]:
    mods: set[str] = set()
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods.add(node.module.split(".")[0])
    stdlib = sys.stdlib_module_names
    return {m for m in mods if m not in stdlib and m != "nfind"}


def _declared_dependency_names() -> set[str]:
    data = tomllib.loads(PYPROJECT.read_text())
    project = data["project"]
    specs = list(project["dependencies"])
    for extra_deps in project.get("optional-dependencies", {}).values():
        specs.extend(extra_deps)
    names = set()
    for spec in specs:
        # Strip version specifiers/markers/extras: "sqlite-vec>=0.1" -> "sqlite-vec".
        name = spec.split(";")[0]
        for sep in ("[", ">", "<", "=", "!", "~", " "):
            name = name.split(sep)[0]
        names.add(_normalize(name))
    return names


def test_every_import_has_a_declared_dependency() -> None:
    imports = {_normalize(m) for m in _top_level_imports()}
    declared = _declared_dependency_names()
    missing = imports - declared
    assert not missing, (
        f"src/nfind imports {missing} directly, but they are not declared in "
        "pyproject.toml's [project.dependencies] (or optional-dependencies). "
        "Relying on a transitive dependency is fragile: a dependency update can "
        "drop it (as typer did with click), or it may only be present because "
        "of a dev-only package like mkdocs."
    )
