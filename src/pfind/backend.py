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

import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from ._constants import _RETRY_TEMPERATURE as _RETRY_TEMPERATURE
from ._constants import DEFAULT_ALLOWED_PACKAGES as DEFAULT_ALLOWED_PACKAGES
from ._constants import DEFAULT_BUILD_TIMEOUT as DEFAULT_BUILD_TIMEOUT
from ._constants import DEFAULT_GENERATION_ATTEMPTS as DEFAULT_GENERATION_ATTEMPTS
from ._constants import DEFAULT_IGNORES as DEFAULT_IGNORES
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
from .sandbox import DockerSandbox as DockerSandbox
from .sandbox import Limits as Limits
from .sandbox import Mount as Mount
from .sandbox import Sandbox as Sandbox
from .sandbox import SandboxError as SandboxError
from .sandbox import SandboxOutputTooLarge as SandboxOutputTooLarge
from .sandbox import SandboxTimeout as SandboxTimeout
from .sandbox import _derived_image_tag as _derived_image_tag
from .sandbox import _dockerfile_path as _dockerfile_path
from .sandbox import _image_exists as _image_exists
from .sandbox import _remove_container as _remove_container
from .sandbox import _run_docker as _run_docker
from .sandbox import build_image as build_image
from .sandbox import check_docker_available as check_docker_available
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


def list_models(model: str = DEFAULT_MODEL) -> list[str]:
    """Return the model ids the selected provider exposes via its ``/models`` endpoint.

    The provider is taken from the ``provider/...`` prefix of ``model`` (the default
    provider for a bare name), reusing the same client and credentials as generation.
    Works for any provider implementing the OpenAI-compatible ``/models`` listing
    (including local ``ollama``/``lmstudio``, which list what is installed). Raises
    ``RuntimeError`` when the provider does not support listing or the request fails.
    """
    provider, _ = _split_model(model)
    client = _make_client(provider)
    try:
        page = client.models.list()
    except Exception as exc:  # noqa: BLE001 - SDK/provider error types vary
        raise RuntimeError(
            f"Could not list models for the {provider!r} provider: {exc}. The provider may "
            "not support listing models; check its documentation for valid model names."
        ) from exc
    ids = [model_id for item in page.data if (model_id := getattr(item, "id", None))]
    return sorted(ids)


_GENERATION_MAX_TOKENS = 4096


def _is_responses_only(exc: Exception) -> bool:
    """True when the error says the model is served only on the Responses API.

    OpenAI's reasoning/codex models (e.g. ``gpt-5.1-codex-mini``) reject
    ``/chat/completions`` with a 404 whose message points to ``v1/responses``. This is
    not a missing model -- it just needs the other endpoint -- so it is checked before
    ``_is_model_not_found`` so the request can be retried via ``client.responses``.
    """
    return "v1/responses" in str(exc).lower()


def _is_model_not_found(exc: Exception) -> bool:
    """True when an SDK/provider error indicates the model id is unknown or inaccessible."""
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    message = str(exc).lower()
    return status == 404 or "model_not_found" in message or "does not exist" in message


def _adapt_request(exc: Exception, policy: dict[str, Any]) -> bool:
    """Adjust ``policy`` to work around an unsupported request parameter; True if changed.

    Newer (reasoning) models reject some chat-completion parameters. Rather than keep a
    brittle per-model table, react to the provider's error and adapt: rename ``max_tokens``
    to ``max_completion_tokens``, drop an unsupported ``temperature``, and -- as a last
    resort, mirroring providers that lack JSON mode -- drop ``response_format``. Each fix
    is applied at most once.
    """
    message = str(exc).lower()
    if policy["max_tokens_key"] == "max_tokens" and "max_tokens" in message:
        policy["max_tokens_key"] = "max_completion_tokens"
        return True
    if "temperature" not in policy["drop"] and "temperature" in message:
        policy["drop"].add("temperature")
        return True
    if "response_format" not in policy["drop"]:
        policy["drop"].add("response_format")
        return True
    return False


def _create_response(
    client: Any,
    *,
    model: str,
    messages: list[dict[str, str]],
    temperature: float,
    policy: dict[str, Any],
) -> str:
    """Issue one request via chat-completions or the Responses API and return the text.

    The endpoint and parameter shape are chosen from ``policy``: reasoning/codex models
    that only accept ``/responses`` use ``client.responses.create`` (``input`` /
    ``max_output_tokens`` / ``text`` JSON format) instead of ``client.chat.completions``.
    """
    if policy["use_responses"]:
        kwargs: dict[str, Any] = {
            "model": model,
            "input": messages,
            "max_output_tokens": _GENERATION_MAX_TOKENS,
        }
        if "temperature" not in policy["drop"]:
            kwargs["temperature"] = temperature
        if "response_format" not in policy["drop"]:
            kwargs["text"] = {"format": {"type": "json_object"}}
        return client.responses.create(**kwargs).output_text or ""
    kwargs = {
        "model": model,
        "messages": messages,
        policy["max_tokens_key"]: _GENERATION_MAX_TOKENS,
    }
    if "temperature" not in policy["drop"]:
        kwargs["temperature"] = temperature
    if "response_format" not in policy["drop"]:
        kwargs["response_format"] = {"type": "json_object"}
    return client.chat.completions.create(**kwargs).choices[0].message.content or ""


def _request_completion(
    client: Any,
    *,
    model: str,
    provider: str,
    messages: list[dict[str, str]],
    temperature: float,
    policy: dict[str, Any],
) -> str:
    """Generate one completion, adapting endpoint and parameters reactively on errors.

    ``policy`` carries learned adaptations across calls (so a fix discovered on one attempt
    is reused on the next): a switch to the Responses API, a renamed token limit, or a
    dropped parameter. A model-not-found error is reported immediately with guidance; other
    errors trigger one adaptation and a retry until none remain.
    """
    for _ in range(5):  # initial try + the API switch + one fix per adaptable parameter
        try:
            return _create_response(
                client, model=model, messages=messages, temperature=temperature, policy=policy
            )
        except Exception as exc:  # noqa: BLE001 - SDK/provider error types vary
            if not policy["use_responses"] and _is_responses_only(exc):
                policy["use_responses"] = True
                continue
            if _is_model_not_found(exc):
                hint = "pfind --list-models"
                if provider != DEFAULT_PROVIDER:
                    hint += f" --model {provider}/<name>"
                raise RuntimeError(
                    f"Model {model!r} was not found for the {provider!r} provider -- it may be "
                    f"misspelled or not enabled for your API key. Run '{hint}' to see what's "
                    "available."
                ) from exc
            if not _adapt_request(exc, policy):
                raise RuntimeError(f"Model request to {provider!r} failed: {exc}") from exc
    raise RuntimeError(f"Model request to {provider!r} failed after adapting parameters.")


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
    # Adapt the request reactively: not every model/provider honours JSON mode, max_tokens,
    # or a custom temperature, and reasoning/codex models need the Responses API. The policy
    # persists across attempts so a fix is learned once. See _request_completion.
    policy: dict[str, Any] = {"drop": set(), "max_tokens_key": "max_tokens", "use_responses": False}
    last_error: ValueError | None = None
    for attempt in range(attempts):
        content = _request_completion(
            client,
            model=model_name,
            provider=provider,
            messages=messages,
            temperature=0 if attempt == 0 else _RETRY_TEMPERATURE,
            policy=policy,
        )
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


def _matches_any(name: str, relative_posix: str, patterns: Sequence[str]) -> bool:
    """True when ``name`` or its root-relative POSIX path matches any glob in ``patterns``."""
    return any(
        fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(relative_posix, pattern)
        for pattern in patterns
    )


def enumerate_paths(
    search_root: Path,
    *,
    exclude: Sequence[str] = (),
    max_depth: int | None = None,
    use_default_ignores: bool = True,
) -> tuple[list[str], dict[str, str]]:
    """Return container paths and a container-to-host result mapping.

    ``exclude`` is a list of glob patterns matched against each entry's name *and* its
    path relative to the search root (POSIX form); a matching directory is pruned --
    skipped from the results and not descended into. When ``use_default_ignores`` is true
    (the default), the common VCS/dependency/cache names in :data:`DEFAULT_IGNORES` are
    excluded too. ``max_depth`` bounds how deep below the root to descend -- a direct child
    is depth 1 -- and ``None`` (the default) means unlimited.
    """
    root = search_root.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"Search root is not a directory: {root}")
    if max_depth is not None and max_depth < 1:
        raise ValueError("max_depth must be at least 1")

    patterns = [*exclude]
    if use_default_ignores:
        patterns += sorted(DEFAULT_IGNORES)

    container_paths: list[str] = []
    host_by_container: dict[str, str] = {}
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        depth = len(current_path.relative_to(root).parts)

        # Prune excluded directories in place so os.walk neither lists nor descends them.
        kept: list[str] = []
        for name in directories:
            relative_posix = (current_path / name).relative_to(root).as_posix()
            if not _matches_any(name, relative_posix, patterns):
                kept.append(name)
        directories[:] = kept

        for name in [*directories, *files]:
            host_path = current_path / name
            relative = host_path.relative_to(root)
            if name in files and _matches_any(name, relative.as_posix(), patterns):
                continue
            container_path = str(PurePosixPath("/data", *relative.parts))
            container_paths.append(container_path)
            host_by_container[container_path] = str(host_path)

        # Stop descending once the next level would exceed max_depth; entries at the
        # current level (including directories) have already been recorded above.
        if max_depth is not None and depth + 1 >= max_depth:
            directories[:] = []
    return container_paths, host_by_container


def build_worker_image(
    image: str = DEFAULT_IMAGE,
    dependencies: Sequence[str] = (),
    *,
    runtime: Runtime = PYTHON_RUNTIME,
    rebuild: bool = False,
    build_timeout: float = DEFAULT_BUILD_TIMEOUT,
    sandbox: Sandbox | None = None,
) -> str:
    """Ensure a runnable worker image and return the tag to run.

    With no dependencies this is the stdlib/runtime-only base image. With
    dependencies it builds (once, then caches) a derived image that layers the
    runtime's package install (``pip``/``npm``) on top of the base, and returns
    that derived tag. The actual Docker work is delegated to ``sandbox`` (a
    :class:`~pfind.sandbox.DockerSandbox` for the runtime by default); this function
    keeps only the pfind-specific ``Runtime``/dependency logic.
    """
    if sandbox is None:
        sandbox = DockerSandbox(
            image, dockerfile=_dockerfile_path(runtime.dockerfile), build_timeout=build_timeout
        )
    sandbox.ensure_image(rebuild=rebuild)
    if not dependencies:
        return image

    packages = sorted(set(dependencies))
    dockerfile_text = runtime.derived_dockerfile(image, packages)
    try:
        return sandbox.derive_image(dockerfile_text, rebuild=rebuild)
    except SandboxError as exc:
        # The sandbox is package-agnostic; restore the actionable list of packages.
        raise DockerError(
            f"Failed to build the worker image with packages ({', '.join(packages)}): {exc}"
        ) from exc


def _parse_worker_response(stdout: bytes) -> dict[str, Any]:
    """Decode and validate the worker's JSON protocol reply (pfind-specific)."""
    try:
        response = json.loads(stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Docker worker returned an invalid response.") from exc
    if not isinstance(response, dict) or response.get("ok") is not True:
        message = (
            response.get("error", "unknown worker error")
            if isinstance(response, dict)
            else "invalid response"
        )
        raise RuntimeError(f"Generated filter failed: {message}")
    return response


def run_filter(
    code: str,
    search_root: Path,
    container_paths: list[str],
    *,
    sandbox: Sandbox | None = None,
    image: str = DEFAULT_IMAGE,
    timeout: float = 10.0,
    memory: str = "256m",
    cpus: float = 1.0,
    pids_limit: int = 64,
    meta: dict[str, Any] | None = None,
    limits: Limits | None = None,
) -> list[dict[str, Any]]:
    """Execute generated code in the sandbox and return container-path records.

    Builds the ``{code, paths, meta}`` request, hands it to ``sandbox.run`` (a
    :class:`~pfind.sandbox.DockerSandbox` for ``image`` by default), then validates the
    worker's ``{ok, results}`` reply against the supplied paths. The sandbox owns the
    hardened container and its limits; this adapter owns the worker protocol.

    Pass a :class:`~pfind.sandbox.Limits` as ``limits`` to set the resource/output caps
    directly; otherwise they are built from the ``timeout``/``memory``/``cpus``/
    ``pids_limit`` arguments (with the host's :data:`MAX_RESULT_BYTES` output cap).
    """
    if limits is None:
        limits = Limits(
            memory=memory,
            cpus=cpus,
            pids=pids_limit,
            timeout=timeout,
            max_output_bytes=MAX_RESULT_BYTES,
        )
    if limits.timeout <= 0 or limits.cpus <= 0 or limits.pids <= 0:
        raise ValueError("timeout, cpus, and pids must be positive")

    root = search_root.expanduser().resolve(strict=True)
    if sandbox is None:
        sandbox = DockerSandbox(image, dockerfile=_dockerfile_path())
    request = json.dumps({"code": code, "paths": container_paths, "meta": meta or {}}).encode()

    try:
        run = sandbox.run(request, mounts=[Mount(root, "/data", read_only=True)], limits=limits)
    except SandboxTimeout as exc:
        raise TimeoutError(f"Generated filter exceeded the {limits.timeout:g}s timeout.") from exc
    except SandboxOutputTooLarge as exc:
        raise RuntimeError("Worker output exceeded the allowed size.") from exc

    if run.returncode != 0:
        error = run.stderr.decode(errors="replace").strip()
        detail = error or f"exit status {run.returncode}"
        raise RuntimeError(f"Docker worker failed: {detail}")

    response = _parse_worker_response(run.stdout)
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
    sandbox: Sandbox | None = None,
    exclude: Sequence[str] = (),
    max_depth: int | None = None,
    use_default_ignores: bool = True,
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

    ``sandbox`` overrides the execution backend (a :class:`~pfind.sandbox.DockerSandbox`
    built from the chosen runtime by default); pass a fake to run without Docker.

    ``exclude`` (glob patterns), ``use_default_ignores`` (skip common VCS/dependency/cache
    directories), and ``max_depth`` (limit traversal depth) shape which paths are
    enumerated and handed to the filter; see :func:`enumerate_paths`.
    """
    root = Path(path).expanduser().resolve(strict=True)
    container_paths, host_by_container = enumerate_paths(
        root, exclude=exclude, max_depth=max_depth, use_default_ignores=use_default_ignores
    )
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
        sandbox=sandbox,
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
    sandbox: Sandbox | None = None,
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
        sandbox=sandbox,
    )
    records = run_filter(
        generated.code,
        root,
        container_paths,
        sandbox=sandbox,
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
    sandbox: Sandbox | None = None,
    exclude: Sequence[str] = (),
    max_depth: int | None = None,
    use_default_ignores: bool = True,
) -> list[dict[str, Any]]:
    """Replay a previously saved filter through the sandbox, skipping the LLM.

    The file written by ``--save``/:func:`render_saved_filter` is parsed back into a
    filter and run in the same hardened container as :func:`search`. Any third-party
    packages it declares are still gated through ``approve_dependencies``/the whitelist,
    so a saved filter cannot silently pull new packages. macOS metadata is not exposed
    on the replay path. ``exclude``/``use_default_ignores``/``max_depth`` shape
    enumeration exactly as for :func:`search`.
    """
    saved = Path(filter_path).expanduser()
    generated = parse_saved_filter(saved.read_text(), filename=saved.name)
    if on_generated is not None:
        on_generated(generated)

    root = Path(path).expanduser().resolve(strict=True)
    container_paths, host_by_container = enumerate_paths(
        root, exclude=exclude, max_depth=max_depth, use_default_ignores=use_default_ignores
    )
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
        sandbox=sandbox,
    )
