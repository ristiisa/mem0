from typing import Optional

from pydantic import BaseModel, Field, field_validator


class EmbedderConfig(BaseModel):
    provider: str = Field(
        description="Provider of the embedding model (e.g., 'ollama', 'openai')",
        default="openai",
    )
    config: Optional[dict] = Field(description="Configuration for the specific embedding model", default={})

    @field_validator("config")
    def validate_config(cls, v, values):
        provider = values.data.get("provider")
        if provider in [
            "openai",
            "ollama",
            "huggingface",
            "azure_openai",
            "gemini",
            "vertexai",
            "together",
            "lmstudio",
            "langchain",
            "aws_bedrock",
            "fastembed",
        ]:
            return v
        # Accept providers registered at runtime (e.g. by the server) in the factory.
        from mem0.utils.factory import EmbedderFactory

        if provider in EmbedderFactory.provider_to_class:
            return v
        raise ValueError(f"Unsupported embedding provider: {provider}")
