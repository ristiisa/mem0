"""Embedder backed by a llama.cpp ``llama-server`` running next to the API.

The server image starts ``llama-server --embeddings`` on localhost (see
``start.sh``) with the same embeddinggemma GGUF Ollama used, so stored vectors
stay valid. Inputs get the same task prefixes as ``ollama_prefixed``.

The model is downloaded on the first start, so calls wait for the server to
come up instead of failing the add or search that happens to run first.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Literal, Optional

from mem0.embeddings.base import EmbeddingBase
from ollama_prefixed_embedder import _doc_prefix, _query_prefix

PROVIDER_NAME = "llama_prefixed"

DEFAULT_BASE_URL = "http://127.0.0.1:8080/v1"
STARTUP_WAIT_SECONDS = float(os.environ.get("MEM0_EMBEDDER_STARTUP_WAIT", "180"))


class LlamaPrefixedEmbedding(EmbeddingBase):
    def __init__(self, config=None):
        super().__init__(config)
        if self.config.embedding_dims is None:
            self.config.embedding_dims = 768
        self.url = (self.config.openai_base_url or DEFAULT_BASE_URL).rstrip("/") + "/embeddings"

    def _post(self, texts):
        body = json.dumps({"input": texts}).encode()
        deadline = time.monotonic() + STARTUP_WAIT_SECONDS
        while True:
            request = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    data = json.load(response)["data"]
                return [item["embedding"] for item in sorted(data, key=lambda item: item["index"])]
            except urllib.error.HTTPError as exc:
                # 503 while the model is still loading
                if exc.code != 503 or time.monotonic() > deadline:
                    raise
            except urllib.error.URLError:
                if time.monotonic() > deadline:
                    raise
            time.sleep(2)

    def _prefix_for(self, memory_action: Optional[str]) -> str:
        return _query_prefix() if memory_action == "search" else _doc_prefix()

    def embed(self, text, memory_action: Optional[Literal["add", "search", "update"]] = None):
        return self._post([self._prefix_for(memory_action) + text])[0]

    def embed_batch(self, texts, memory_action="add"):
        prefix = self._prefix_for(memory_action)
        return self._post([prefix + text for text in texts])


def register() -> None:
    """Make PROVIDER_NAME resolvable by mem0's EmbedderFactory (idempotent)."""
    from mem0.utils.factory import EmbedderFactory

    EmbedderFactory.provider_to_class.setdefault(PROVIDER_NAME, f"{__name__}.LlamaPrefixedEmbedding")


register()
