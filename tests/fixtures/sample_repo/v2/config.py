"""Application configuration constants."""

DATABASE_URL = "sqlite:///./app.db"
DEBUG = False
MAX_RETRIES = 3
TIMEOUT_SECONDS = 30
LOG_LEVEL = "INFO"


def get_config() -> dict:
    """Return the application configuration as a dictionary."""
    return {
        "database_url": DATABASE_URL,
        "debug": DEBUG,
        "max_retries": MAX_RETRIES,
        "timeout": TIMEOUT_SECONDS,
        "log_level": LOG_LEVEL,
    }
