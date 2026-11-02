FROM ghcr.io/ggml-org/llama.cpp:server-b12033 AS llama

FROM python:3.12

WORKDIR /app

# Install Poetry
RUN curl -sSL https://install.python-poetry.org | python3 -
ENV PATH="/root/.local/bin:$PATH"

# Copy requirements first for better caching
COPY server/requirements.txt .
RUN pip install -r requirements.txt

# Install mem0 in editable mode using Poetry
WORKDIR /app/packages
COPY pyproject.toml .
COPY poetry.lock .
COPY README.md .
COPY mem0 ./mem0
RUN pip install -e .[graph]

# llama.cpp embedding server, started by start.sh when LLAMA_MODEL_URL is set
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*
COPY --from=llama /app/ /opt/llama/
ENV LD_LIBRARY_PATH=/opt/llama

# Return to app directory and copy server code
WORKDIR /app
COPY server .

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
