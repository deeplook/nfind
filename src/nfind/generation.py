"""Generate and validate LLM-written filters.

This module owns the model-facing half of nfind: provider selection, request
adaptation, prompt text, response parsing, retry feedback, and generated Python source
cleanup. It does not know how paths are enumerated or how filters run in the sandbox.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .constants import (
    _RETRY_TEMPERATURE,
    DEFAULT_GENERATION_ATTEMPTS,
    DEFAULT_MODEL,
    DEFAULT_PROVIDER,
    DEFAULT_RUNTIME,
    FILTER_LINE_LENGTH,
    PROVIDERS,
)
from .endpoint_cache import get_endpoint, set_endpoint
from .runtimes import RUNTIMES, GeneratedFilter, _validate_code_shape

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

`paths` contains directory entries as well as files. When a filter inspects file
contents (reads bytes, parses tags, opens the path), it MUST skip entries that are
not regular files -- guard with `os.path.isfile(p)` (or filter by extension) before
opening. A single uncaught exception from `filter_paths` aborts the whole run, so make
per-path work defensive: opening a directory like a file (e.g. mutagen on a folder)
raises and discards every other match. When in doubt, wrap per-path work in
try/except and skip entries that fail.

For "python": name the function `filter_paths`. Put EVERY import at the module top
level, before the function -- never inside `filter_paths`. For example, write
`import os` and `from pathlib import Path` on their own lines first, then
`def filter_paths(paths):`. "code" must contain only those top-level imports and the
single function definition (no markdown, no decorators, no other top-level statements).
"dependencies" lists any third-party PyPI packages it imports (pip names), e.g.
["mutagen"] to read audio tags; use [] when the standard library suffices.

To parse source code structure (functions, imports, classes) in the python runtime,
use tree-sitter with the per-language grammar wheel (named tree-sitter-<lang>, e.g.
tree-sitter-go). The installed tree-sitter is modern (>= 0.22); use EXACTLY this API
and nothing older (imports stay at the top level, per the rule above):

    import tree_sitter_go
    from tree_sitter import Language, Parser

    def filter_paths(paths):
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
    """Locate the ruff executable, preferring the one in nfind's own environment."""
    binary = "ruff.exe" if os.name == "nt" else "ruff"
    local = Path(sys.executable).resolve().parent / binary
    if local.exists():
        return str(local)
    return shutil.which("ruff")


def _format_generated_code(code: str, runtime: str) -> str:
    """Tidy LLM-generated code before it is shown, saved, or run."""
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


def _check_undefined_names(code: str, *, extra_builtins: Sequence[str] = ()) -> None:
    """Reject Python filters that reference names they never import or define."""
    ruff = _ruff_path()
    if ruff is None:
        return
    cmd = [
        ruff,
        "check",
        "--quiet",
        "--isolated",
        "--select",
        "F821",
        "--output-format",
        "json",
        "--stdin-filename",
        "filter_paths.py",
    ]
    if extra_builtins:
        names = ", ".join(f"'{name}'" for name in extra_builtins)
        cmd += ["--config", f"builtins=[{names}]"]
    cmd.append("-")
    try:
        result = subprocess.run(
            cmd, input=code, capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return
    if result.returncode == 0:
        return
    try:
        findings = json.loads(result.stdout)
    except json.JSONDecodeError:
        return
    matches = (_UNDEFINED_NAME_RE.match(f["message"]) for f in findings)
    undefined = sorted({m.group(1) for m in matches if m})
    if not undefined:
        return
    names = ", ".join(repr(name) for name in undefined)
    raise ValueError(
        f"filter_paths references undefined name(s) {names}. Every name a filter uses must "
        "be imported at the module top level (before the function) or defined in the code."
    )


_UNDEFINED_NAME_RE = re.compile(r"Undefined name `([^`]+)`")


def _split_model(model: str) -> tuple[str, str]:
    """Split a "provider/model" selector into (provider, model_name)."""
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
        return OpenAI(base_url=base_url, api_key="local")
    api_key = os.environ.get(key_env)
    if not api_key:
        raise RuntimeError(f"Set {key_env} to use the {provider!r} provider.")
    return OpenAI(base_url=base_url, api_key=api_key)


_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _extract_json_object(content: str) -> str:
    """Best-effort recovery of a JSON object from a possibly chatty/fenced reply."""
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


def _parse_generation(content: str, *, macos_meta: bool = False) -> GeneratedFilter:
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
    if runtime_name == DEFAULT_RUNTIME:
        _check_undefined_names(code, extra_builtins=("META",) if macos_meta else ())
    dependencies = runtime.validate_packages(payload.get("dependencies", []))
    return GeneratedFilter(code=code, dependencies=dependencies, runtime=runtime_name)


def list_models(model: str = DEFAULT_MODEL) -> list[str]:
    """Return the model ids the selected provider exposes via its ``/models`` endpoint."""
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
    """True when the error says the model is served only on the Responses API."""
    return "v1/responses" in str(exc).lower()


def _is_model_not_found(exc: Exception) -> bool:
    """True when an SDK/provider error indicates the model id is unknown or inaccessible."""
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    message = str(exc).lower()
    return status == 404 or "model_not_found" in message or "does not exist" in message


def _adapt_request(exc: Exception, policy: dict[str, Any]) -> bool:
    """Adjust ``policy`` to work around an unsupported request parameter; True if changed."""
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
    """Issue one request via chat-completions or the Responses API and return the text."""
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
    on_responses_switch: Callable[[], None] | None = None,
) -> str:
    """Generate one completion, adapting endpoint and parameters reactively on errors."""
    for _ in range(5):
        try:
            return _create_response(
                client, model=model, messages=messages, temperature=temperature, policy=policy
            )
        except Exception as exc:  # noqa: BLE001 - SDK/provider error types vary
            if not policy["use_responses"] and _is_responses_only(exc):
                policy["use_responses"] = True
                if on_responses_switch is not None:
                    on_responses_switch()
                continue
            if _is_model_not_found(exc):
                hint = "nfind --list-models"
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
    """Generate a filter on the host, where the API credentials remain."""
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    provider, model_name = _split_model(model)
    client = _make_client(provider)
    system = _SYSTEM + "\n" + _MACOS_META_SYSTEM if macos_meta else _SYSTEM
    messages: list[dict[str, str]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": _USER_TEMPLATE.format(prompt=prompt)},
    ]
    use_responses = get_endpoint(model) == "responses"
    policy: dict[str, Any] = {
        "drop": set(),
        "max_tokens_key": "max_tokens",
        "use_responses": use_responses,
    }
    last_error: ValueError | None = None
    for attempt in range(attempts):
        content = _request_completion(
            client,
            model=model_name,
            provider=provider,
            messages=messages,
            temperature=0 if attempt == 0 else _RETRY_TEMPERATURE,
            policy=policy,
            on_responses_switch=lambda: set_endpoint(model, "responses"),
        )
        try:
            return _parse_generation(content, macos_meta=macos_meta)
        except ValueError as exc:
            last_error = exc
            if attempt + 1 < attempts:
                if on_retry is not None:
                    on_retry(attempt + 1, exc)
                messages.append({"role": "assistant", "content": content})
                messages.append({"role": "user", "content": _RETRY_TEMPLATE.format(error=exc)})

    assert last_error is not None
    raise ValueError(
        f"Model did not return a valid filter after {attempts} attempt(s): {last_error}"
    ) from last_error
