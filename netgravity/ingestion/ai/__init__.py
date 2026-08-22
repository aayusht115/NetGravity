"""LLM integration. Every model call in the pipeline routes through client.py."""

from netgravity.ingestion.ai.client import LLMClient, LLMResponse, get_client

__all__ = ["LLMClient", "LLMResponse", "get_client"]
