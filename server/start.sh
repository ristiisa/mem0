#!/bin/sh
# Starts the embedding server next to the API when LLAMA_MODEL_URL is set, restarting it if it exits.
set -e

if [ -n "$LLAMA_MODEL_URL" ]; then
  (
    model=/models/embedder.gguf
    until [ -f "$model" ]; do
      if curl -fsSL -o "$model.part" "$LLAMA_MODEL_URL" && echo "$LLAMA_MODEL_SHA256  $model.part" | sha256sum -c --status -; then
        mv "$model.part" "$model"
      else
        echo "embedding model download failed, retrying in 30s" >&2
        sleep 30
      fi
    done

    while true; do
      /opt/llama/llama-server -m "$model" --embeddings --pooling mean --host 127.0.0.1 --port 8080 -c 2048 || true
      echo "llama-server exited, restarting in 2s" >&2
      sleep 2
    done
  ) &
fi

alembic upgrade head
exec uvicorn main:app --host 0.0.0.0 --port 8000 --no-server-header
