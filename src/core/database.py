"""SQLite database operations for VeoAPI."""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, date
from typing import Any, Optional

import aiosqlite

from .config import DB_PATH
from .logger import logger
from .models import (
    AccountDB,
    AccountStatsDB,
    ProxyStateDB,
    VideoDB,
    VideoStatus,
)

SCHEMA = """
-- Accounts table
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
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_used_at TIMESTAMP,
    use_count INTEGER DEFAULT 0,
    proxy TEXT
);

-- Account stats table
CREATE TABLE IF NOT EXISTS account_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL UNIQUE REFERENCES accounts(id) ON DELETE CASCADE,
    total_videos INTEGER DEFAULT 0,
    success_count INTEGER DEFAULT 0,
    error_count INTEGER DEFAULT 0,
    consecutive_errors INTEGER DEFAULT 0,
    today_count INTEGER DEFAULT 0,
    today_date TEXT DEFAULT (date('now'))
);

-- Videos table
CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id TEXT NOT NULL UNIQUE,
    account_id INTEGER REFERENCES accounts(id),
    task_id TEXT,
    scene_id TEXT,
    prompt TEXT NOT NULL,
    translated_prompt TEXT,
    detected_language TEXT,
    model TEXT NOT NULL,
    google_model_key TEXT NOT NULL,
    mode TEXT NOT NULL,
    aspect_ratio TEXT DEFAULT '16:9',
    start_image_path TEXT,
    end_image_path TEXT,
    reference_images_paths TEXT,
    continuation_video_id TEXT,
    seed INTEGER,
    status TEXT DEFAULT 'pending',
    progress INTEGER DEFAULT 0,
    progress_message TEXT,
    error_code TEXT,
    error_message TEXT,
    download_url TEXT,
    video_url TEXT,
    cost REAL,
    video_type TEXT DEFAULT 'normal',
    original_video_id TEXT,
    original_video_ids TEXT,
    file_size INTEGER,
    duration REAL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP
);

-- Image generation jobs for the migrated flow.google.com provider.
CREATE TABLE IF NOT EXISTS image_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    image_id TEXT NOT NULL UNIQUE,
    prompt TEXT NOT NULL,
    model TEXT NOT NULL,
    aspect_ratio TEXT NOT NULL,
    requested_count INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'pending',
    progress INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    error_message TEXT,
    reference_files TEXT,
    parent_image_id TEXT,
    parent_result_index INTEGER,
    revision INTEGER NOT NULL DEFAULT 1,
    result_files TEXT,
    result_metadata TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP
);

-- Admin config table (kept for schema compatibility)
CREATE TABLE IF NOT EXISTS admin_config (
    id INTEGER PRIMARY KEY DEFAULT 1,
    username TEXT DEFAULT 'admin',
    password TEXT,
    api_key TEXT,
    capsolver_api_key TEXT,
    error_ban_threshold INTEGER DEFAULT 5
);

-- Proxy config table
CREATE TABLE IF NOT EXISTS proxy_config (
    id INTEGER PRIMARY KEY DEFAULT 1,
    enabled BOOLEAN DEFAULT 0,
    url TEXT
);

-- Generation config table
CREATE TABLE IF NOT EXISTS generation_config (
    id INTEGER PRIMARY KEY DEFAULT 1,
    video_timeout INTEGER DEFAULT 1800,
    poll_interval INTEGER DEFAULT 5
);

-- Per-proxy rotation state. proxy_pool.txt is the source list; this table
-- layers usage/cooldown on top so the rotator can pick rested proxies and
-- bench burnt ones when Google flags the active set with unusual activity.
CREATE TABLE IF NOT EXISTS proxy_state (
    proxy_url TEXT PRIMARY KEY,
    label TEXT,
    status TEXT DEFAULT 'benched',          -- 'active' | 'benched' | 'cooldown'
    last_assigned_at TIMESTAMP,
    last_generation_at TIMESTAMP,
    cooldown_until TIMESTAMP,
    flagged_count INTEGER DEFAULT 0,
    flagged_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- IP Whitelist config table (kept for schema compatibility)
CREATE TABLE IF NOT EXISTS ip_whitelist_config (
    id INTEGER PRIMARY KEY DEFAULT 1,
    enabled BOOLEAN DEFAULT 0
);

-- IP Whitelist table (kept for schema compatibility)
CREATE TABLE IF NOT EXISTS ip_whitelist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ip_address TEXT NOT NULL UNIQUE,
    description TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Request logs table
CREATE TABLE IF NOT EXISTS request_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    client_ip TEXT,
    duration_ms INTEGER,
    video_id TEXT,
    error_message TEXT
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_accounts_status ON accounts(status);
CREATE INDEX IF NOT EXISTS idx_accounts_is_active ON accounts(is_active);
CREATE INDEX IF NOT EXISTS idx_accounts_last_used ON accounts(last_used_at);

CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status);
CREATE INDEX IF NOT EXISTS idx_videos_video_id ON videos(video_id);
CREATE INDEX IF NOT EXISTS idx_videos_created_at ON videos(created_at);
CREATE INDEX IF NOT EXISTS idx_videos_account_id ON videos(account_id);

CREATE INDEX IF NOT EXISTS idx_image_tasks_status ON image_tasks(status);
CREATE INDEX IF NOT EXISTS idx_image_tasks_created_at ON image_tasks(created_at);

CREATE INDEX IF NOT EXISTS idx_request_logs_timestamp ON request_logs(timestamp);
CREATE INDEX IF NOT EXISTS idx_request_logs_video_id ON request_logs(video_id);

CREATE INDEX IF NOT EXISTS idx_ip_whitelist_ip ON ip_whitelist(ip_address);

CREATE INDEX IF NOT EXISTS idx_proxy_state_status ON proxy_state(status);
"""

# New columns to add to existing databases
_MIGRATIONS = [
    ("videos", "translated_prompt", "TEXT"),
    ("videos", "detected_language", "TEXT"),
    ("videos", "video_type", "TEXT DEFAULT 'normal'"),
    ("videos", "original_video_id", "TEXT"),
    ("videos", "original_video_ids", "TEXT"),
    ("videos", "file_size", "INTEGER"),
    ("videos", "duration", "REAL"),
    ("videos", "progress_message", "TEXT"),
    ("accounts", "proxy", "TEXT"),  # Per-account proxy URL
    ("videos", "requested_resolution", "TEXT"),  # Auto-upscale target
    ("videos", "mask_path", "TEXT"),  # Path to saved mask image
    ("videos", "mask_coordinates", "TEXT"),  # JSON string of coordinates
    ("accounts", "relaxed_disabled", "BOOLEAN DEFAULT 0"),  # Per-account relaxed model disable
    ("accounts", "residential_proxy", "TEXT"),  # Per-account residential proxy (auth/generate/upscale)
    # ---- Browser provider (browser2api fork) ----
    ("accounts", "browser_ready", "BOOLEAN DEFAULT 0"),  # account usable for browser-mode generation
    ("accounts", "browser_last_check", "TIMESTAMP"),     # last successful browser session timestamp
    ("videos", "provider", "TEXT DEFAULT 'veo'"),        # 'veo' (http) | 'browser' (flow UI)
    ("videos", "project_url", "TEXT"),                   # labs.google/fx/tools/flow/project/<uuid> for extend/upscale
    # Cooldown tracking — survives restarts so throttle/rest timers don't reset
    ("accounts", "cooldown_until", "TIMESTAMP"),
    # When PENDING→GENERATING happened. Updated_at gets bumped on every
    # poll-progress write, so it's not a faithful "started generating"
    # marker — this column stays put across the whole generation and is
    # what fail_stale_generating_videos uses to enforce the generating SLA.
    ("videos", "generating_since", "TIMESTAMP"),
    # Hard pin: when True the account ALWAYS generates on the lower-
    # priority Flow queue (Veo 3.1 - Lite [Lower) regardless of which
    # model the user requested. Set via the dashboard "Relaxed mode"
    # toggle. Distinct from ``relaxed_disabled`` which is the opposite
    # signal (BLOCK relaxed on this account).
    ("accounts", "force_relaxed", "BOOLEAN DEFAULT 0"),
    # User's requested duration (preserves choice before completion duration overwrite)
    ("videos", "requested_duration", "REAL"),
    # Google OAuth refresh_token for cockpit-tools antigravity accounts
    ("accounts", "refresh_token", "TEXT"),
    ("image_tasks", "reference_files", "TEXT"),
    ("image_tasks", "parent_image_id", "TEXT"),
    ("image_tasks", "parent_result_index", "INTEGER"),
    ("image_tasks", "revision", "INTEGER NOT NULL DEFAULT 1"),
]

# Whitelists of allowed column names for dynamic UPDATE queries
_ACCOUNT_COLUMNS = frozenset({
    "email", "st", "at", "at_expires", "project_id", "credits",
    "user_paygate_tier", "is_active", "status", "remark",
    "last_used_at", "use_count", "proxy", "relaxed_disabled", "residential_proxy",
    "browser_ready", "browser_last_check", "cooldown_until", "force_relaxed",
    "refresh_token",
})

_PROXY_STATE_COLUMNS = frozenset({
    "label", "status", "last_assigned_at", "last_generation_at",
    "cooldown_until", "flagged_count", "flagged_at",
})

_VIDEO_COLUMNS = frozenset({
    "account_id", "task_id", "scene_id", "prompt", "translated_prompt",
    "detected_language", "model", "google_model_key", "mode",
    "aspect_ratio", "start_image_path", "end_image_path",
    "reference_images_paths", "continuation_video_id", "seed",
    "status", "progress", "progress_message", "error_code",
    "error_message", "download_url", "video_url", "cost",
    "video_type", "requested_resolution", "original_video_id", "original_video_ids",
    "mask_path", "mask_coordinates",
    "file_size", "duration", "updated_at", "completed_at",
    "provider", "project_url", "generating_since", "requested_duration",
})

_IMAGE_TASK_COLUMNS = frozenset({
    "prompt", "model", "aspect_ratio", "requested_count", "status",
    "progress", "error_code", "error_message", "result_files",
    "result_metadata", "reference_files", "parent_image_id",
    "parent_result_index", "revision", "updated_at", "completed_at",
})

# Default connection pool size
_POOL_SIZE = 5

# Timeout for acquiring a connection from the pool (seconds)
_POOL_ACQUIRE_TIMEOUT = 30


def _validate_columns(kwargs: dict[str, Any], allowed: frozenset[str], table: str) -> None:
    """Validate that all kwargs keys are in the allowed column set."""
    invalid = set(kwargs.keys()) - allowed
    if invalid:
        raise ValueError(
            f"Invalid column(s) for {table}: {', '.join(sorted(invalid))}"
        )


class Database:
    """Async SQLite database wrapper with connection pool."""

    def __init__(self, pool_size: int = _POOL_SIZE):
        self._pool: Optional[asyncio.Queue[aiosqlite.Connection]] = None
        self._pool_size = pool_size

    async def _create_connection(self) -> aiosqlite.Connection:
        """Create and configure a single database connection."""
        conn = await aiosqlite.connect(
            DB_PATH,
            isolation_level=None,  # Autocommit mode
        )
        conn.row_factory = aiosqlite.Row

        # Enable WAL mode for better concurrency
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")
        await conn.execute("PRAGMA cache_size=10000")
        await conn.execute("PRAGMA temp_store=MEMORY")

        return conn

    async def connect(self) -> None:
        """Connect to the database, initialize schema, and create connection pool."""
        # Create first connection for schema setup and migrations
        init_conn = await self._create_connection()
        await init_conn.executescript(SCHEMA)
        await self._apply_migrations(init_conn)

        # Initialize connection pool
        self._pool = asyncio.Queue(maxsize=self._pool_size)
        await self._pool.put(init_conn)

        # Fill the rest of the pool
        for _ in range(self._pool_size - 1):
            conn = await self._create_connection()
            await self._pool.put(conn)

        logger.info(f"Database connected: {DB_PATH} (pool_size={self._pool_size})")

    async def _apply_migrations(self, conn: aiosqlite.Connection) -> None:
        """Add new columns to existing tables if they don't exist."""
        for table, column, col_type in _MIGRATIONS:
            try:
                await conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"
                )
                logger.info(f"Migration: added {table}.{column}")
            except Exception:
                # Column already exists
                pass

    @asynccontextmanager
    async def _acquire(self):
        """Acquire a connection from the pool and return it when done."""
        try:
            conn = await asyncio.wait_for(
                self._pool.get(), timeout=_POOL_ACQUIRE_TIMEOUT
            )
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"Database connection pool exhausted "
                f"(timeout={_POOL_ACQUIRE_TIMEOUT}s, pool_size={self._pool_size})"
            )
        try:
            yield conn
        finally:
            await self._pool.put(conn)

    async def close(self) -> None:
        """Close all database connections in the pool."""
        if self._pool:
            closed = 0
            while not self._pool.empty():
                conn = await self._pool.get()
                await conn.close()
                closed += 1
            self._pool = None
            logger.info(f"Database connections closed ({closed})")

    async def execute(
        self, query: str, params: tuple = ()
    ) -> aiosqlite.Cursor:
        """Execute a query."""
        async with self._acquire() as conn:
            return await conn.execute(query, params)

    async def executemany(
        self, query: str, params_list: list[tuple]
    ) -> aiosqlite.Cursor:
        """Execute a query with multiple parameter sets."""
        async with self._acquire() as conn:
            return await conn.executemany(query, params_list)

    async def fetchone(
        self, query: str, params: tuple = ()
    ) -> Optional[aiosqlite.Row]:
        """Fetch a single row."""
        async with self._acquire() as conn:
            cursor = await conn.execute(query, params)
            return await cursor.fetchone()

    async def fetchall(
        self, query: str, params: tuple = ()
    ) -> list[aiosqlite.Row]:
        """Fetch all rows."""
        async with self._acquire() as conn:
            cursor = await conn.execute(query, params)
            return await cursor.fetchall()

    async def commit(self) -> None:
        """Commit the transaction (no-op in autocommit mode, kept for API compat)."""
        pass

    # Account operations
    async def create_account(
        self,
        email: str,
        st: str,
        remark: Optional[str] = None,
        proxy: Optional[str] = None,
        refresh_token: Optional[str] = None,
    ) -> int:
        """Create a new account."""
        async with self._acquire() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO accounts (email, st, remark, proxy, refresh_token, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (email, st, remark, proxy, refresh_token, datetime.utcnow()),
            )
            account_id = cursor.lastrowid

            # Create stats record
            await conn.execute(
                """
                INSERT INTO account_stats (account_id, today_date)
                VALUES (?, ?)
                """,
                (account_id, date.today().isoformat()),
            )

            return account_id

    async def get_account(self, account_id: int) -> Optional[AccountDB]:
        """Get an account by ID."""
        row = await self.fetchone(
            "SELECT * FROM accounts WHERE id = ?", (account_id,)
        )
        if row:
            return AccountDB(**dict(row))
        return None

    async def get_account_by_email(self, email: str) -> Optional[AccountDB]:
        """Get an account by email."""
        row = await self.fetchone(
            "SELECT * FROM accounts WHERE email = ?", (email,)
        )
        if row:
            return AccountDB(**dict(row))
        return None

    async def get_active_accounts(self) -> list[AccountDB]:
        """Get all active accounts (including relaxed_only and free_tier with credits)."""
        rows = await self.fetchall(
            """
            SELECT * FROM accounts
            WHERE status IN ('active', 'relaxed_only', 'free_tier') AND is_active = 1
            ORDER BY last_used_at ASC NULLS FIRST
            """
        )
        return [AccountDB(**dict(row)) for row in rows]

    async def get_all_accounts(self) -> list[AccountDB]:
        """Get all accounts."""
        rows = await self.fetchall(
            "SELECT * FROM accounts ORDER BY created_at DESC"
        )
        return [AccountDB(**dict(row)) for row in rows]

    async def update_account(
        self, account_id: int, **kwargs: Any
    ) -> None:
        """Update account fields."""
        if not kwargs:
            return

        _validate_columns(kwargs, _ACCOUNT_COLUMNS, "accounts")

        set_clause = ", ".join(f"{k} = ?" for k in kwargs.keys())
        values = list(kwargs.values()) + [account_id]

        await self.execute(
            f"UPDATE accounts SET {set_clause} WHERE id = ?",
            tuple(values),
        )

    async def delete_account(self, account_id: int) -> None:
        """Delete an account."""
        async with self._acquire() as conn:
            await conn.execute(
                "DELETE FROM accounts WHERE id = ?", (account_id,)
            )
            await conn.execute(
                "DELETE FROM account_stats WHERE account_id = ?", (account_id,)
            )

    async def update_account_used(self, account_id: int) -> None:
        """Update account last used timestamp and increment use count."""
        await self.execute(
            """
            UPDATE accounts
            SET last_used_at = ?, use_count = use_count + 1
            WHERE id = ?
            """,
            (datetime.utcnow(), account_id),
        )

    async def touch_account_last_used(self, account_id: int) -> None:
        """Bump ``last_used_at`` only — no ``use_count`` increment.

        Called by the load balancer at selection time so that concurrent
        ``get_account`` calls see this account as "recently used" and pick
        a different one. Without this, the next request reads the same
        stale ``last_used_at`` (only updated when the generation finishes)
        and piles up onto the same account, defeating rotation entirely.
        ``use_count`` is left to ``update_account_used`` so the dashboard
        counter still reflects completed-attempt counts, not selections.
        """
        await self.execute(
            "UPDATE accounts SET last_used_at = ? WHERE id = ?",
            (datetime.utcnow(), account_id),
        )

    # ---- Proxy rotation state ----

    async def sync_proxy_states(self, entries: list[tuple[str, str]]) -> int:
        """Upsert proxies from proxy_pool.txt into proxy_state.

        ``entries`` is a list of ``(proxy_url, label)``. New proxies are
        inserted as ``benched``; existing rows are left untouched except for
        a label refresh (so renaming a proxy in the file doesn't wipe its
        cooldown). Returns the number of newly-inserted proxies.
        """
        if not entries:
            return 0
        inserted = 0
        async with self._acquire() as conn:
            for url, label in entries:
                cursor = await conn.execute(
                    "SELECT 1 FROM proxy_state WHERE proxy_url = ?", (url,)
                )
                exists = await cursor.fetchone()
                if exists:
                    await conn.execute(
                        "UPDATE proxy_state SET label = ? WHERE proxy_url = ?",
                        (label, url),
                    )
                else:
                    await conn.execute(
                        """
                        INSERT INTO proxy_state (proxy_url, label, status, created_at)
                        VALUES (?, ?, 'benched', ?)
                        """,
                        (url, label, datetime.utcnow()),
                    )
                    inserted += 1
        return inserted

    async def get_all_proxy_states(self) -> list[ProxyStateDB]:
        """Get all tracked proxy states."""
        rows = await self.fetchall("SELECT * FROM proxy_state")
        return [ProxyStateDB(**dict(row)) for row in rows]

    async def get_proxy_state(self, proxy_url: str) -> Optional[ProxyStateDB]:
        """Get the rotation state for a single proxy URL."""
        row = await self.fetchone(
            "SELECT * FROM proxy_state WHERE proxy_url = ?", (proxy_url,)
        )
        if row:
            return ProxyStateDB(**dict(row))
        return None

    async def update_proxy_state(self, proxy_url: str, **kwargs: Any) -> None:
        """Update proxy_state fields for a single proxy."""
        if not kwargs:
            return
        _validate_columns(kwargs, _PROXY_STATE_COLUMNS, "proxy_state")
        set_clause = ", ".join(f"{k} = ?" for k in kwargs.keys())
        values = list(kwargs.values()) + [proxy_url]
        await self.execute(
            f"UPDATE proxy_state SET {set_clause} WHERE proxy_url = ?",
            tuple(values),
        )

    async def mark_proxy_generation(self, proxy_url: str) -> None:
        """Bump ``last_generation_at`` for a proxy (called on each successful
        generation so the rotator's 'rested ≥N hours' filter stays honest)."""
        await self.execute(
            "UPDATE proxy_state SET last_generation_at = ? WHERE proxy_url = ?",
            (datetime.utcnow(), proxy_url),
        )

    # Account stats operations
    async def get_account_stats(
        self, account_id: int
    ) -> Optional[AccountStatsDB]:
        """Get account stats."""
        row = await self.fetchone(
            "SELECT * FROM account_stats WHERE account_id = ?",
            (account_id,),
        )
        if row:
            return AccountStatsDB(**dict(row))
        return None

    async def get_all_account_stats(self) -> dict[int, AccountStatsDB]:
        """Get all account stats as a dict keyed by account_id."""
        rows = await self.fetchall("SELECT * FROM account_stats")
        return {
            row["account_id"]: AccountStatsDB(**dict(row))
            for row in rows
        }

    async def increment_account_stats(
        self, account_id: int, success: bool
    ) -> None:
        """Increment account stats after video generation."""
        today = date.today().isoformat()

        async with self._acquire() as conn:
            # Check if stats exist
            cursor = await conn.execute(
                "SELECT * FROM account_stats WHERE account_id = ?",
                (account_id,),
            )
            stats_row = await cursor.fetchone()

            if not stats_row:
                # Create stats if not exists
                await conn.execute(
                    """
                    INSERT INTO account_stats (account_id, today_date)
                    VALUES (?, ?)
                    """,
                    (account_id, today),
                )
                cursor = await conn.execute(
                    "SELECT * FROM account_stats WHERE account_id = ?",
                    (account_id,),
                )
                stats_row = await cursor.fetchone()

            stats = AccountStatsDB(**dict(stats_row)) if stats_row else None

            # Reset today's count if date changed
            if stats and stats.today_date != today:
                await conn.execute(
                    """
                    UPDATE account_stats
                    SET today_count = 0, today_date = ?
                    WHERE account_id = ?
                    """,
                    (today, account_id),
                )

            if success:
                await conn.execute(
                    """
                    UPDATE account_stats
                    SET total_videos = total_videos + 1,
                        success_count = success_count + 1,
                        consecutive_errors = 0,
                        today_count = today_count + 1
                    WHERE account_id = ?
                    """,
                    (account_id,),
                )
            else:
                await conn.execute(
                    """
                    UPDATE account_stats
                    SET total_videos = total_videos + 1,
                        error_count = error_count + 1,
                        consecutive_errors = consecutive_errors + 1,
                        today_count = today_count + 1
                    WHERE account_id = ?
                    """,
                    (account_id,),
                )

    async def reset_consecutive_errors(self, account_id: int) -> None:
        """Reset consecutive errors for an account."""
        await self.execute(
            """
            UPDATE account_stats
            SET consecutive_errors = 0
            WHERE account_id = ?
            """,
            (account_id,),
        )

    # Video operations
    async def create_video(
        self,
        video_id: str,
        prompt: str,
        model: str,
        google_model_key: str,
        mode: str,
        aspect_ratio: str = "16:9",
        account_id: Optional[int] = None,
        start_image_path: Optional[str] = None,
        end_image_path: Optional[str] = None,
        reference_images_paths: Optional[list[str]] = None,
        continuation_video_id: Optional[str] = None,
        seed: Optional[int] = None,
        translated_prompt: Optional[str] = None,
        detected_language: Optional[str] = None,
        video_type: str = "normal",
        requested_resolution: Optional[str] = None,
        original_video_id: Optional[str] = None,
        original_video_ids: Optional[list[str]] = None,
        status: str = "pending",
        mask_path: Optional[str] = None,
        mask_coordinates: Optional[str] = None,
        file_size: Optional[int] = None,
        duration: Optional[float] = None,
        download_url: Optional[str] = None,
        cost: Optional[float] = None,
        completed_at: Optional[datetime] = None,
        requested_duration: Optional[float] = None,
    ) -> int:
        """Create a new video record."""
        now = datetime.utcnow()
        ref_paths_json = (
            json.dumps(reference_images_paths)
            if reference_images_paths
            else None
        )
        orig_ids_json = (
            json.dumps(original_video_ids)
            if original_video_ids
            else None
        )

        async with self._acquire() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO videos (
                    video_id, account_id, prompt, translated_prompt, detected_language,
                    model, google_model_key, mode,
                    aspect_ratio, start_image_path, end_image_path,
                    reference_images_paths, continuation_video_id, seed,
                    status, video_type, requested_resolution,
                    original_video_id, original_video_ids,
                    mask_path, mask_coordinates,
                    file_size, duration, download_url, cost,
                    created_at, updated_at, completed_at, requested_duration
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    video_id,
                    account_id,
                    prompt,
                    translated_prompt,
                    detected_language,
                    model,
                    google_model_key,
                    mode,
                    aspect_ratio,
                    start_image_path,
                    end_image_path,
                    ref_paths_json,
                    continuation_video_id,
                    seed,
                    status,
                    video_type,
                    requested_resolution,
                    original_video_id,
                    orig_ids_json,
                    mask_path,
                    mask_coordinates,
                    file_size,
                    duration,
                    download_url,
                    cost,
                    now,
                    now,
                    completed_at,
                    requested_duration,
                ),
            )
            return cursor.lastrowid

    async def get_video(self, video_id: str) -> Optional[VideoDB]:
        """Get a video by video_id."""
        row = await self.fetchone(
            "SELECT * FROM videos WHERE video_id = ?", (video_id,)
        )
        if row:
            return VideoDB(**dict(row))
        return None

    async def get_video_by_id(self, id: int) -> Optional[VideoDB]:
        """Get a video by internal ID."""
        row = await self.fetchone(
            "SELECT * FROM videos WHERE id = ?", (id,)
        )
        if row:
            return VideoDB(**dict(row))
        return None

    async def get_videos(
        self,
        limit: int = 50,
        offset: int = 0,
        status: Optional[str] = None,
        sort_asc: bool = False,
    ) -> list[VideoDB]:
        """Get videos with pagination."""
        order = "ASC" if sort_asc else "DESC"
        if status:
            rows = await self.fetchall(
                f"""
                SELECT * FROM videos
                WHERE status = ?
                ORDER BY created_at {order}
                LIMIT ? OFFSET ?
                """,
                (status, limit, offset),
            )
        else:
            rows = await self.fetchall(
                f"""
                SELECT * FROM videos
                ORDER BY created_at {order}
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            )
        return [VideoDB(**dict(row)) for row in rows]

    async def get_generating_videos(self) -> list[VideoDB]:
        """Get all videos in generating status."""
        rows = await self.fetchall(
            """
            SELECT * FROM videos
            WHERE status = ?
            ORDER BY created_at ASC
            """,
            (VideoStatus.GENERATING.value,),
        )
        return [VideoDB(**dict(row)) for row in rows]

    # Statuses that terminate a video's lifecycle. Once a video is in one
    # of these, no in-flight worker should be able to overwrite it — the
    # watchdog (or an explicit user cancel) has decided this video is
    # done. Without this guard, a worker that finishes a slow generation
    # right after the SLA watchdog flipped the row to FAILED would
    # silently resurrect the video as COMPLETED, which is exactly the
    # "I see TIMEOUT but the video keeps generating" bug users reported.
    _TERMINAL_STATUSES: tuple[str, ...] = ("failed", "cancelled")

    async def update_video(self, video_id: str, **kwargs: Any) -> None:
        """Update video fields.

        IMPORTANT: never overwrites rows whose status is already terminal
        (``failed`` / ``cancelled``). This makes worker writes
        idempotent w.r.t. the watchdog: if the watchdog moved the video
        to FAILED while the worker was still grinding, the worker's
        eventual ``status=completed`` / ``progress=...`` writes are
        no-ops instead of resurrecting the video.
        """
        if not kwargs:
            return

        kwargs["updated_at"] = datetime.utcnow()
        _validate_columns(kwargs, _VIDEO_COLUMNS, "videos")

        set_clause = ", ".join(f"{k} = ?" for k in kwargs.keys())
        values = list(kwargs.values()) + [video_id]

        # Watchdogs / cancel endpoints must be able to flip a video INTO
        # a terminal state from any prior state — only normal worker
        # writes are guarded.
        new_status = kwargs.get("status")
        if isinstance(new_status, str) and new_status in self._TERMINAL_STATUSES:
            await self.execute(
                f"UPDATE videos SET {set_clause} WHERE video_id = ?",
                tuple(values),
            )
            return

        placeholders = ", ".join(["?"] * len(self._TERMINAL_STATUSES))
        await self.execute(
            f"UPDATE videos SET {set_clause} "
            f"WHERE video_id = ? AND status NOT IN ({placeholders})",
            tuple(values) + self._TERMINAL_STATUSES,
        )

    async def try_claim_video_for_generation(
        self,
        video_id: str,
        account_id: Optional[int],
        progress_message: str = "Generating...",
    ) -> bool:
        """Atomically transition a video from PENDING to GENERATING.

        Returns True if the claim succeeded; False if the video is no
        longer in PENDING (e.g. ``fail_stale_pending_videos`` watchdog
        already moved it to FAILED, or another worker beat us to it).

        Workers MUST call this instead of an unconditional
        ``update_video(status='generating', ...)``. Otherwise a
        long-running ``_wait_for_browser_account`` can resurrect a video
        that the watchdog killed minutes ago — the user sees FAILED in
        the API, then the worker overwrites it with GENERATING and
        eventually COMPLETED, which is exactly the bug we're guarding
        against.
        """
        now = datetime.utcnow()
        async with self._acquire() as conn:
            cursor = await conn.execute(
                """
                UPDATE videos
                SET status = 'generating',
                    account_id = ?,
                    progress_message = ?,
                    generating_since = ?,
                    updated_at = ?
                WHERE video_id = ? AND status = 'pending'
                """,
                (account_id, progress_message, now, now, video_id),
            )
            return cursor.rowcount > 0

    async def count_videos(self, status: Optional[str] = None) -> int:
        """Count videos."""
        if status:
            row = await self.fetchone(
                "SELECT COUNT(*) as cnt FROM videos WHERE status = ?",
                (status,),
            )
        else:
            row = await self.fetchone(
                "SELECT COUNT(*) as cnt FROM videos"
            )
        return row["cnt"] if row else 0

    async def get_video_counts(self) -> dict[str, int]:
        """Get video counts grouped by status."""
        rows = await self.fetchall(
            "SELECT status, COUNT(*) as cnt FROM videos GROUP BY status"
        )
        counts = {row["status"]: row["cnt"] for row in rows}
        # Ensure all statuses are present
        for s in ("pending", "generating", "completed", "failed", "cancelled"):
            counts.setdefault(s, 0)
        counts["total"] = sum(counts.values())
        return counts

    # Image generation operations
    async def create_image_task(
        self,
        image_id: str,
        prompt: str,
        model: str,
        aspect_ratio: str,
        requested_count: int,
        reference_files: Optional[list[str]] = None,
        parent_image_id: Optional[str] = None,
        parent_result_index: Optional[int] = None,
        revision: int = 1,
    ) -> int:
        """Create an image generation task and return its internal row id."""
        now = datetime.utcnow()
        async with self._acquire() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO image_tasks (
                    image_id, prompt, model, aspect_ratio, requested_count,
                    reference_files, parent_image_id, parent_result_index,
                    revision, status, progress, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)
                """,
                (
                    image_id,
                    prompt,
                    model,
                    aspect_ratio,
                    requested_count,
                    json.dumps(reference_files or []),
                    parent_image_id,
                    parent_result_index,
                    max(1, revision),
                    now,
                    now,
                ),
            )
            return cursor.lastrowid

    async def get_image_task(self, image_id: str) -> Optional[dict[str, Any]]:
        row = await self.fetchone(
            "SELECT * FROM image_tasks WHERE image_id = ?", (image_id,)
        )
        return dict(row) if row else None

    async def get_image_tasks(
        self,
        limit: int = 50,
        offset: int = 0,
        status: Optional[str] = None,
        sort_asc: bool = False,
    ) -> list[dict[str, Any]]:
        order = "ASC" if sort_asc else "DESC"
        if status:
            rows = await self.fetchall(
                f"""
                SELECT * FROM image_tasks
                WHERE status = ?
                ORDER BY created_at {order}
                LIMIT ? OFFSET ?
                """,
                (status, limit, offset),
            )
        else:
            rows = await self.fetchall(
                f"""
                SELECT * FROM image_tasks
                ORDER BY created_at {order}
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            )
        return [dict(row) for row in rows]

    async def update_image_task(self, image_id: str, **kwargs: Any) -> None:
        if not kwargs:
            return
        kwargs["updated_at"] = datetime.utcnow()
        _validate_columns(kwargs, _IMAGE_TASK_COLUMNS, "image_tasks")
        set_clause = ", ".join(f"{key} = ?" for key in kwargs)
        values = list(kwargs.values()) + [image_id]
        await self.execute(
            f"UPDATE image_tasks SET {set_clause} WHERE image_id = ?",
            tuple(values),
        )

    async def try_claim_image_task(self, image_id: str) -> bool:
        now = datetime.utcnow()
        async with self._acquire() as conn:
            cursor = await conn.execute(
                """
                UPDATE image_tasks
                SET status = 'generating', progress = 5, updated_at = ?
                WHERE image_id = ? AND status = 'pending'
                """,
                (now, image_id),
            )
            return cursor.rowcount > 0

    async def get_image_counts(self) -> dict[str, int]:
        rows = await self.fetchall(
            "SELECT status, COUNT(*) AS cnt FROM image_tasks GROUP BY status"
        )
        counts = {row["status"]: row["cnt"] for row in rows}
        for status in ("pending", "generating", "completed", "failed"):
            counts.setdefault(status, 0)
        counts["total"] = sum(counts.values())
        return counts

    # Config operations (kept for backward compatibility)
    async def get_generation_config(self) -> Optional[dict]:
        """Get generation config."""
        row = await self.fetchone(
            "SELECT * FROM generation_config WHERE id = 1"
        )
        return dict(row) if row else None

    async def get_proxy_config(self) -> Optional[dict]:
        """Get proxy config."""
        row = await self.fetchone("SELECT * FROM proxy_config WHERE id = 1")
        return dict(row) if row else None

    # Request logs
    async def log_request(
        self,
        method: str,
        path: str,
        status_code: int,
        client_ip: str,
        duration_ms: int,
        video_id: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        """Log a request."""
        await self.execute(
            """
            INSERT INTO request_logs (
                method, path, status_code, client_ip, duration_ms,
                video_id, error_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                method,
                path,
                status_code,
                client_ip,
                duration_ms,
                video_id,
                error_message,
            ),
        )

    async def get_request_logs(
        self, limit: int = 100, offset: int = 0
    ) -> list[dict]:
        """Get request logs."""
        rows = await self.fetchall(
            """
            SELECT * FROM request_logs
            ORDER BY timestamp DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        )
        return [dict(row) for row in rows]

    # Cleanup operations
    async def get_old_videos(self, max_age_days: int) -> list[VideoDB]:
        """Get videos older than max_age_days (for pre-deletion cleanup)."""
        rows = await self.fetchall(
            """
            SELECT * FROM videos
            WHERE created_at < datetime('now', '-' || ? || ' days')
            """,
            (max_age_days,),
        )
        return [VideoDB(**dict(row)) for row in rows]

    async def cleanup_old_videos(self, max_age_days: int) -> int:
        """Delete videos older than max_age_days."""
        async with self._acquire() as conn:
            cursor = await conn.execute(
                """
                DELETE FROM videos
                WHERE created_at < datetime('now', '-' || ? || ' days')
                """,
                (max_age_days,),
            )
            return cursor.rowcount

    async def get_old_image_tasks(self, max_age_days: int) -> list[dict[str, Any]]:
        """Get terminal image tasks whose retention period has expired."""
        rows = await self.fetchall(
            """
            SELECT * FROM image_tasks
            WHERE status IN ('completed', 'failed')
              AND COALESCE(completed_at, updated_at, created_at)
                  < datetime('now', '-' || ? || ' days')
            """,
            (max_age_days,),
        )
        return [dict(row) for row in rows]

    async def delete_image_task(self, image_id: str) -> int:
        """Delete one image task after its stored files have been removed."""
        async with self._acquire() as conn:
            cursor = await conn.execute(
                "DELETE FROM image_tasks WHERE image_id = ?",
                (image_id,),
            )
            return cursor.rowcount

    async def cleanup_old_logs(self, max_age_days: int = 30) -> int:
        """Delete logs older than max_age_days."""
        async with self._acquire() as conn:
            cursor = await conn.execute(
                """
                DELETE FROM request_logs
                WHERE timestamp < datetime('now', '-' || ? || ' days')
                """,
                (max_age_days,),
            )
            return cursor.rowcount


# Global database instance
db = Database()
