"""Build a prompt embedder for the query cache's semantic matching.

The embedder reuses the same provider machinery as filter generation
(:mod:`nfind.generation`), so embeddings cross exactly the trust boundary generation
already does: an OpenAI model embeds via OpenAI, a local Ollama model embeds locally.
Only the prompt text is sent -- never file contents or paths, which the sandbox keeps
on the host regardless.

This is only needed on the opt-in semantic path (``pip install nfind[semantic]``); the
default normalized-exact cache calls nothing here.
"""

from __future__ import annotations

from .constants import DEFAULT_PROVIDER
from .generation import _make_client, _split_model

DEFAULT_EMBEDDING_MODEL = "openai/text-embedding-3-small"
"""Default embedding model when semantic matching is enabled without an explicit one."""


def build_embedder(model: str) -> tuple[str, object]:
    """Return ``(canonical_model, embed)`` where ``embed(text) -> list[float]``.

    ``model`` is a ``provider/model`` selector (a bare name uses the default provider),
    mirroring ``--model`` for generation. The returned callable issues one embeddings
    request per call through the provider's OpenAI-compatible endpoint. The canonical
    ``provider/model`` string is returned too so the cache can detect an embedding-model
    change and avoid comparing vectors across incompatible models.
    """
    provider, name = _split_model(model)
    canonical = f"{provider}/{name}" if provider != DEFAULT_PROVIDER or "/" in model else model
    client = _make_client(provider)

    def embed(text: str) -> list[float]:
        response = client.embeddings.create(model=name, input=text)
        return list(response.data[0].embedding)

    return canonical, embed
