"""LLM client factories.

Local (Ollama) is used whenever the document is sensitive; external (Groq)
only for non-sensitive content. Construction is lazy so importing this module
never requires a running Ollama server or a Groq API key.
"""
import json
import re

from langchain_groq import ChatGroq
from langchain_ollama import ChatOllama

from src.config.settings import settings


def get_local_client():
    return ChatOllama(
        model=settings.OLLAMA_MODEL,
        base_url=settings.OLLAMA_BASE_URL,
        temperature=0,
    )


def get_external_client():
    if not settings.GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY is not set; external model unavailable")
    return ChatGroq(
        model=settings.GROQ_MODEL,
        api_key=settings.GROQ_API_KEY,
        temperature=0,
    )


def get_client_for_sensitivity(sensitivity_flag):
    """Returns (client, "local"|"external"). Treats None as sensitive (fail-safe)."""
    if sensitivity_flag is False:
        return get_external_client(), "external"
    return get_local_client(), "local"


def parse_json_object(content) -> dict:
    """Extract a JSON object from a chat-model response (tolerates code fences,
    surrounding prose, and content-block lists). Raises ValueError otherwise."""
    if isinstance(content, list):
        content = "".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content)
    text = str(content).strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("model response did not contain a JSON object")
    data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("model response JSON is not an object")
    return data
