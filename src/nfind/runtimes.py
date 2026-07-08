"""Language runtimes (Python and Node.js) and package-name handling.

Each :class:`Runtime` bundles the per-ecosystem knowledge the host needs: which base
image and Dockerfile to use, how to validate the generated code's interface, how to
canonicalize package names, and how to install dependencies into a derived image.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from .constants import (
    DEFAULT_ALLOWED_PACKAGES,
    DEFAULT_IMAGE,
    DEFAULT_NODE_IMAGE,
    DEFAULT_NODE_PACKAGES,
    DEFAULT_RUNTIME,
)

# A conservative pip/PEP 503 package name: no version specifiers, URLs, or options.
_PACKAGE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
# An npm package name, optionally scoped (@scope/name).
_NPM_NAME = re.compile(r"^(@[a-z0-9][a-z0-9._-]{0,99}/)?[a-z0-9][a-z0-9._-]{0,99}$")


def _normalize_pip_name(name: str) -> str:
    """Canonicalize a PyPI name per PEP 503: collapse runs of -, _, . to - and lowercase.

    pip treats ``tree_sitter_python``, ``Tree-Sitter-Python``, and ``tree-sitter-python``
    as the same distribution; storing the canonical form keeps the whitelist free of
    near-duplicates and lets the model's wording match the built-in defaults.
    """
    return re.sub(r"[-_.]+", "-", name).lower()


def _normalize_npm_name(name: str) -> str:
    """Canonicalize an npm name: lowercase only (npm treats ``-`` and ``_`` as distinct)."""
    return name.lower()


@dataclass(frozen=True)
class Runtime:
    """A language ecosystem the generated filter can run in (Python or Node.js)."""

    name: str
    base_image: str
    dockerfile: str  # filename packaged next to this module
    final_user: str  # unprivileged user the worker runs as
    worker_uid: int  # numeric uid of ``final_user`` inside the image
    worker_gid: int  # numeric gid of ``final_user`` inside the image
    default_packages: frozenset[str]
    _package_name: re.Pattern[str]
    _validate_code: Callable[[str], None]
    _install: Callable[[Sequence[str]], str]  # derived-image install instructions
    _normalize: Callable[[str], str]  # canonicalize a package name for this ecosystem

    def validate_code(self, code: str) -> None:
        self._validate_code(code)

    def validate_packages(self, dependencies: Any) -> list[str]:
        return _validate_dependencies(dependencies, self._package_name, self._normalize)

    def normalize_name(self, name: str) -> str:
        return self._normalize(name)

    def derived_dockerfile(self, base: str, packages: Sequence[str]) -> str:
        return f"FROM {base}\nUSER root\n{self._install(packages)}\nUSER {self.final_user}\n"


@dataclass
class GeneratedFilter:
    """A generated filter: its source, runtime, and any packages it needs."""

    code: str
    dependencies: list[str] = field(default_factory=list)
    runtime: str = DEFAULT_RUNTIME


def _validate_code_shape(code: str) -> None:
    """Require one undecorated top-level filter function, optionally preceded by imports.

    Top-level ``import``/``from ... import`` statements are allowed (and preferred over
    function-local imports) so the filter's globals carry them at call time; any other
    top-level statement is rejected. This is an interface check, not a security boundary.
    Docker provides the isolation for the intentionally expressive generated Python.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise ValueError(f"Generated code has a syntax error: {exc}") from exc

    func_defs = (ast.FunctionDef, ast.AsyncFunctionDef)
    allowed = (ast.Import, ast.ImportFrom, *func_defs)
    functions = [n for n in tree.body if isinstance(n, func_defs)]
    extras = [n for n in tree.body if not isinstance(n, allowed)]
    if len(functions) != 1 or extras:
        raise ValueError(
            "Generated code must contain exactly one top-level function definition, "
            "preceded only by import statements."
        )
    function = functions[0]
    if isinstance(function, ast.AsyncFunctionDef):
        raise ValueError("filter_paths must be a synchronous function.")
    if function.name != "filter_paths":
        raise ValueError("Generated function must be named filter_paths.")
    if function.decorator_list:
        raise ValueError("filter_paths must not have decorators.")
    args = function.args
    if (
        len(args.args) != 1
        or args.args[0].arg != "paths"
        or args.posonlyargs
        or args.kwonlyargs
        or args.vararg is not None
        or args.kwarg is not None
        or args.defaults
        or args.kw_defaults
    ):
        raise ValueError("filter_paths must take exactly one argument named paths.")


_NODE_FILTER = re.compile(r"\bfilterPaths\b")


def _validate_node_code(code: str) -> None:
    """Light interface check for Node.js filters.

    Like the Python check, this is an interface check rather than a security
    boundary -- the container provides the isolation. We only confirm the code is
    non-empty and defines something named ``filterPaths``.
    """
    if not code.strip():
        raise ValueError("Generated code is empty.")
    if not _NODE_FILTER.search(code):
        raise ValueError("Generated code must define a filterPaths function.")


def _pip_install(packages: Sequence[str]) -> str:
    return "RUN pip install --no-cache-dir " + " ".join(packages)


def _npm_install(packages: Sequence[str]) -> str:
    return "WORKDIR /app\nRUN npm install --no-audit --no-fund --no-save " + " ".join(packages)


PYTHON_RUNTIME = Runtime(
    name="python",
    base_image=DEFAULT_IMAGE,
    dockerfile="Dockerfile.python",
    final_user="worker",
    worker_uid=10001,
    worker_gid=10001,
    default_packages=DEFAULT_ALLOWED_PACKAGES,
    _package_name=_PACKAGE_NAME,
    _validate_code=_validate_code_shape,
    _install=_pip_install,
    _normalize=_normalize_pip_name,
)

NODE_RUNTIME = Runtime(
    name="node",
    base_image=DEFAULT_NODE_IMAGE,
    dockerfile="Dockerfile.node",
    final_user="node",
    worker_uid=1000,
    worker_gid=1000,
    default_packages=DEFAULT_NODE_PACKAGES,
    _package_name=_NPM_NAME,
    _validate_code=_validate_node_code,
    _install=_npm_install,
    _normalize=_normalize_npm_name,
)

RUNTIMES: dict[str, Runtime] = {
    PYTHON_RUNTIME.name: PYTHON_RUNTIME,
    NODE_RUNTIME.name: NODE_RUNTIME,
}


def imply_packages(runtime_name: str, dependencies: Sequence[str]) -> list[str]:
    """Add packages implied by others that pip won't pull in automatically.

    The tree-sitter grammar wheels (``tree-sitter-<lang>``) declare the ``tree-sitter``
    core only as an optional extra, so installing a grammar alone leaves ``import
    tree_sitter`` failing. Whenever a grammar wheel is requested for the Python runtime,
    ensure the core is installed too.
    """
    deps = set(dependencies)
    if (
        runtime_name == DEFAULT_RUNTIME
        and any(d.startswith("tree-sitter-") for d in deps)
        and "tree-sitter" not in deps
    ):
        deps.add("tree-sitter")
    return sorted(deps)


def _validate_dependencies(
    dependencies: Any,
    pattern: re.Pattern[str] = _PACKAGE_NAME,
    normalize: Callable[[str], str] = _normalize_pip_name,
) -> list[str]:
    """Validate and canonicalize a requested dependency list to bare package names."""
    if not isinstance(dependencies, list) or not all(isinstance(d, str) for d in dependencies):
        raise ValueError("dependencies must be a list of package-name strings.")
    names: list[str] = []
    for dependency in dependencies:
        name = dependency.strip()
        if not pattern.match(name.lower()):
            raise ValueError(f"Invalid package name requested: {dependency!r}")
        names.append(normalize(name))
    return sorted(set(names))
