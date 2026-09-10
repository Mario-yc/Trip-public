import sqlite3
from pathlib import Path

from src.core.database import PROJECT_ROOT


class ImageCleanupService:
    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def cleanup_temporary_originals(self) -> dict:
        rows = self.db.execute(
            """
            SELECT id, original_path
            FROM source_materials
            WHERE original_retention = 'temporary_cache'
              AND cache_status = 'retained'
              AND original_path IS NOT NULL
            """
        ).fetchall()
        cleared = 0
        for row in rows:
            original_path = PROJECT_ROOT / Path(row["original_path"])
            if original_path.exists() and original_path.is_file():
                original_path.unlink()
            self.db.execute(
                """
                UPDATE source_materials
                SET original_path = NULL, cache_status = 'manually_cleared'
                WHERE id = ?
                """,
                (row["id"],),
            )
            cleared += 1

        retained = self.db.execute(
            "SELECT COUNT(*) FROM source_materials WHERE original_retention = 'long_term_opt_in'"
        ).fetchone()[0]
        self.db.commit()
        return {"clearedCount": cleared, "retainedLongTermCount": retained}
