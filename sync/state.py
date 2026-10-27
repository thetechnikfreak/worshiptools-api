import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

class SyncState:
    def __init__(self, db_path: str = "/data/sync_state.sqlite"):
        self.db_path = db_path
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._get_conn() as conn:
            conn.execute("""
            CREATE TABLE IF NOT EXISTS links (
                domain TEXT NOT NULL,
                ct_id TEXT NOT NULL,
                wt_id TEXT NOT NULL,
                ct_hash TEXT,
                wt_hash TEXT,
                ct_mod TEXT,
                wt_mod TEXT,
                missing_count INTEGER DEFAULT 0,
                last_synced TEXT,
                deleted_at TEXT,
                PRIMARY KEY (domain, ct_id, wt_id)
            );
            """)
            conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_links_lookup 
            ON links (domain, ct_id, wt_id);
            """)
            conn.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT,
                finished_at TEXT,
                summary_json TEXT
            );
            """)
            conn.execute("""
            CREATE TABLE IF NOT EXISTS seen (
                namespace TEXT NOT NULL,
                key TEXT NOT NULL,
                seen_at TEXT,
                PRIMARY KEY (namespace, key)
            );
            """)
            conn.commit()

    def migrate_from_json(self, json_path: str):
        """Migrates legacy /data/sync_state.json if it exists and links table is empty."""
        p = Path(json_path)
        if not p.exists():
            return

        with self._get_conn() as conn:
            cur = conn.execute("SELECT COUNT(*) as cnt FROM links")
            if cur.fetchone()["cnt"] > 0:
                return

            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                events = data.get("events", {})
                for ct_id, details in events.items():
                    wt_id = details.get("wt_id")
                    if not wt_id:
                        continue
                    now = datetime.now(timezone.utc).isoformat()
                    conn.execute("""
                    INSERT OR IGNORE INTO links 
                    (domain, ct_id, wt_id, ct_hash, wt_hash, ct_mod, wt_mod, missing_count, last_synced, deleted_at)
                    VALUES ('service', ?, ?, '', '', '', '', 0, ?, NULL)
                    """, (str(ct_id), str(wt_id), now))
                conn.commit()
                logger.info(f"Successfully migrated {len(events)} events from {json_path} to SQLite state.")
            except Exception as e:
                logger.error(f"Error migrating {json_path}: {e}")

    def get_links(self, domain: str, active_only: bool = True) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            if active_only:
                cur = conn.execute("SELECT * FROM links WHERE domain = ? AND deleted_at IS NULL", (domain,))
            else:
                cur = conn.execute("SELECT * FROM links WHERE domain = ?", (domain,))
            return [dict(row) for row in cur.fetchall()]

    def get_link_by_ct(self, domain: str, ct_id: str) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            cur = conn.execute("SELECT * FROM links WHERE domain = ? AND ct_id = ? AND deleted_at IS NULL", (domain, str(ct_id)))
            row = cur.fetchone()
            return dict(row) if row else None

    def get_link_by_wt(self, domain: str, wt_id: str) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            cur = conn.execute("SELECT * FROM links WHERE domain = ? AND wt_id = ? AND deleted_at IS NULL", (domain, str(wt_id)))
            row = cur.fetchone()
            return dict(row) if row else None

    def upsert_link(self, domain: str, ct_id: str, wt_id: str, ct_hash: str = "", wt_hash: str = "",
                    ct_mod: str = "", wt_mod: str = "", missing_count: int = 0):
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute("""
            INSERT INTO links (domain, ct_id, wt_id, ct_hash, wt_hash, ct_mod, wt_mod, missing_count, last_synced, deleted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(domain, ct_id, wt_id) DO UPDATE SET
                ct_hash = excluded.ct_hash,
                wt_hash = excluded.wt_hash,
                ct_mod = excluded.ct_mod,
                wt_mod = excluded.wt_mod,
                missing_count = excluded.missing_count,
                last_synced = excluded.last_synced,
                deleted_at = NULL
            """, (domain, str(ct_id), str(wt_id), ct_hash, wt_hash, ct_mod, wt_mod, missing_count, now))
            conn.commit()

    def mark_missing(self, domain: str, ct_id: str, wt_id: str) -> int:
        with self._get_conn() as conn:
            cur = conn.execute("""
            UPDATE links SET missing_count = missing_count + 1 
            WHERE domain = ? AND ct_id = ? AND wt_id = ?
            RETURNING missing_count
            """, (domain, str(ct_id), str(wt_id)))
            row = cur.fetchone()
            conn.commit()
            return row["missing_count"] if row else 1

    def mark_deleted(self, domain: str, ct_id: str, wt_id: str):
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute("""
            UPDATE links SET deleted_at = ?
            WHERE domain = ? AND ct_id = ? AND wt_id = ?
            """, (now, domain, str(ct_id), str(wt_id)))
            conn.commit()

    def record_run(self, started_at: str, finished_at: str, summary: Dict[str, Any]):
        with self._get_conn() as conn:
            conn.execute("""
            INSERT INTO runs (started_at, finished_at, summary_json)
            VALUES (?, ?, ?)
            """, (started_at, finished_at, json.dumps(summary, ensure_ascii=False)))
            conn.commit()

    def is_seen(self, namespace: str, key: str) -> bool:
        with self._get_conn() as conn:
            cur = conn.execute("SELECT 1 FROM seen WHERE namespace = ? AND key = ?", (namespace, str(key)))
            return cur.fetchone() is not None

    def mark_seen(self, namespace: str, key: str):
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute("INSERT OR REPLACE INTO seen (namespace, key, seen_at) VALUES (?, ?, ?)", (namespace, str(key), now))
            conn.commit()

    def get_all_seen(self, namespace: str) -> List[str]:
        with self._get_conn() as conn:
            cur = conn.execute("SELECT key FROM seen WHERE namespace = ?", (namespace,))
            return [row["key"] for row in cur.fetchall()]

    def delete_seen(self, namespace: Optional[str] = None, key_pattern: Optional[str] = None) -> int:
        """Deletes seen records matching namespace and/or key pattern (supports LIKE %pattern%)."""
        with self._get_conn() as conn:
            query = "DELETE FROM seen WHERE 1=1"
            params = []
            if namespace:
                query += " AND namespace = ?"
                params.append(namespace)
            if key_pattern:
                if "%" in key_pattern:
                    query += " AND key LIKE ?"
                else:
                    query += " AND key = ?"
                params.append(key_pattern)
            cur = conn.execute(query, tuple(params))
            conn.commit()
            return cur.rowcount

    def delete_link_permanently(self, domain: str, ct_id: Optional[str] = None, wt_id: Optional[str] = None) -> int:
        """Completely removes a link from the links table."""
        with self._get_conn() as conn:
            query = "DELETE FROM links WHERE domain = ?"
            params = [domain]
            if ct_id:
                query += " AND ct_id = ?"
                params.append(str(ct_id))
            if wt_id:
                query += " AND wt_id = ?"
                params.append(str(wt_id))
            cur = conn.execute(query, tuple(params))
            conn.commit()
            return cur.rowcount
