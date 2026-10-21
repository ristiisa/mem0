"""Ollama embedder that applies embeddinggemma task prefixes.

embeddinggemma expects a task prefix on every input, and quality drops sharply
without it. The stock ``mem0.embeddings.ollama.OllamaEmbedding`` receives the
``memory_action`` ("add" / "update" / "search") the core passes on every call
but ignores it, so documents and queries go in unprefixed. This subclass adds
the prefixes and is otherwise identical.

Google's recommended prefixes for embeddinggemma:
  - documents (add / update): ``title: none | text: <t>``
  - queries   (search):       ``task: search result | query: <t>``

Both are overridable via env so the same wrapper works for other prefix-style
embedders. Import triggers ``register()`` so the provider name is available to
mem0's EmbedderFactory; ``register()`` is also safe to call explicitly.
"""

from __future__ import annotations

import os
from typing import Literal, Optional

from mem0.embeddings.ollama import OllamaEmbedding

PROVIDER_NAME = "ollama_prefixed"

DEFAULT_DOC_PREFIX = "title: none | text: "
DEFAULT_QUERY_PREFIX = "task: search result | query: "


def _doc_prefix() -> str:
    return os.environ.get("MEM0_EMBED_DOC_PREFIX", DEFAULT_DOC_PREFIX)


def _query_prefix() -> str:
    return os.environ.get("MEM0_EMBED_QUERY_PREFIX", DEFAULT_QUERY_PREFIX)


class OllamaPrefixedEmbedding(OllamaEmbedding):
    def __init__(self, config=None):
        super().__init__(config)
        # embeddinggemma-2:570m is 768-dim; keep the base default otherwise.
        if self.config.embedding_dims is None:
            self.config.embedding_dims = 768

    def _prefix_for(self, memory_action: Optional[str]) -> str:
        return _query_prefix() if memory_action == "search" else _doc_prefix()

    def embed(self, text, memory_action: Optional[Literal["add", "search", "update"]] = None):
        prefix = self._prefix_for(memory_action)
        if isinstance(text, str):
            text = prefix + text
        elif isinstance(text, list):
            text = [prefix + t if isinstance(t, str) else t for t in text]
        return super().embed(text, memory_action)

    def embed_batch(self, texts, memory_action="add"):
        prefix = self._prefix_for(memory_action)
        prefixed = [prefix + t if isinstance(t, str) else t for t in texts]
        return super().embed_batch(prefixed, memory_action)


def register() -> None:
    """Make PROVIDER_NAME resolvable by mem0's EmbedderFactory (idempotent)."""
    from mem0.utils.factory import EmbedderFactory

    EmbedderFactory.provider_to_class.setdefault(
        PROVIDER_NAME, f"{__name__}.OllamaPrefixedEmbedding"
    )


register()
