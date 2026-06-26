"""Small SQLite metadata store for the local bridge admin console."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class BridgeAdminStore:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _migrate(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS artifacts (
                    file_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    path TEXT NOT NULL,
                    download_url TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    bytes INTEGER,
                    account TEXT,
                    prompt TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS artifacts_created_idx
                    ON artifacts(created_at DESC);

                CREATE TABLE IF NOT EXISTS account_captures (
                    account TEXT PRIMARY KEY,
                    capture_path TEXT NOT NULL,
                    plan_type TEXT,
                    email_masked TEXT,
                    capabilities_json TEXT NOT NULL DEFAULT '{}',
                    checks_json TEXT NOT NULL DEFAULT '[]',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def record_artifact(
        self,
        asset: dict[str, Any],
        *,
        kind: str,
        account: str | None = None,
        prompt: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        with self._connect() as db:
            db.execute(
                """
                INSERT OR REPLACE INTO artifacts (
                    file_id, kind, filename, path, download_url, content_type, bytes,
                    account, prompt, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(
                    (SELECT created_at FROM artifacts WHERE file_id = ?),
                    ?
                ))
                """,
                (
                    str(asset.get("id") or ""),
                    kind,
                    str(asset.get("filename") or "download"),
                    str(asset.get("path") or ""),
                    str(asset.get("download_url") or ""),
                    str(asset.get("content_type") or "application/octet-stream"),
                    asset.get("bytes") if isinstance(asset.get("bytes"), int) else None,
                    account,
                    prompt,
                    json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
                    str(asset.get("id") or ""),
                    utc_now(),
                ),
            )

    def list_artifacts(self, *, limit: int = 100) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 500))
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM artifacts ORDER BY created_at DESC LIMIT ?",
                (safe_limit * 4,),
            ).fetchall()
        artifacts = [self._artifact_row(row) for row in rows]
        stale_ids = [artifact["file_id"] for artifact in artifacts if not artifact["exists"]]
        if stale_ids:
            self.delete_artifacts(stale_ids)
        return [artifact for artifact in artifacts if artifact["exists"]][:safe_limit]

    def get_artifact(self, file_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM artifacts WHERE file_id = ?", (file_id,)).fetchone()
        return self._artifact_row(row) if row is not None else None

    def artifact_count(self) -> int:
        with self._connect() as db:
            rows = db.execute("SELECT file_id, path FROM artifacts").fetchall()
        stale_ids = [row["file_id"] for row in rows if not Path(row["path"]).is_file()]
        if stale_ids:
            self.delete_artifacts(stale_ids)
        return len(rows) - len(stale_ids)

    def delete_artifact(self, file_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM artifacts WHERE file_id = ?", (file_id,)).fetchone()
            if row is None:
                return None
            artifact = self._artifact_row(row)
            db.execute("DELETE FROM artifacts WHERE file_id = ?", (file_id,))
        return artifact

    def delete_artifacts(self, file_ids: list[str]) -> int:
        safe_ids = [file_id for file_id in file_ids if file_id]
        if not safe_ids:
            return 0
        placeholders = ",".join("?" for _ in safe_ids)
        with self._connect() as db:
            cursor = db.execute(f"DELETE FROM artifacts WHERE file_id IN ({placeholders})", safe_ids)
            return cursor.rowcount

    def record_account_capture(
        self,
        *,
        account: str,
        capture_path: Path,
        inspection: dict[str, Any],
    ) -> None:
        detected = inspection.get("detected") if isinstance(inspection.get("detected"), dict) else {}
        with self._connect() as db:
            db.execute(
                """
                INSERT OR REPLACE INTO account_captures (
                    account, capture_path, plan_type, email_masked,
                    capabilities_json, checks_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account,
                    str(capture_path),
                    detected.get("plan_type"),
                    detected.get("email"),
                    json.dumps(inspection.get("capabilities") or {}, ensure_ascii=False, sort_keys=True),
                    json.dumps(inspection.get("checks") or [], ensure_ascii=False, sort_keys=True),
                    utc_now(),
                ),
            )

    def list_account_captures(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM account_captures ORDER BY account ASC").fetchall()
        return [self._account_row(row) for row in rows]

    def delete_account_capture(self, account: str) -> bool:
        with self._connect() as db:
            cursor = db.execute("DELETE FROM account_captures WHERE account = ?", (account,))
            return cursor.rowcount > 0

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._connect() as db:
            row = db.execute("SELECT value_json FROM settings WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value_json"])
        except json.JSONDecodeError:
            return default

    def set_setting(self, key: str, value: Any) -> None:
        with self._connect() as db:
            db.execute(
                """
                INSERT OR REPLACE INTO settings (key, value_json, updated_at)
                VALUES (?, ?, ?)
                """,
                (key, json.dumps(value, ensure_ascii=False, sort_keys=True), utc_now()),
            )

    def delete_setting(self, key: str) -> bool:
        with self._connect() as db:
            cursor = db.execute("DELETE FROM settings WHERE key = ?", (key,))
            return cursor.rowcount > 0

    def _artifact_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "file_id": row["file_id"],
            "kind": row["kind"],
            "filename": row["filename"],
            "path": row["path"],
            "download_url": row["download_url"],
            "content_type": row["content_type"],
            "bytes": row["bytes"],
            "account": row["account"],
            "prompt": row["prompt"],
            "metadata": _json_or_empty(row["metadata_json"]),
            "created_at": row["created_at"],
            "exists": Path(row["path"]).is_file(),
        }

    def _account_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "account": row["account"],
            "capture_path": row["capture_path"],
            "plan_type": row["plan_type"],
            "email": row["email_masked"],
            "capabilities": _json_or_empty(row["capabilities_json"]),
            "checks": _json_or_empty(row["checks_json"]),
            "updated_at": row["updated_at"],
        }


def _json_or_empty(value: str | None) -> Any:
    if not value:
        return {}
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return {}
