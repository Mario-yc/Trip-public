from collections.abc import Generator
from pathlib import Path
import sqlite3

from src.core.config import get_settings


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def sqlite_path_from_url(database_url: str) -> Path:
    if database_url.startswith("sqlite:///"):
        path = Path(database_url.removeprefix("sqlite:///"))
    else:
        path = Path(database_url)
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def get_db() -> Generator[sqlite3.Connection, None, None]:
    settings = get_settings()
    db_path = sqlite_path_from_url(settings.database_url)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    try:
        yield connection
    finally:
        connection.close()
