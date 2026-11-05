"""Where drafts and call reports live: D1 on Cloudflare, SQLite anywhere else.

Both stores run the same SQL (``migrations/*.sql`` is the schema for each), so
the tests exercise the queries the Worker runs. Only ``_run`` differs.
JSON columns are encoded and decoded here, so callers see plain dicts and lists.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

MIGRATIONS = Path(__file__).resolve().parent.parent.parent / "migrations"

CALL_COLUMNS = (
    "id",
    "draft_id",
    "created_at",
    "updated_at",
    "mode",
    "callee_name",
    "to_number",
    "topic",
    "reference",
    "brief_json",
    "status",
    "outcome",
    "reason",
    "summary",
    "answers_json",
    "follow_ups_json",
    "details_json",
    "shared_json",
    "evaluation_json",
    "conversation_id",
    "batch_id",
    "scheduled_for",
    "started_at",
    "ended_at",
    "duration_secs",
    "cost",
    "transcript_json",
    "transcript_purge_at",
    "notified_at",
)
DRAFT_COLUMNS = (
    "id",
    "created_at",
    "expires_at",
    "brief_json",
    "preview_json",
    "used_at",
    "call_id",
)
OPEN_STATUSES = ("scheduled", "dialing", "in_progress")
MENU_PATH_LIMIT = 10


def _encode(row: dict) -> dict:
    return {
        k: (json.dumps(v) if k.endswith("_json") and v is not None else v) for k, v in row.items()
    }


def _decode(row: dict | None) -> dict | None:
    if row is None:
        return None
    return {
        k: (json.loads(v) if k.endswith("_json") and isinstance(v, str) else v)
        for k, v in row.items()
    }


class Store:
    """The queries. Subclasses provide ``_run(sql, params) -> (rows, changes)``."""

    async def _run(self, sql: str, params: list[Any]) -> tuple[list[dict], int]:
        raise NotImplementedError

    async def _insert(self, table: str, columns: tuple[str, ...], row: dict) -> None:
        unknown = set(row) - set(columns)
        if unknown:
            raise ValueError(f"unknown {table} columns: {sorted(unknown)}")
        row = _encode(row)
        names = list(row)
        sql = f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})"  # noqa: S608 (names are checked against the column list)
        await self._run(sql, [row[n] for n in names])

    # Drafts ----------------------------------------------------------------

    async def insert_draft(self, row: dict) -> None:
        await self._insert("drafts", DRAFT_COLUMNS, row)

    async def get_draft(self, draft_id: str) -> dict | None:
        rows, _ = await self._run("SELECT * FROM drafts WHERE id = ?", [draft_id])
        return _decode(rows[0]) if rows else None

    async def claim_draft(self, draft_id: str, call_id: str, now: int) -> bool:
        """Use a draft for one call. False if it was used already or has expired."""
        _, changes = await self._run(
            "UPDATE drafts SET used_at = ?, call_id = ? "
            "WHERE id = ? AND used_at IS NULL AND expires_at > ?",
            [now, call_id, draft_id, now],
        )
        return changes == 1

    async def release_draft(self, draft_id: str) -> None:
        """Undo a claim when ElevenLabs refused the call, so the draft can be retried."""
        await self._run("UPDATE drafts SET used_at = NULL, call_id = NULL WHERE id = ?", [draft_id])

    async def purge_drafts(self, before: int) -> int:
        _, changes = await self._run("DELETE FROM drafts WHERE expires_at < ?", [before])
        return changes

    # Calls -----------------------------------------------------------------

    async def insert_call(self, row: dict) -> None:
        await self._insert("calls", CALL_COLUMNS, row)

    async def get_call(self, call_id: str) -> dict | None:
        rows, _ = await self._run("SELECT * FROM calls WHERE id = ?", [call_id])
        return _decode(rows[0]) if rows else None

    async def call_by_conversation(self, conversation_id: str) -> dict | None:
        rows, _ = await self._run(
            "SELECT * FROM calls WHERE conversation_id = ?", [conversation_id]
        )
        return _decode(rows[0]) if rows else None

    async def update_call(self, call_id: str, fields: dict) -> None:
        unknown = set(fields) - set(CALL_COLUMNS)
        if unknown or "id" in fields:
            raise ValueError(
                f"cannot update calls columns: {sorted(unknown | {'id'} & set(fields))}"
            )
        fields = _encode(fields)
        names = list(fields)
        sql = f"UPDATE calls SET {', '.join(f'{n} = ?' for n in names)} WHERE id = ?"  # noqa: S608 (names are checked against the column list)
        await self._run(sql, [*(fields[n] for n in names), call_id])

    async def delete_call(self, call_id: str) -> bool:
        _, changes = await self._run("DELETE FROM calls WHERE id = ?", [call_id])
        return changes == 1

    async def list_calls(
        self,
        *,
        outcome: str | None = None,
        status: str | None = None,
        since: int | None = None,
        reference: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        where, params = [], []
        if outcome:
            where.append("outcome = ?")
            params.append(outcome)
        if status:
            where.append("status = ?")
            params.append(status)
        if since is not None:
            where.append("created_at >= ?")
            params.append(since)
        if reference:
            where.append("reference = ?")
            params.append(reference)
        sql = "SELECT * FROM calls"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        rows, _ = await self._run(sql, [*params, limit])
        return [_decode(r) for r in rows]

    async def open_calls_before(self, before: int) -> list[dict]:
        """Calls still waiting on a result that should have one by now."""
        placeholders = ", ".join("?" * len(OPEN_STATUSES))
        rows, _ = await self._run(
            f"SELECT * FROM calls WHERE status IN ({placeholders}) "  # noqa: S608 (constant placeholders)
            "AND COALESCE(scheduled_for, created_at) < ? ORDER BY created_at LIMIT 50",
            [*OPEN_STATUSES, before],
        )
        return [_decode(r) for r in rows]

    # Menu paths ------------------------------------------------------------

    async def menu_path_worked(
        self, to_number: str, steps: list[dict], topic: str, call_id: str, now: int
    ) -> None:
        """Record that this path reached a person, adding it if it is new."""
        digits = ",".join(s["press"] for s in steps)
        await self._run(
            "INSERT INTO menu_paths (to_number, digits, steps_json, topic, created_at, "
            "updated_at, worked_count, last_worked_at, last_call_id) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?) "
            "ON CONFLICT (to_number, digits) DO UPDATE SET "
            "steps_json = excluded.steps_json, topic = excluded.topic, "
            "updated_at = excluded.updated_at, worked_count = worked_count + 1, "
            "last_worked_at = excluded.last_worked_at, last_call_id = excluded.last_call_id",
            [to_number, digits, json.dumps(steps), topic, now, now, now, call_id],
        )

    async def menu_path_failed(self, to_number: str, digits: str, now: int) -> bool:
        """Mark a saved path as having failed. False if there is no such path."""
        _, changes = await self._run(
            "UPDATE menu_paths SET failed_count = failed_count + 1, last_failed_at = ?, "
            "updated_at = ? WHERE to_number = ? AND digits = ?",
            [now, now, to_number, digits],
        )
        return changes == 1

    async def menu_paths(self, to_number: str) -> list[dict]:
        rows, _ = await self._run(
            "SELECT * FROM menu_paths WHERE to_number = ? "
            "ORDER BY COALESCE(last_worked_at, 0) DESC, digits LIMIT ?",
            [to_number, MENU_PATH_LIMIT],
        )
        return [_decode(r) for r in rows]

    async def delete_menu_paths(self, to_number: str, digits: str | None = None) -> int:
        if digits is None:
            _, changes = await self._run("DELETE FROM menu_paths WHERE to_number = ?", [to_number])
        else:
            _, changes = await self._run(
                "DELETE FROM menu_paths WHERE to_number = ? AND digits = ?", [to_number, digits]
            )
        return changes

    async def purge_transcripts(self, now: int) -> int:
        _, changes = await self._run(
            "UPDATE calls SET transcript_json = NULL, transcript_purge_at = NULL "
            "WHERE transcript_purge_at IS NOT NULL AND transcript_purge_at <= ?",
            [now],
        )
        return changes


class SqliteStore(Store):
    """For tests and for running outside Cloudflare (``uvicorn``)."""

    def __init__(self, path: str = ":memory:"):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row

    def migrate(self) -> None:
        """Apply the migrations not yet applied, counted in ``PRAGMA user_version``.

        A database made before the count existed (version 0, ``calls`` present)
        has the first migration already.
        """
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if (
            version == 0
            and self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='calls'"
            ).fetchone()
        ):
            version = 1
        files = sorted(MIGRATIONS.glob("*.sql"))
        for i, f in enumerate(files, 1):
            if i > version:
                self.conn.executescript(f.read_text())
        self.conn.execute(f"PRAGMA user_version = {len(files)}")
        self.conn.commit()

    async def _run(self, sql: str, params: list[Any]) -> tuple[list[dict], int]:
        cur = self.conn.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
        self.conn.commit()
        return rows, cur.rowcount if cur.rowcount >= 0 else 0


class D1Store(Store):
    """Cloudflare D1, through the Worker's ``DB`` binding."""

    def __init__(self, db: Any):
        self.db = db

    async def _run(self, sql: str, params: list[Any]) -> tuple[list[dict], int]:
        from pyodide.ffi import jsnull  # only exists inside the Worker

        bound = [jsnull if p is None else p for p in params]
        result = await self.db.prepare(sql).bind(*bound).all()
        rows = [dict(_py(r)) for r in (_py(getattr(result, "results", None)) or [])]
        meta = _py(getattr(result, "meta", None)) or {}
        return rows, int(meta.get("changes") or 0)


def _py(value: Any) -> Any:
    """A JS value as Python. The runtime converts some results already, and not others."""
    return value.to_py() if hasattr(value, "to_py") else value
