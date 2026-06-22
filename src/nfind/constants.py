"""Configuration constants and defaults shared across nfind's modules."""

from __future__ import annotations

DEFAULT_IMAGE = "nfind-search-paths:latest"
DEFAULT_NODE_IMAGE = "nfind-search-node:latest"
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_RUNTIME = "python"
DEFAULT_PROVIDER = "openai"

# Providers reachable through the OpenAI-compatible chat-completions API. A model is
# selected as "provider/model" (e.g. "anthropic/claude-3-5-sonnet-latest"); a bare name
# means the default provider. Each entry is (base_url, api-key env var); a None base_url
# uses the OpenAI SDK default, and a None env var marks a local server needing no key.
# OpenRouter is a near-universal escape hatch: "openrouter/<vendor>/<model>".
PROVIDERS: dict[str, tuple[str | None, str | None]] = {
    "openai": (None, "OPENAI_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "mistral": ("https://api.mistral.ai/v1", "MISTRAL_API_KEY"),
    "deepseek": ("https://api.deepseek.com", "DEEPSEEK_API_KEY"),
    "xai": ("https://api.x.ai/v1", "XAI_API_KEY"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/", "GEMINI_API_KEY"),
    "anthropic": ("https://api.anthropic.com/v1/", "ANTHROPIC_API_KEY"),
    "ollama": ("http://localhost:11434/v1", None),
    "lmstudio": ("http://localhost:1234/v1", None),
}

# Directory/file names skipped during enumeration unless --no-ignore is given. These are
# VCS metadata, dependency trees, and tool caches that are almost never search targets and
# would otherwise bloat the path list the filter receives (and slow the search).
DEFAULT_IGNORES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".DS_Store",
    }
)

# How many times to ask the model in total when its reply fails validation. The
# first attempt runs at temperature 0; retries feed the error back and nudge the
# temperature up so the model diverges from the response that just failed.
DEFAULT_GENERATION_ATTEMPTS = 3
_RETRY_TEMPERATURE = 0.3
DOCKER_CHECK_TIMEOUT = 10.0
DEFAULT_BUILD_TIMEOUT = 120.0
# Line length ruff wraps generated filters to (matches nfind's own style; pinned so the
# output is stable regardless of ruff's default).
FILTER_LINE_LENGTH = 100

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
        # Multi-language syntactic parsing: tree-sitter core plus per-language grammar
        # wheels. Each wheel bundles its compiled grammar, so parsing works offline in
        # the no-network, read-only sandbox (unlike tree-sitter-language-pack, which
        # downloads grammars at runtime). Filters use the standard API:
        # Parser(Language(tree_sitter_python.language())).
        "tree-sitter",
        "tree-sitter-bash",
        "tree-sitter-c",
        "tree-sitter-dart",
        "tree-sitter-go",
        "tree-sitter-java",
        "tree-sitter-javascript",
        "tree-sitter-kotlin",
        "tree-sitter-python",
        "tree-sitter-rust",
        "tree-sitter-swift",
        "tree-sitter-typescript",
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
