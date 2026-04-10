"""
Заглушка будущего бэкенда MariaDB.

Реальный проект работает только с PostgreSQL (см. shared/database.py).
Полный чеклист переноса: docs/mariadb-migration-future.md
"""

from __future__ import annotations

__all__ = ["MARIADB_MIGRATION_PLANNED", "require_postgres_database_url"]


MARIADB_MIGRATION_PLANNED = True


def require_postgres_database_url(database_url: str) -> None:
    """
    Явная проверка на этапе старта (опционально вызвать из entrypoint'ов),
    чтобы случайно не запуститься с неподдерживаемым URL до готовности миграции.
    """
    u = (database_url or "").strip().lower()
    if u.startswith("mysql") or u.startswith("mariadb"):
        raise RuntimeError(
            "Async MariaDB ещё не подключён. См. docs/mariadb-migration-future.md "
            "и shared/db_mariadb_future.py"
        )
