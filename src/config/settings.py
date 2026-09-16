"""Loads configuration from .env (falls back to process env / defaults)."""
import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")


class Settings:
    GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")
    GROQ_MODEL: str = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "llama3.2:3b")
    LANGFUSE_PUBLIC_KEY: str = os.getenv("LANGFUSE_PUBLIC_KEY", "")
    LANGFUSE_SECRET_KEY: str = os.getenv("LANGFUSE_SECRET_KEY", "")
    LANGFUSE_HOST: str = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com")
    DB_PATH: str = os.getenv("DB_PATH", str(PROJECT_ROOT / "data" / "pipeline.db"))
    DB_CHECKPOINT_PATH: str = os.getenv(
        "DB_CHECKPOINT_PATH", str(PROJECT_ROOT / "data" / "langgraph_checkpoints.db")
    )
    # Retries allowed AFTER the original attempt (see .env.example).
    MAX_RETRIES: int = int(os.getenv("MAX_RETRIES", "1"))
    MAX_FILE_SIZE_BYTES: int = int(os.getenv("MAX_FILE_SIZE_BYTES", str(10 * 1024 * 1024)))
    UPLOAD_DIR: str = os.getenv("UPLOAD_DIR", str(PROJECT_ROOT / "data" / "uploads"))
    VALIDATION_SCHEMAS_PATH: str = str(Path(__file__).resolve().parent / "validation_schemas.yaml")
    # Governed analytical store — a THIRD database, separate from pipeline.db
    # (business outcomes) and langgraph_checkpoints.db (execution state).
    DB_GOVERNED_PATH: str = os.getenv("DB_GOVERNED_PATH", str(PROJECT_ROOT / "data" / "governed.duckdb"))
    GOVERNED_ROW_LIMIT: int = int(os.getenv("GOVERNED_ROW_LIMIT", "500"))
    GOVERNED_QUERY_TIMEOUT_S: float = float(os.getenv("GOVERNED_QUERY_TIMEOUT_S", "10"))


settings = Settings()
