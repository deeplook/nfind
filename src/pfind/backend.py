#!/usr/bin/env python3
"""Search paths with an LLM-generated Python filter executed inside Docker.

The host enumerates the search tree and asks the model for code.  The generated
code runs in a disposable container with the search root mounted at /data as
read-only.  Only paths supplied by the host may be returned.

This module is also the entry point for the in-container worker: the packaged
``Dockerfile`` runs ``python backend.py --worker``, which reads a JSON request on
stdin and writes a JSON response on stdout.
"""

from __future__ import annotations

import ast
import contextlib
import ctypes
import ctypes.util
import hashlib
import io
import json
import os
import plistlib
import re
import signal
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

DEFAULT_IMAGE = "pfind-search-paths:latest"
DEFAULT_NODE_IMAGE = "pfind-search-node:latest"
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_RUNTIME = "python"
# How many times to ask the model in total when its reply fails validation. The
# first attempt runs at temperature 0; retries feed the error back and nudge the
# temperature up so the model diverges from the response that just failed.
DEFAULT_GENERATION_ATTEMPTS = 3
_RETRY_TEMPERATURE = 0.3
MAX_RESULT_BYTES = 1_000_000
DOCKER_CHECK_TIMEOUT = 10.0
DEFAULT_BUILD_TIMEOUT = 120.0

# Python packages the filter may request without an explicit approval prompt. These
# are common, well-known, read-only analysis libraries. Anything outside this set
# (and outside the user's saved whitelist) must be confirmed before it is installed.
DEFAULT_ALLOWED_PACKAGES = frozenset(
    {
        "chardet",
        "mutagen",
        "pillow",
        "pillow-heif",
        "pdfminer-six",
        "pypdf",
        "python-magic",
        "pyyaml",
        "tinytag",
        "tomli",
        # Multi-language syntactic parsing: tree-sitter core plus a bundle of
        # precompiled grammars, so filters can query source structure (functions,
        # imports, ...) across many languages from the Python runtime.
        "tree-sitter",
        "tree-sitter-language-pack",
    }
)

# npm packages pre-approved for the Node.js runtime: source-analysis tooling.
DEFAULT_NODE_PACKAGES = frozenset(
    {
        "@babel/parser",
        "acorn",
        "esprima",
        "fast-xml-parser",
        "ts-morph",
        "typescript",
        "yaml",
    }
)

# macOS extended attributes surfaced by --macos-meta. These live on the host and do
# not reliably survive Docker's file-sharing layer into the Linux container, so they
# are read host-side and passed into the sandbox alongside the paths.
_XATTR_TAGS = "com.apple.metadata:_kMDItemUserTags"
_XATTR_WHERE_FROMS = "com.apple.metadata:kMDItemWhereFroms"
_XATTR_QUARANTINE = "com.apple.quarantine"
_XATTR_NOFOLLOW = 0x0001  # macOS getxattr option: do not follow symlinks

# A conservative pip/PEP 503 package name: no version specifiers, URLs, or options.
_PACKAGE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
# An npm package name, optionally scoped (@scope/name).
_NPM_NAME = re.compile(r"^(@[a-z0-9][a-z0-9._-]{0,99}/)?[a-z0-9][a-z0-9._-]{0,99}$")


class DockerError(RuntimeError):
    """Base class for actionable Docker lifecycle failures."""


class DockerUnavailableError(DockerError):
    """Raised when the Docker CLI or daemon cannot be reached."""


class DependencyError(RuntimeError):
    """Raised when a filter needs packages that were not approved for install."""


@dataclass(frozen=True)
class Runtime:
    """A language ecosystem the generated filter can run in (Python or Node.js)."""

    name: str
    base_image: str
    dockerfile: str  # filename packaged next to this module
    final_user: str  # unprivileged user the worker runs as
    default_packages: frozenset[str]
    _package_name: re.Pattern[str]
    _validate_code: Callable[[str], None]
    _install: Callable[[Sequence[str]], str]  # derived-image install instructions

    def validate_code(self, code: str) -> None:
        self._validate_code(code)

    def validate_packages(self, dependencies: Any) -> list[str]:
        return _validate_dependencies(dependencies, self._package_name)

    def derived_dockerfile(self, base: str, packages: Sequence[str]) -> str:
        return (
            f"FROM {base}\n"
            "USER root\n"
            f"{self._install(packages)}\n"
            f"USER {self.final_user}\n"
        )


@dataclass
class GeneratedFilter:
    """A generated filter: its source, runtime, and any packages it needs."""

    code: str
    dependencies: list[str] = field(default_factory=list)
    runtime: str = DEFAULT_RUNTIME


_SYSTEM = """\
You generate a file-filtering program. Given a description of which paths to select,
respond with a single JSON object of exactly this shape:

  {"runtime": "python" | "node", "dependencies": [...], "code": "..."}

Choose "runtime":
  * "python" (the default) for almost everything.
  * "node" only when the task is clearly better solved with the JavaScript/TypeScript
    ecosystem -- for example parsing TypeScript with ts-morph or a JS AST library.
    Prefer "python" when either ecosystem would do.

"code" defines one function that takes a single argument `paths`: the list of
absolute container paths below /data (both files and directories). It returns the
matching entries, each corresponding to one of the input paths, as either:

  * a flat list of matching path strings, or
  * a list of objects each having a "path" field (exactly one of the input paths)
    plus any additional fields the description asks for, for example
    {"path": p, "lines": 42}.

Use the object form only when the description requests extra per-path information;
otherwise return a plain list of paths.

For "python": name the function `filter_paths`; "code" must contain only that
function definition (no markdown, no decorators, no top-level statements).
"dependencies" lists any third-party PyPI packages it imports (pip names), e.g.
["mutagen"] to read audio tags; use [] when the standard library suffices.

For "node": write CommonJS that defines a function `filterPaths(paths)` and uses
`require(...)` for any packages. "dependencies" lists npm package names, e.g.
["ts-morph"]; use [] when none are needed.

The code runs in a disposable Linux container: /data is read-only, the network is
disabled, and resources are limited. Prefer the standard library and an empty
dependency list whenever practical. Respond with only the JSON object.
"""

_USER_TEMPLATE = """\
Generate a filter that includes only the paths matching this description:
{prompt}

Respond with the JSON object containing "runtime", "dependencies", and "code".
"""

_MACOS_META_SYSTEM = """\
macOS metadata is available for this run. When you choose the "python" runtime, a
global dict named META is in scope (do not define it yourself). It maps a path string
-- one of the values in `paths` -- to that path's macOS metadata. Only paths that have
metadata appear, so always use META.get(path, {}). Each value is a dict that may
contain:
  * "tags": list of Finder tag names, e.g. ["Red", "Work"]
  * "quarantined": true when the file carries a download (quarantine) flag
  * "where_froms": list of source URLs the file was downloaded from
Example: return [p for p in paths if META.get(p, {}).get("quarantined")]
META exists only in the python runtime; prefer python when the description mentions
Finder tags, downloads / where-from, or other macOS metadata.
"""

_RETRY_TEMPLATE = """\
Your previous response was rejected: {error}

Fix the problem and respond again with only the JSON object containing
"runtime", "dependencies", and "code", matching the required shape exactly.
"""


def _normalize_results(results: Any, allowed: set[str]) -> list[dict[str, Any]]:
    """Coerce filter output into path records and verify each path was supplied.

    Accepts either a list of path strings or a list of dicts carrying a "path"
    key plus extra fields. Returns a list of dicts that always contain "path".
    """
    if not isinstance(results, list):
        raise ValueError("filter_paths must return a list.")
    records: list[dict[str, Any]] = []
    for item in results:
        if isinstance(item, str):
            record: dict[str, Any] = {"path": item}
        elif isinstance(item, dict):
            path = item.get("path")
            if not isinstance(path, str):
                raise ValueError("each result dict must have a string 'path'.")
            record = dict(item)
            record["path"] = path
        else:
            raise ValueError("filter_paths results must be strings or dicts with a 'path'.")
        if record["path"] not in allowed:
            raise ValueError("filter_paths returned a path outside its input set.")
        records.append(record)
    if len(records) > len(allowed):
        raise ValueError("filter_paths returned more results than input paths.")
    return records


def _strip_code_fence(code: str) -> str:
    code = code.strip()
    if not code.startswith("```"):
        return code
    lines = code.splitlines()
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines[1:]).strip()


def _validate_code_shape(code: str) -> None:
    """Require one undecorated top-level filter function.

    This is an interface check, not a security boundary. Docker provides the
    isolation for the intentionally expressive generated Python.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise ValueError(f"Generated code has a syntax error: {exc}") from exc

    if len(tree.body) != 1 or not isinstance(tree.body[0], (ast.FunctionDef, ast.AsyncFunctionDef)):
        raise ValueError("Generated code must contain exactly one top-level function definition.")
    function = tree.body[0]
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
    default_packages=DEFAULT_ALLOWED_PACKAGES,
    _package_name=_PACKAGE_NAME,
    _validate_code=_validate_code_shape,
    _install=_pip_install,
)

NODE_RUNTIME = Runtime(
    name="node",
    base_image=DEFAULT_NODE_IMAGE,
    dockerfile="Dockerfile.node",
    final_user="node",
    default_packages=DEFAULT_NODE_PACKAGES,
    _package_name=_NPM_NAME,
    _validate_code=_validate_node_code,
    _install=_npm_install,
)

RUNTIMES: dict[str, Runtime] = {PYTHON_RUNTIME.name: PYTHON_RUNTIME, NODE_RUNTIME.name: NODE_RUNTIME}


def _validate_dependencies(dependencies: Any, pattern: re.Pattern[str] = _PACKAGE_NAME) -> list[str]:
    """Validate and normalize a requested dependency list to bare package names."""
    if not isinstance(dependencies, list) or not all(isinstance(d, str) for d in dependencies):
        raise ValueError("dependencies must be a list of package-name strings.")
    names: list[str] = []
    for dependency in dependencies:
        name = dependency.strip()
        if not pattern.match(name.lower()):
            raise ValueError(f"Invalid package name requested: {dependency!r}")
        names.append(name.lower())
    return sorted(set(names))


def _parse_generation(content: str) -> GeneratedFilter:
    """Parse and validate the model's JSON response into a GeneratedFilter."""
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Model response was not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Model response must be a JSON object.")
    runtime_name = payload.get("runtime", DEFAULT_RUNTIME)
    if runtime_name not in RUNTIMES:
        raise ValueError(f"Unknown runtime requested: {runtime_name!r}")
    runtime = RUNTIMES[runtime_name]
    code = payload.get("code")
    if not isinstance(code, str):
        raise ValueError("Model response is missing a 'code' string.")
    code = _strip_code_fence(code)
    runtime.validate_code(code)
    dependencies = runtime.validate_packages(payload.get("dependencies", []))
    return GeneratedFilter(code=code, dependencies=dependencies, runtime=runtime_name)


def generate_filter(
    prompt: str,
    model: str = DEFAULT_MODEL,
    *,
    attempts: int = DEFAULT_GENERATION_ATTEMPTS,
    on_retry: Callable[[int, ValueError], None] | None = None,
    macos_meta: bool = False,
) -> GeneratedFilter:
    """Generate a filter on the host, where the API credentials remain.

    Returns the filter source together with any third-party packages the model
    says it needs.

    When the model's reply fails validation (malformed JSON, wrong function shape,
    an invalid package name, ...), the error is fed back to the model and the
    request retried up to ``attempts`` times in total. The first attempt runs at
    temperature 0; retries nudge the temperature up so the model diverges from the
    reply that just failed. ``on_retry``, if given, is called with the 1-based
    retry number and the validation error before each retry.
    """
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("The host requires the 'openai' package to generate a filter.") from exc

    client = OpenAI()
    system = _SYSTEM + "\n" + _MACOS_META_SYSTEM if macos_meta else _SYSTEM
    messages: list[dict[str, str]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": _USER_TEMPLATE.format(prompt=prompt)},
    ]
    last_error: ValueError | None = None
    for attempt in range(attempts):
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=4096,
            temperature=0 if attempt == 0 else _RETRY_TEMPERATURE,
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content or ""
        try:
            return _parse_generation(content)
        except ValueError as exc:
            last_error = exc
            if attempt + 1 < attempts:
                if on_retry is not None:
                    on_retry(attempt + 1, exc)
                messages.append({"role": "assistant", "content": content})
                messages.append({"role": "user", "content": _RETRY_TEMPLATE.format(error=exc)})

    assert last_error is not None  # the loop only exits early via return
    raise ValueError(
        f"Model did not return a valid filter after {attempts} attempt(s): {last_error}"
    ) from last_error


def enumerate_paths(search_root: Path) -> tuple[list[str], dict[str, str]]:
    """Return container paths and a container-to-host result mapping."""
    root = search_root.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"Search root is not a directory: {root}")

    container_paths: list[str] = []
    host_by_container: dict[str, str] = {}
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in [*directories, *files]:
            host_path = current_path / name
            relative = host_path.relative_to(root)
            container_path = str(PurePosixPath("/data", *relative.parts))
            container_paths.append(container_path)
            host_by_container[container_path] = str(host_path)
    return container_paths, host_by_container


_libc_getxattr: Callable[..., int] | None = None


def _getxattr(path: str, name: str) -> bytes | None:
    """Read a single macOS extended attribute, or None if it is absent.

    Uses libc ``getxattr`` directly (CPython's ``os.getxattr`` is Linux-only) and
    does not follow symlinks, matching the symlink-free host enumeration.
    """
    global _libc_getxattr
    if _libc_getxattr is None:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        libc.getxattr.restype = ctypes.c_ssize_t
        libc.getxattr.argtypes = [
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_uint32,
            ctypes.c_int,
        ]
        _libc_getxattr = libc.getxattr

    path_bytes = os.fsencode(path)
    name_bytes = name.encode()
    size = _libc_getxattr(path_bytes, name_bytes, None, 0, 0, _XATTR_NOFOLLOW)
    if size < 0:
        return None
    if size == 0:
        return b""
    buffer = ctypes.create_string_buffer(size)
    read = _libc_getxattr(path_bytes, name_bytes, buffer, size, 0, _XATTR_NOFOLLOW)
    if read < 0:
        return None
    return buffer.raw[:read]


def _plist_strings(raw: bytes | None) -> list[str]:
    """Decode a binary-plist array of strings, tolerating malformed values."""
    if not raw:
        return []
    try:
        values = plistlib.loads(raw)
    except Exception:  # noqa: BLE001 - any decode failure means "no usable value"
        return []
    return [v for v in values if isinstance(v, str)] if isinstance(values, list) else []


def collect_macos_metadata(host_by_container: dict[str, str]) -> dict[str, dict[str, Any]]:
    """Read selected macOS attributes per path, keyed by container path.

    Returns a mapping from container path to a metadata dict for the paths that have
    any. Each value may contain "tags" (Finder tag names), "quarantined" (True when
    the file carries a download-quarantine flag), and "where_froms" (source URLs).
    Returns an empty mapping on non-macOS hosts so callers degrade gracefully.
    """
    if sys.platform != "darwin":
        return {}
    metadata: dict[str, dict[str, Any]] = {}
    for container_path, host_path in host_by_container.items():
        entry: dict[str, Any] = {}
        # Finder tags are stored as "Name" or "Name\n<color-index>"; keep the name.
        tags = [value.split("\n", 1)[0] for value in _plist_strings(_getxattr(host_path, _XATTR_TAGS))]
        if tags:
            entry["tags"] = tags
        if _getxattr(host_path, _XATTR_QUARANTINE) is not None:
            entry["quarantined"] = True
        where_froms = _plist_strings(_getxattr(host_path, _XATTR_WHERE_FROMS))
        if where_froms:
            entry["where_froms"] = where_froms
        if entry:
            metadata[container_path] = entry
    return metadata


def _dockerfile_path(name: str = PYTHON_RUNTIME.dockerfile) -> Path:
    return Path(__file__).with_name(name)


def _docker_error_detail(completed: subprocess.CompletedProcess[str]) -> str:
    detail = (completed.stderr or completed.stdout or "unknown error").strip()
    return detail[-500:]


def _run_docker(
    command: list[str],
    *,
    timeout: float,
    input: bytes | str | None = None,
    capture_output: bool = False,
    text: bool = False,
    stdout: int | None = None,
    stderr: int | None = None,
) -> subprocess.CompletedProcess[Any]:
    """Run Docker and kill its whole CLI/plugin process group on timeout."""
    captured_stdout = None
    captured_stderr = None
    if capture_output:
        if stdout is not None or stderr is not None:
            raise ValueError("stdout and stderr cannot be used with capture_output")
        # Files, rather than pipes, are deliberate. Docker Desktop plugins can
        # daemonize and retain inherited pipes after the CLI exits or is killed,
        # causing subprocess.communicate() to wait forever for EOF.
        captured_stdout = tempfile.TemporaryFile()  # noqa: SIM115 - closed below
        captured_stderr = tempfile.TemporaryFile()  # noqa: SIM115 - closed below
        stdout = captured_stdout.fileno()
        stderr = captured_stderr.fileno()

    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE if input is not None else None,
        stdout=stdout,
        stderr=stderr,
        text=text,
        start_new_session=os.name == "posix",
    )
    try:
        output, errors = process.communicate(input=input, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        if os.name == "posix":
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=2)
        if process.poll() is None:
            process.kill()
            process.wait()
        output, errors = None, None
        if captured_stdout is not None and captured_stderr is not None:
            captured_stdout.seek(0)
            captured_stderr.seek(0)
            output = captured_stdout.read()
            errors = captured_stderr.read()
            if text:
                output = output.decode(errors="replace")
                errors = errors.decode(errors="replace")
            captured_stdout.close()
            captured_stderr.close()
        raise subprocess.TimeoutExpired(
            command, timeout, output=output or exc.output, stderr=errors or exc.stderr
        ) from exc

    if captured_stdout is not None and captured_stderr is not None:
        captured_stdout.seek(0)
        captured_stderr.seek(0)
        output = captured_stdout.read()
        errors = captured_stderr.read()
        if text:
            output = output.decode(errors="replace")
            errors = errors.decode(errors="replace")
        captured_stdout.close()
        captured_stderr.close()
    return subprocess.CompletedProcess(command, process.returncode, output, errors)


def check_docker_available() -> None:
    """Fail early with an actionable error when the Docker daemon is unavailable."""
    try:
        completed = _run_docker(
            ["docker", "ps", "--quiet", "--no-trunc"],
            capture_output=True,
            text=True,
            timeout=DOCKER_CHECK_TIMEOUT,
        )
    except FileNotFoundError as exc:
        raise DockerUnavailableError(
            "Docker CLI was not found. Install Docker and ensure 'docker' is on PATH."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise DockerUnavailableError(
            "Docker daemon did not respond within 10 seconds. Start or restart Docker, then retry."
        ) from exc

    daemon_error = completed.stderr.strip()
    if completed.returncode != 0 or daemon_error:
        detail = _docker_error_detail(completed)
        raise DockerUnavailableError(
            f"Docker daemon is unavailable: {detail}. "
            "Start Docker Desktop (macOS/Windows) or the Docker daemon (Linux), then retry."
        )


def build_image(
    image: str = DEFAULT_IMAGE,
    *,
    rebuild: bool = False,
    build_timeout: float = DEFAULT_BUILD_TIMEOUT,
    dockerfile: str = PYTHON_RUNTIME.dockerfile,
) -> None:
    """Build the base worker image when absent, or unconditionally when requested."""
    if build_timeout <= 0:
        raise ValueError("build_timeout must be positive")
    check_docker_available()
    if not rebuild:
        try:
            probe = _run_docker(
                ["docker", "image", "inspect", image],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=DOCKER_CHECK_TIMEOUT,
            )
        except subprocess.TimeoutExpired as exc:
            raise DockerUnavailableError(
                "Docker timed out while inspecting the worker image. Restart Docker, then retry."
            ) from exc
        if probe.returncode == 0:
            return

    dockerfile_path = _dockerfile_path(dockerfile)
    try:
        completed = _run_docker(
            [
                "docker",
                "build",
                "--load",
                "--file",
                str(dockerfile_path),
                "--tag",
                image,
                str(dockerfile_path.parent),
            ],
            timeout=build_timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise DockerError(
            f"Docker worker image build exceeded the {build_timeout:g}s timeout. "
            "Restart Docker and retry."
        ) from exc
    if completed.returncode != 0:
        raise DockerError(
            f"Docker worker image build failed with exit status {completed.returncode}. "
            "The daemon may have stopped; verify it with 'docker info' and retry."
        )


def _image_exists(image: str) -> bool:
    try:
        probe = _run_docker(
            ["docker", "image", "inspect", image],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=DOCKER_CHECK_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise DockerUnavailableError(
            "Docker timed out while inspecting the worker image. Restart Docker, then retry."
        ) from exc
    return probe.returncode == 0


def _derived_image_tag(image: str, dependencies: Sequence[str]) -> str:
    """Stable per-dependency-set tag derived from the base image and packages."""
    repository = image.split(":", 1)[0]
    digest = hashlib.sha256("\n".join(sorted(dependencies)).encode()).hexdigest()[:12]
    return f"{repository}:deps-{digest}"


def build_worker_image(
    image: str = DEFAULT_IMAGE,
    dependencies: Sequence[str] = (),
    *,
    runtime: Runtime = PYTHON_RUNTIME,
    rebuild: bool = False,
    build_timeout: float = DEFAULT_BUILD_TIMEOUT,
) -> str:
    """Ensure a runnable worker image and return the tag to run.

    With no dependencies this is the stdlib/runtime-only base image. With
    dependencies it builds (once, then caches) a derived image that layers the
    runtime's package install (``pip``/``npm``) on top of the base, and returns
    that derived tag.
    """
    build_image(image, rebuild=rebuild, build_timeout=build_timeout, dockerfile=runtime.dockerfile)
    if not dependencies:
        return image

    derived = _derived_image_tag(image, dependencies)
    if not rebuild and _image_exists(derived):
        return derived

    packages = sorted(set(dependencies))
    dockerfile = runtime.derived_dockerfile(image, packages)
    with tempfile.TemporaryDirectory(prefix="pfind-deps-") as context:
        (Path(context) / "Dockerfile").write_text(dockerfile)
        try:
            completed = _run_docker(
                ["docker", "build", "--load", "--tag", derived, context],
                timeout=build_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise DockerError(
                f"Building the worker image with dependencies exceeded the "
                f"{build_timeout:g}s timeout. Restart Docker and retry."
            ) from exc
    if completed.returncode != 0:
        raise DockerError(
            "Failed to install requested packages into the worker image "
            f"({', '.join(packages)}); exit status {completed.returncode}."
        )
    return derived


def _whitelist_path() -> Path:
    """Location of the persisted package whitelist."""
    override = os.environ.get("PFIND_WHITELIST")
    if override:
        return Path(override)
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "pfind" / "whitelist.json"


def _read_whitelist_file() -> dict[str, Any]:
    path = _whitelist_path()
    if path.exists():
        with contextlib.suppress(json.JSONDecodeError, OSError):
            data = json.loads(path.read_text())
            if isinstance(data, dict):
                return data
    return {}


def _saved_packages(data: dict[str, Any], runtime: str) -> set[str]:
    saved = {p for p in data.get(runtime, []) if isinstance(p, str)}
    if runtime == DEFAULT_RUNTIME:
        # Absorb the pre-runtime flat format, {"packages": [...]}, as Python.
        saved |= {p for p in data.get("packages", []) if isinstance(p, str)}
    return saved


def load_whitelist(runtime: str = DEFAULT_RUNTIME) -> set[str]:
    """Return approved package names for a runtime: defaults plus saved approvals."""
    defaults = set(RUNTIMES[runtime].default_packages)
    return defaults | _saved_packages(_read_whitelist_file(), runtime)


def approve_packages(packages: Sequence[str], runtime: str = DEFAULT_RUNTIME) -> None:
    """Persist newly approved packages for a runtime to the user's whitelist file."""
    if not packages:
        return
    data = _read_whitelist_file()
    existing = _saved_packages(data, runtime)
    existing |= set(packages)
    data[runtime] = sorted(existing)
    data.pop("packages", None)  # migrate away from the legacy flat format
    path = _whitelist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def _remove_container(name: str) -> None:
    with contextlib.suppress(FileNotFoundError, subprocess.TimeoutExpired):
        _run_docker(
            ["docker", "rm", "--force", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )


def run_filter(
    code: str,
    search_root: Path,
    container_paths: list[str],
    *,
    image: str = DEFAULT_IMAGE,
    timeout: float = 10.0,
    memory: str = "256m",
    cpus: float = 1.0,
    pids_limit: int = 64,
    meta: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Execute generated code in a constrained, disposable container."""
    if timeout <= 0 or cpus <= 0 or pids_limit <= 0:
        raise ValueError("timeout, cpus, and pids_limit must be positive")

    root = search_root.expanduser().resolve(strict=True)
    name = f"pfind-search-{uuid.uuid4().hex}"
    request = json.dumps({"code": code, "paths": container_paths, "meta": meta or {}}).encode()
    command = [
        "docker",
        "run",
        "--rm",
        "--name",
        name,
        "--interactive",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        str(pids_limit),
        "--memory",
        memory,
        "--cpus",
        str(cpus),
        "--ulimit",
        "nofile=128:128",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=16m",
        "--mount",
        f"type=bind,src={root},dst=/data,readonly",
        image,
    ]

    try:
        completed = _run_docker(
            command,
            input=request,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        _remove_container(name)
        raise TimeoutError(f"Generated filter exceeded the {timeout:g}s timeout.") from exc

    if len(completed.stdout) > MAX_RESULT_BYTES or len(completed.stderr) > MAX_RESULT_BYTES:
        raise RuntimeError("Worker output exceeded the allowed size.")
    if completed.returncode != 0:
        error = completed.stderr.decode(errors="replace").strip()
        detail = error or f"exit status {completed.returncode}"
        raise RuntimeError(f"Docker worker failed: {detail}")

    try:
        response = json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Docker worker returned an invalid response.") from exc
    if not isinstance(response, dict) or response.get("ok") is not True:
        message = (
            response.get("error", "unknown worker error")
            if isinstance(response, dict)
            else "invalid response"
        )
        raise RuntimeError(f"Generated filter failed: {message}")

    try:
        return _normalize_results(response.get("results"), set(container_paths))
    except ValueError as exc:
        raise RuntimeError(f"Generated filter returned an invalid result: {exc}") from exc


def search(
    path: str,
    prompt: str,
    *,
    image: str | None = None,
    model: str = DEFAULT_MODEL,
    timeout: float = 10.0,
    memory: str = "256m",
    cpus: float = 1.0,
    pids_limit: int = 64,
    rebuild: bool = False,
    build_timeout: float = DEFAULT_BUILD_TIMEOUT,
    on_generated: Callable[[GeneratedFilter], None] | None = None,
    on_retry: Callable[[int, ValueError], None] | None = None,
    approve_dependencies: Callable[[list[str]], bool] | None = None,
    whitelist: set[str] | None = None,
    macos_meta: bool = False,
) -> list[dict[str, Any]]:
    """Generate and execute a filter, returning host-path result records.

    Each record is a dict with at least a "path" key (a host path); the filter may
    attach extra per-path fields when the prompt asks for them.

    The model chooses the runtime (Python or Node.js); the matching base image is
    used unless ``image`` overrides the base tag.

    ``on_generated``, if given, is called with the ``GeneratedFilter`` after it is
    produced but before it runs. It may inspect, save, or display the code; raising
    from it (e.g. on a declined confirmation) aborts before execution.

    If the filter requests third-party packages that are not already approved
    (``whitelist``, defaulting to the runtime's built-in plus saved whitelist),
    ``approve_dependencies`` is called with the new package names. When it returns
    True the packages are installed into a derived image and remembered; otherwise
    a ``DependencyError`` is raised. Without an approver, unapproved packages are
    rejected.

    When ``macos_meta`` is true and the host is macOS, selected per-path attributes
    (Finder tags, quarantine/where-from) are read on the host and exposed to a Python
    filter as a global ``META`` dict, enabling queries that combine macOS metadata with
    file contents. It is a no-op on other platforms.
    """
    root = Path(path).expanduser().resolve(strict=True)
    container_paths, host_by_container = enumerate_paths(root)
    if not container_paths:
        return []
    # Verify Docker up front so a missing daemon fails before any API call.
    check_docker_available()
    meta = collect_macos_metadata(host_by_container) if macos_meta else {}
    generated = generate_filter(prompt, model=model, on_retry=on_retry, macos_meta=macos_meta)
    runtime = RUNTIMES[generated.runtime]
    if on_generated is not None:
        on_generated(generated)

    approved = whitelist if whitelist is not None else load_whitelist(runtime.name)
    new_packages = [pkg for pkg in generated.dependencies if pkg not in approved]
    if new_packages:
        if approve_dependencies is None or not approve_dependencies(new_packages):
            raise DependencyError(
                "filter requires packages that were not approved: "
                + ", ".join(new_packages)
            )
        approve_packages(new_packages, runtime.name)

    run_image = build_worker_image(
        image if image is not None else runtime.base_image,
        generated.dependencies,
        runtime=runtime,
        rebuild=rebuild,
        build_timeout=build_timeout,
    )
    records = run_filter(
        generated.code,
        root,
        container_paths,
        image=run_image,
        timeout=timeout,
        memory=memory,
        cpus=cpus,
        pids_limit=pids_limit,
        meta=meta,
    )
    host_records: list[dict[str, Any]] = []
    for record in records:
        mapped = dict(record)
        mapped["path"] = host_by_container[record["path"]]
        host_records.append(mapped)
    return host_records


def _worker_response(payload: dict[str, Any]) -> dict[str, Any]:
    code = payload.get("code")
    paths = payload.get("paths")
    if (
        not isinstance(code, str)
        or not isinstance(paths, list)
        or not all(isinstance(path, str) for path in paths)
    ):
        raise ValueError("Worker request must contain code and a list of path strings.")
    meta = payload.get("meta") or {}
    if not isinstance(meta, dict):
        raise ValueError("Worker request 'meta' must be an object.")

    # META is host-collected macOS metadata (empty unless --macos-meta). The generated
    # filter may read it via META.get(path, {}); see _MACOS_META_SYSTEM.
    namespace: dict[str, Any] = {"__name__": "generated_filter", "META": meta}
    # Suppress ordinary generated-code output so stdout remains a JSON protocol.
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        exec(compile(code, "<generated-filter>", "exec"), namespace)  # noqa: S102
        function = namespace.get("filter_paths")
        if not callable(function):
            raise ValueError("Generated code did not define filter_paths.")
        results = function(paths)

    return {"ok": True, "results": _normalize_results(results, set(paths))}


def worker_main() -> int:
    """Container supervisor: keep generated-code output off the host protocol."""
    request = sys.stdin.buffer.read()
    response_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix="response-", dir="/tmp", delete=False) as file:
            response_path = file.name
        completed = subprocess.run(
            [
                sys.executable,
                "-I",
                str(Path(__file__).resolve()),
                "--execute-worker",
                response_path,
            ],
            input=request,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"filter process exited with status {completed.returncode}")
        with open(response_path, "rb") as file:
            encoded = file.read(MAX_RESULT_BYTES + 1)
        if len(encoded) > MAX_RESULT_BYTES:
            raise RuntimeError("filter response exceeded the allowed size")
        response = json.loads(encoded)
    except BaseException as exc:
        response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        if response_path is not None:
            Path(response_path).unlink(missing_ok=True)

    json.dump(response, sys.stdout, separators=(",", ":"))
    return 0


def execute_worker_main(response_path: str) -> int:
    """Child entry point that executes generated code and writes a response file."""
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("Worker request must be a JSON object.")
        response = _worker_response(payload)
    except BaseException as exc:
        response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    encoded = json.dumps(response, separators=(",", ":")).encode()
    if len(encoded) > MAX_RESULT_BYTES:
        encoded = b'{"ok":false,"error":"filter response exceeded the allowed size"}'
    Path(response_path).write_bytes(encoded)
    return 0


def _module_main() -> int:
    """In-container entry point: handle only the worker dispatch modes.

    The host-facing command line lives in ``pfind.cli``. Inside the Docker image
    the module is invoked as ``python backend.py --worker`` (which in turn
    re-invokes itself with ``--execute-worker``).
    """
    if sys.argv[1:] == ["--worker"]:
        return worker_main()
    if len(sys.argv) == 3 and sys.argv[1] == "--execute-worker":
        return execute_worker_main(sys.argv[2])
    print(
        "backend.py is the in-container worker; use the 'pfind' command on the host.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(_module_main())
