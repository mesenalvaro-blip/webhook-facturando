"""
Session persistence — PostgreSQL (production) or SQLite (local dev).

Detects backend via DATABASE_URL environment variable:
  - Set    → asyncpg → PostgreSQL (Render managed DB, persistent)
  - Absent → aiosqlite → SQLite file (local development)
"""

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

DATABASE_URL = os.environ.get("DATABASE_URL")

# ---------------------------------------------------------------------------
# PostgreSQL backend (production — Render)
# ---------------------------------------------------------------------------

if DATABASE_URL:
    import asyncpg

    _pool: Optional[asyncpg.Pool] = None

    async def init_db():
        global _pool
        # Render issues postgres:// URLs; asyncpg requires postgresql://
        url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
        _pool = await asyncpg.create_pool(url, min_size=1, max_size=5)
        await _pool.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id          TEXT PRIMARY KEY,
                created_at  TEXT NOT NULL,
                name        TEXT,
                segments    TEXT NOT NULL,
                summary     TEXT
            )
        """)

    async def create_session(name: Optional[str], segments: list, summary: Optional[dict]) -> dict:
        session_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        await _pool.execute(
            "INSERT INTO sessions (id, created_at, name, segments, summary) "
            "VALUES ($1, $2, $3, $4, $5)",
            session_id, now, name,
            json.dumps(segments),
            json.dumps(summary) if summary else None,
        )
        return {"id": session_id, "created_at": now, "name": name}

    async def get_session(session_id: str) -> Optional[dict]:
        row = await _pool.fetchrow(
            "SELECT * FROM sessions WHERE id = $1", session_id
        )
        if not row:
            return None
        return {
            "id":         row["id"],
            "created_at": row["created_at"],
            "name":       row["name"],
            "segments":   json.loads(row["segments"]),
            "summary":    json.loads(row["summary"]) if row["summary"] else None,
        }

    async def list_sessions(limit: int = 20) -> list:
        rows = await _pool.fetch(
            "SELECT id, created_at, name, summary "
            "FROM sessions ORDER BY created_at DESC LIMIT $1",
            limit,
        )
        return [
            {
                "id":         r["id"],
                "created_at": r["created_at"],
                "name":       r["name"],
                "summary":    json.loads(r["summary"]) if r["summary"] else None,
            }
            for r in rows
        ]

# ---------------------------------------------------------------------------
# SQLite backend (local development)
# ---------------------------------------------------------------------------

else:
    import aiosqlite

    DB_PATH = "effort_sparring.db"

    async def init_db():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id          TEXT PRIMARY KEY,
                    created_at  TEXT NOT NULL,
                    name        TEXT,
                    segments    TEXT NOT NULL,
                    summary     TEXT
                )
            """)
            await db.commit()

    async def create_session(name: Optional[str], segments: list, summary: Optional[dict]) -> dict:
        session_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO sessions (id, created_at, name, segments, summary) VALUES (?,?,?,?,?)",
                (
                    session_id, now, name,
                    json.dumps(segments),
                    json.dumps(summary) if summary else None,
                ),
            )
            await db.commit()
        return {"id": session_id, "created_at": now, "name": name}

    async def get_session(session_id: str) -> Optional[dict]:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                return {
                    "id":         row["id"],
                    "created_at": row["created_at"],
                    "name":       row["name"],
                    "segments":   json.loads(row["segments"]),
                    "summary":    json.loads(row["summary"]) if row["summary"] else None,
                }

    async def list_sessions(limit: int = 20) -> list:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id, created_at, name, summary "
                "FROM sessions ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ) as cursor:
                rows = await cursor.fetchall()
                return [
                    {
                        "id":         r["id"],
                        "created_at": r["created_at"],
                        "name":       r["name"],
                        "summary":    json.loads(r["summary"]) if r["summary"] else None,
                    }
                    for r in rows
                ]
