#!/usr/bin/env python3
"""Search paths with an LLM-generated Python filter executed inside Docker.

The host enumerates the search tree and asks the model for code.  The generated
code runs in a disposable container with the search root mounted at /data as
read-only.  Only paths supplied by the host may be returned.

The in-container worker that runs the generated code lives in :mod:`pfind.worker`,
a self-contained, standard-library-only module the Docker image ships and runs as
``python worker.py --worker``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from ._constants import _RETRY_TEMPERATURE as _RETRY_TEMPERATURE
from ._constants import DEFAULT_ALLOWED_PACKAGES as DEFAULT_ALLOWED_PACKAGES
from ._constants import DEFAULT_BUILD_TIMEOUT as DEFAULT_BUILD_TIMEOUT
from ._constants import DEFAULT_GENERATION_ATTEMPTS as DEFAULT_GENERATION_ATTEMPTS
from ._constants import DEFAULT_IMAGE as DEFAULT_IMAGE
from ._constants import DEFAULT_MODEL as DEFAULT_MODEL
from ._constants import DEFAULT_NODE_IMAGE as DEFAULT_NODE_IMAGE
from ._constants import DEFAULT_PROVIDER as DEFAULT_PROVIDER
from ._constants import DEFAULT_RUNTIME as DEFAULT_RUNTIME
from ._constants import DOCKER_CHECK_TIMEOUT as DOCKER_CHECK_TIMEOUT
from ._constants import FILTER_LINE_LENGTH as FILTER_LINE_LENGTH
from ._constants import PROVIDERS as PROVIDERS
from .errors import DependencyError as DependencyError
from .errors import DockerError as DockerError
from .errors import DockerUnavailableError as DockerUnavailableError
from .metadata import collect_macos_metadata as collect_macos_metadata
from .runtimes import NODE_RUNTIME as NODE_RUNTIME
from .runtimes import PYTHON_RUNTIME as PYTHON_RUNTIME
from .runtimes import RUNTIMES as RUNTIMES
from .runtimes import GeneratedFilter as GeneratedFilter
from .runtimes import Runtime as Runtime
from .runtimes import _imply_packages as _imply_packages
from .runtimes import _validate_code_shape as _validate_code_shape
from .runtimes import _validate_dependencies as _validate_dependencies
from .saved import _PEP723_RE as _PEP723_RE
from .saved import parse_saved_filter as parse_saved_filter
from .saved import render_saved_filter as render_saved_filter
from .whitelist import _whitelist_path as _whitelist_path
from .whitelist import approve_packages as approve_packages
from .whitelist import load_whitelist as load_whitelist
from .worker import MAX_RESULT_BYTES as MAX_RESULT_BYTES
from .worker import _module_main as _module_main
from .worker import _normalize_results as _normalize_results
from .worker import _worker_response as _worker_response
from .worker import execute_worker_main as execute_worker_main
from .worker import worker_main as worker_main

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

To parse source code structure (functions, imports, classes) in the python runtime,
use tree-sitter with the per-language grammar wheel (named tree-sitter-<lang>, e.g.
tree-sitter-go). The installed tree-sitter is modern (>= 0.22); use EXACTLY this API
and nothing older (keep all imports inside filter_paths, per the rule above):

    def filter_paths(paths):
        import tree_sitter_go
        from tree_sitter import Language, Parser
        parser = Parser(Language(tree_sitter_go.language()))
        tree = parser.parse(open(paths[0], "rb").read())   # parse() takes bytes
        ...

Do NOT call Parser().set_language(...), Language(path, name), or
Language.build_library(...) -- all removed. List BOTH "tree-sitter" and the
"tree-sitter-<lang>" wheel in "dependencies". Do not use tree-sitter-language-pack
(it downloads grammars at runtime, which the no-network sandbox forbids).

Most wheels expose a single `language()`. The exception is tree_sitter_typescript:
use `tree_sitter_typescript.language_typescript()` or `.language_tsx()` (it has no
plain `language()`).

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


def _strip_code_fence(code: str) -> str:
    code = code.strip()
    if not code.startswith("```"):
        return code
    lines = code.splitlines()
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines[1:]).strip()


def _ruff_path() -> str | None:
    """Locate the ruff executable, preferring the one in pfind's own environment."""
    binary = "ruff.exe" if os.name == "nt" else "ruff"
    local = Path(sys.executable).resolve().parent / binary
    if local.exists():
        return str(local)
    return shutil.which("ruff")


def _format_generated_code(code: str, runtime: str) -> str:
    """Tidy LLM-generated code before it is shown, saved, or run.

    Runs ruff over the source: a narrow fix pass (remove unused imports ``F401`` and
    sort imports ``I``) followed by ``ruff format``. Both transforms preserve behaviour,
    so the cleaned code is safe to run unchanged in the sandbox. Only the Python runtime
    is handled (ruff is a Python tool); Node code is returned as-is. Any problem -- ruff
    missing, a non-zero exit, or a result that no longer satisfies the filter contract --
    falls back to the original code. ``--isolated`` keeps any ruff config in the user's
    working directory from influencing the result, and ``--line-length`` is pinned so the
    output does not depend on ruff's evolving default.
    """
    if runtime != DEFAULT_RUNTIME:
        return code
    ruff = _ruff_path()
    if ruff is None:
        return code
    tail = [
        "--isolated",
        "--line-length",
        str(FILTER_LINE_LENGTH),
        "--stdin-filename",
        "filter_paths.py",
        "-",
    ]
    try:
        fixed = subprocess.run(
            [ruff, "check", "--quiet", "--fix-only", "--select", "F401,I", *tail],
            input=code,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        staged = fixed.stdout if fixed.returncode == 0 and fixed.stdout else code
        formatted = subprocess.run(
            [ruff, "format", "--quiet", *tail],
            input=staged,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        cleaned = formatted.stdout if formatted.returncode == 0 and formatted.stdout else staged
    except (OSError, subprocess.SubprocessError):
        return code
    if cleaned == code:
        return code
    try:
        _validate_code_shape(cleaned)
    except ValueError:
        return code
    return cleaned


def _split_model(model: str) -> tuple[str, str]:
    """Split a "provider/model" selector into (provider, model_name).

    A bare name (no slash) uses the default provider, preserving existing behaviour.
    Only the first slash separates the provider, so vendor-qualified names pass through
    (e.g. "openrouter/anthropic/claude-3-5-sonnet").
    """
    if "/" in model:
        provider, _, name = model.partition("/")
        provider, name = provider.strip(), name.strip()
        if provider and name:
            return provider, name
    return DEFAULT_PROVIDER, model.strip()


def _make_client(provider: str) -> Any:
    """Build an OpenAI-SDK client pointed at the given provider's compatible endpoint."""
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("The host requires the 'openai' package to generate a filter.") from exc

    if provider not in PROVIDERS:
        known = ", ".join(sorted(PROVIDERS))
        raise ValueError(f"Unknown model provider {provider!r}. Known providers: {known}.")
    base_url, key_env = PROVIDERS[provider]
    if key_env is None:
        # Local server (ollama/lmstudio): the SDK still requires a non-empty key string.
        return OpenAI(base_url=base_url, api_key="local")
    api_key = os.environ.get(key_env)
    if not api_key:
        raise RuntimeError(f"Set {key_env} to use the {provider!r} provider.")
    return OpenAI(base_url=base_url, api_key=api_key)


# A ```json ... ``` or ``` ... ``` fenced block, used to recover JSON from providers
# that ignore response_format and wrap the object in markdown.
_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _extract_json_object(content: str) -> str:
    """Best-effort recovery of a JSON object from a possibly chatty/fenced reply.

    Providers without strict JSON mode may wrap the object in a code fence or add prose.
    Returns the original content when it already parses or no object can be located.
    """
    text = content.strip()
    try:
        json.loads(text)
        return text
    except json.JSONDecodeError:
        pass
    fence = _JSON_FENCE.search(text)
    if fence:
        return fence.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        return text[start : end + 1]
    return content


def _parse_generation(content: str) -> GeneratedFilter:
    """Parse and validate the model's JSON response into a GeneratedFilter."""
    try:
        payload = json.loads(_extract_json_object(content))
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

    ``model`` may be a bare name (default provider) or ``provider/model`` to target any
    OpenAI-compatible provider in ``PROVIDERS`` (e.g. ``anthropic/...``, ``ollama/...``,
    ``openrouter/vendor/...``). The matching base URL and API-key env var are used.

    When the model's reply fails validation (malformed JSON, wrong function shape,
    an invalid package name, ...), the error is fed back to the model and the
    request retried up to ``attempts`` times in total. The first attempt runs at
    temperature 0; retries nudge the temperature up so the model diverges from the
    reply that just failed. ``on_retry``, if given, is called with the 1-based
    retry number and the validation error before each retry.
    """
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    provider, model_name = _split_model(model)
    client = _make_client(provider)
    system = _SYSTEM + "\n" + _MACOS_META_SYSTEM if macos_meta else _SYSTEM
    messages: list[dict[str, str]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": _USER_TEMPLATE.format(prompt=prompt)},
    ]
    # Not every OpenAI-compatible endpoint honours JSON mode; drop it on the first
    # rejection and rely on _extract_json_object plus the retry loop instead.
    use_json_mode = True
    last_error: ValueError | None = None
    for attempt in range(attempts):
        kwargs: dict[str, Any] = {
            "model": model_name,
            "messages": messages,
            "max_tokens": 4096,
            "temperature": 0 if attempt == 0 else _RETRY_TEMPERATURE,
        }
        if use_json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            response = client.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 - SDK/provider error types vary
            if not use_json_mode:
                raise RuntimeError(f"Model request to {provider!r} failed: {exc}") from exc
            # The provider may reject response_format; drop it and try once more.
            use_json_mode = False
            kwargs.pop("response_format", None)
            try:
                response = client.chat.completions.create(**kwargs)
            except Exception as exc2:  # noqa: BLE001 - SDK/provider error types vary
                raise RuntimeError(f"Model request to {provider!r} failed: {exc2}") from exc2
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
    format_code: bool = True,
) -> list[dict[str, Any]]:
    """Generate and execute a filter, returning host-path result records.

    Each record is a dict with at least a "path" key (a host path); the filter may
    attach extra per-path fields when the prompt asks for them.

    The model chooses the runtime (Python or Node.js); the matching base image is
    used unless ``image`` overrides the base tag.

    When ``format_code`` is true (the default), the generated Python filter is tidied
    with ruff -- unused imports removed, imports sorted, and the source reformatted --
    before it is shown, saved, or run. The transforms preserve behaviour and fall back
    to the original code on any failure.

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
    generated.dependencies = _imply_packages(generated.runtime, generated.dependencies)
    if format_code:
        generated.code = _format_generated_code(generated.code, generated.runtime)
    if on_generated is not None:
        on_generated(generated)

    return _run_generated(
        generated,
        root,
        container_paths,
        host_by_container,
        meta=meta,
        image=image,
        timeout=timeout,
        memory=memory,
        cpus=cpus,
        pids_limit=pids_limit,
        rebuild=rebuild,
        build_timeout=build_timeout,
        approve_dependencies=approve_dependencies,
        whitelist=whitelist,
    )


def _run_generated(
    generated: GeneratedFilter,
    root: Path,
    container_paths: list[str],
    host_by_container: dict[str, str],
    *,
    meta: dict[str, dict[str, Any]],
    image: str | None,
    timeout: float,
    memory: str,
    cpus: float,
    pids_limit: int,
    rebuild: bool,
    build_timeout: float,
    approve_dependencies: Callable[[list[str]], bool] | None,
    whitelist: set[str] | None,
) -> list[dict[str, Any]]:
    """Build the sandbox image for a filter and run it, returning host-path records.

    Shared by ``search`` (freshly generated filters) and ``run_saved`` (filters
    replayed from a saved file). Unapproved third-party packages are gated through
    ``approve_dependencies``/the whitelist exactly as for a fresh generation.
    """
    runtime = RUNTIMES[generated.runtime]
    approved = whitelist if whitelist is not None else load_whitelist(runtime.name)
    new_packages = [pkg for pkg in generated.dependencies if pkg not in approved]
    if new_packages:
        if approve_dependencies is None or not approve_dependencies(new_packages):
            raise DependencyError(
                "filter requires packages that were not approved: " + ", ".join(new_packages)
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


def run_saved(
    filter_path: str | Path,
    path: str | Path = ".",
    *,
    image: str | None = None,
    timeout: float = 10.0,
    memory: str = "256m",
    cpus: float = 1.0,
    pids_limit: int = 64,
    rebuild: bool = False,
    build_timeout: float = DEFAULT_BUILD_TIMEOUT,
    approve_dependencies: Callable[[list[str]], bool] | None = None,
    whitelist: set[str] | None = None,
    on_generated: Callable[[GeneratedFilter], None] | None = None,
) -> list[dict[str, Any]]:
    """Replay a previously saved filter through the sandbox, skipping the LLM.

    The file written by ``--save``/:func:`render_saved_filter` is parsed back into a
    filter and run in the same hardened container as :func:`search`. Any third-party
    packages it declares are still gated through ``approve_dependencies``/the whitelist,
    so a saved filter cannot silently pull new packages. macOS metadata is not exposed
    on the replay path.
    """
    saved = Path(filter_path).expanduser()
    generated = parse_saved_filter(saved.read_text(), filename=saved.name)
    if on_generated is not None:
        on_generated(generated)

    root = Path(path).expanduser().resolve(strict=True)
    container_paths, host_by_container = enumerate_paths(root)
    if not container_paths:
        return []
    check_docker_available()
    return _run_generated(
        generated,
        root,
        container_paths,
        host_by_container,
        meta={},
        image=image,
        timeout=timeout,
        memory=memory,
        cpus=cpus,
        pids_limit=pids_limit,
        rebuild=rebuild,
        build_timeout=build_timeout,
        approve_dependencies=approve_dependencies,
        whitelist=whitelist,
    )
