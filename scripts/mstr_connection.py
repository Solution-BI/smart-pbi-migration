"""Shared helper that builds an mstrio Connection from values in .env."""
import os

from dotenv import load_dotenv
from mstrio.connection import Connection

from paths import ENV_FILE

load_dotenv(ENV_FILE)


def _env_bool(name: str, default: bool = True) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "y")


def get_connection(project_id: str | None = None) -> Connection:
    """Return an authenticated mstrio Connection using settings from .env.

    If project_id is given it overrides MSTR_PROJECT_ID / MSTR_PROJECT_NAME.
    """
    base_url = os.getenv("MSTR_BASE_URL")
    username = os.getenv("MSTR_USERNAME")
    password = os.getenv("MSTR_PASSWORD")
    missing = [n for n, v in (("MSTR_BASE_URL", base_url),
                              ("MSTR_USERNAME", username),
                              ("MSTR_PASSWORD", password)) if not v]
    if missing:
        raise RuntimeError(f"Missing required settings in .env: {', '.join(missing)}")

    return Connection(
        base_url=base_url,
        username=username,
        password=password,
        login_mode=int(os.getenv("MSTR_LOGIN_MODE") or 1),
        project_name=None if project_id else (os.getenv("MSTR_PROJECT_NAME") or None),
        project_id=project_id or os.getenv("MSTR_PROJECT_ID") or None,
        ssl_verify=_env_bool("MSTR_SSL_VERIFY", True),
    )
