"""Async SQLite database for ImageAPI."""

import asyncio
import time
from contextlib import asynccontextmanager
from datetime import datetime, date
from pathlib import Path
from typing import Any, Optional

import aiosqlite

from .logger import logger
from .models import AccountDB, AccountStatsDB, TaskDB

DB_PATH = Path(__file__).parent.parent.parent / "data" / "imageapi" / "imageapi.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

_MIGRATIONS = [
    ("accounts", "proxy", "TEXT"),
    ("accounts", "user_paygate_tier", "TEXT"),
    ("accounts", "credits", "INTEGER"),
    ("tasks", "resolution", "TEXT"),
    ("tasks", "image_urls", "TEXT"),
]

_ACCOUNT_COLUMNS = frozenset({
    "email", "st", "at", "at_expires", "project_id", "credits",
    "user_paygate_tier", "is_active", "status", "remark", "proxy",
    "last_used_at", "use_count",
})

_TASK_COLUMNS = frozenset({
    "account_id", "prompt", "model", "aspect_ratio", "seed", "resolution", "status",
    "images_count", "result_dir", "image_urls", "error_message", "started_at",
    "completed_at", "processing_time",
})


def _validate_columns(kwargs: dict[str, Any], allowed: frozenset[str], table: str) -> None:
    invalid = set(kwargs.keys()) - allowed
    if invalid:
        raise ValueError(f"Invalid column(s) for {table}: {', '.join(sorted(invalid))}")


class Database:
    """Async SQLite with connection pool."""

    def __init__(self, pool_size: int = 5, timeout: float = 30.0):
        self._pool_size = pool_size
        self._timeout = timeout
        self._pool: asyncio.Queue[aiosqlite.Connection] = asyncio.Queue(maxsize=pool_size)
        self._connected = False

    async def connect(self) -> None:
        if self._connected:
            return
        for _ in range(self._pool_size):
            conn = await aiosqlite.connect(str(DB_PATH), isolation_level=None)
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA busy_timeout=5000")
            self._pool.put_nowait(conn)
        self._connected = True
        await self._create_tables()
        logger.info(f"Database connected: {DB_PATH} (pool={self._pool_size})")

    async def close(self) -> None:
        if not self._connected:
            return
        self._connected = False
        while not self._pool.empty():
            conn = self._pool.get_nowait()
            await conn.close()
        logger.info("Database closed")

    @asynccontextmanager
    async def _acquire(self):
        conn = await asyncio.wait_for(self._pool.get(), timeout=self._timeout)
        try:
            yield conn
        finally:
            self._pool.put_nowait(conn)

    async def _create_tables(self) -> None:
        async with self._acquire() as conn:
            await conn.executescript("""
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT NOT NULL UNIQUE,
                    st TEXT NOT NULL,
                    at TEXT,
                    at_expires TIMESTAMP,
                    project_id TEXT,
                    credits INTEGER,
                    user_paygate_tier TEXT,
                    is_active BOOLEAN DEFAULT 1,
                    status TEXT DEFAULT 'active',
                    remark TEXT,
                    proxy TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_used_at TIMESTAMP,
                    use_count INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS account_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL UNIQUE REFERENCES accounts(id) ON DELETE CASCADE,
                    total_tasks INTEGER DEFAULT 0,
                    success_count INTEGER DEFAULT 0,
                    error_count INTEGER DEFAULT 0,
                    consecutive_errors INTEGER DEFAULT 0,
                    today_count INTEGER DEFAULT 0,
                    today_date TEXT DEFAULT (date('now'))
                );

                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL UNIQUE,
                    account_id INTEGER REFERENCES accounts(id),
                    prompt TEXT NOT NULL,
                    model TEXT DEFAULT 'fast',
                    aspect_ratio TEXT,
                    seed INTEGER,
                    status TEXT DEFAULT 'pending',
                    images_count INTEGER DEFAULT 0,
                    result_dir TEXT,
                    error_message TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    started_at TIMESTAMP,
                    completed_at TIMESTAMP,
                    processing_time REAL
                );

                CREATE INDEX IF NOT EXISTS idx_accounts_status ON accounts(status);
                CREATE INDEX IF NOT EXISTS idx_accounts_active ON accounts(is_active);
                CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
                CREATE INDEX IF NOT EXISTS idx_tasks_task_id ON tasks(task_id);
                CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at);
            """)
            await self._apply_migrations(conn)

    async def _apply_migrations(self, conn: aiosqlite.Connection) -> None:
        for table, column, col_type in _MIGRATIONS:
            try:
                await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
                logger.info(f"Migration: added {table}.{column}")
            except Exception:
                pass

    # ---- Account CRUD ----

    async def add_account(self, email: str, st: str, remark: str = "", proxy: str = "") -> int:
        async with self._acquire() as conn:
            cursor = await conn.execute(
                "INSERT INTO accounts (email, st, remark, proxy) VALUES (?, ?, ?, ?)",
                (email, st, remark, proxy),
            )
            account_id = cursor.lastrowid
            await conn.execute(
                "INSERT INTO account_stats (account_id) VALUES (?)", (account_id,)
            )
            return account_id

    async def get_account(self, account_id: int) -> Optional[AccountDB]:
        async with self._acquire() as conn:
            cursor = await conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,))
            row = await cursor.fetchone()
            return AccountDB(**dict(row)) if row else None

    async def get_account_by_email(self, email: str) -> Optional[AccountDB]:
        async with self._acquire() as conn:
            cursor = await conn.execute("SELECT * FROM accounts WHERE email = ?", (email,))
            row = await cursor.fetchone()
            return AccountDB(**dict(row)) if row else None

    async def get_all_accounts(self) -> list[AccountDB]:
        async with self._acquire() as conn:
            cursor = await conn.execute("SELECT * FROM accounts ORDER BY id")
            rows = await cursor.fetchall()
            return [AccountDB(**dict(r)) for r in rows]

    async def get_active_accounts(self) -> list[AccountDB]:
        async with self._acquire() as conn:
            cursor = await conn.execute(
                "SELECT * FROM accounts WHERE is_active = 1 AND status = 'active' ORDER BY id"
            )
            rows = await cursor.fetchall()
            return [AccountDB(**dict(r)) for r in rows]

    async def update_account(self, account_id: int, **kwargs: Any) -> None:
        if not kwargs:
            return
        _validate_columns(kwargs, _ACCOUNT_COLUMNS, "accounts")
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        values = list(kwargs.values()) + [account_id]
        async with self._acquire() as conn:
            await conn.execute(f"UPDATE accounts SET {sets} WHERE id = ?", values)

    async def delete_account(self, account_id: int) -> None:
        async with self._acquire() as conn:
            await conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))

    # ---- Account Stats ----

    async def get_account_stats(self, account_id: int) -> Optional[AccountStatsDB]:
        async with self._acquire() as conn:
            cursor = await conn.execute(
                "SELECT * FROM account_stats WHERE account_id = ?", (account_id,)
            )
            row = await cursor.fetchone()
            return AccountStatsDB(**dict(row)) if row else None

    async def increment_account_stats(self, account_id: int, success: bool = True) -> None:
        today = date.today().isoformat()
        async with self._acquire() as conn:
            # Reset today_count if new day
            await conn.execute(
                """UPDATE account_stats
                   SET today_count = CASE WHEN today_date = ? THEN today_count ELSE 0 END,
                       today_date = ?
                   WHERE account_id = ?""",
                (today, today, account_id),
            )
            if success:
                await conn.execute(
                    """UPDATE account_stats
                       SET total_tasks = total_tasks + 1,
                           success_count = success_count + 1,
                           consecutive_errors = 0,
                           today_count = today_count + 1
                       WHERE account_id = ?""",
                    (account_id,),
                )
            else:
                await conn.execute(
                    """UPDATE account_stats
                       SET total_tasks = total_tasks + 1,
                           error_count = error_count + 1,
                           consecutive_errors = consecutive_errors + 1,
                           today_count = today_count + 1
                       WHERE account_id = ?""",
                    (account_id,),
                )

    async def reset_account_failures(self, account_id: int) -> None:
        async with self._acquire() as conn:
            await conn.execute(
                "UPDATE account_stats SET consecutive_errors = 0 WHERE account_id = ?",
                (account_id,),
            )

    # ---- Task CRUD ----

    async def create_task(self, task_id: str, prompt: str, model: str = "fast",
                          aspect_ratio: Optional[str] = None, seed: Optional[int] = None,
                          resolution: Optional[str] = None) -> int:
        async with self._acquire() as conn:
            cursor = await conn.execute(
                """INSERT INTO tasks (task_id, prompt, model, aspect_ratio, seed, resolution)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (task_id, prompt, model, aspect_ratio, seed, resolution),
            )
            return cursor.lastrowid

    async def get_task(self, task_id: str) -> Optional[TaskDB]:
        async with self._acquire() as conn:
            cursor = await conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
            row = await cursor.fetchone()
            return TaskDB(**dict(row)) if row else None

    async def update_task(self, task_id: str, **kwargs: Any) -> None:
        if not kwargs:
            return
        _validate_columns(kwargs, _TASK_COLUMNS, "tasks")
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        values = list(kwargs.values()) + [task_id]
        async with self._acquire() as conn:
            await conn.execute(f"UPDATE tasks SET {sets} WHERE task_id = ?", values)

    async def get_tasks(self, limit: int = 50, offset: int = 0,
                        status: Optional[str] = None) -> list[TaskDB]:
        async with self._acquire() as conn:
            if status:
                cursor = await conn.execute(
                    "SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (status, limit, offset),
                )
            else:
                cursor = await conn.execute(
                    "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                )
            rows = await cursor.fetchall()
            return [TaskDB(**dict(r)) for r in rows]

    async def get_task_count(self, status: Optional[str] = None) -> int:
        async with self._acquire() as conn:
            if status:
                cursor = await conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE status = ?", (status,)
                )
            else:
                cursor = await conn.execute("SELECT COUNT(*) FROM tasks")
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def get_task_counts(self) -> dict[str, int]:
        """Get counts per status in one query."""
        async with self._acquire() as conn:
            cursor = await conn.execute(
                "SELECT status, COUNT(*) as cnt FROM tasks GROUP BY status"
            )
            rows = await cursor.fetchall()
            counts = {row["status"]: row["cnt"] for row in rows}
            counts["total"] = sum(counts.values())
            return counts

    async def get_all_account_stats(self) -> dict[int, AccountStatsDB]:
        """Get all account stats keyed by account_id."""
        async with self._acquire() as conn:
            cursor = await conn.execute("SELECT * FROM account_stats")
            rows = await cursor.fetchall()
            return {row["account_id"]: AccountStatsDB(**dict(row)) for row in rows}

    async def get_recent_tasks(self, limit: int = 25) -> list[TaskDB]:
        """Get recent tasks for dashboard."""
        async with self._acquire() as conn:
            cursor = await conn.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
            )
            rows = await cursor.fetchall()
            return [TaskDB(**dict(r)) for r in rows]

    async def cleanup_old_tasks(self, max_age_days: int = 1) -> int:
        async with self._acquire() as conn:
            cursor = await conn.execute(
                "DELETE FROM tasks WHERE created_at < datetime('now', ?)",
                (f"-{max_age_days} days",),
            )
            return cursor.rowcount


db = Database()
