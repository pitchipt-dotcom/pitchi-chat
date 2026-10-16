import sqlite3
import json
from contextlib import contextmanager
from pathlib import Path

from backend.app.models.base import Message
from backend.app.models.response import history_final, split_response


class HistoryStore:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id),
                    role TEXT NOT NULL CHECK(role IN ('user','assistant')),
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                );
                CREATE TABLE IF NOT EXISTS chat_turn_metrics (
                    request_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id),
                    metadata TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS conversation_ratings (
                    id INTEGER PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id),
                    category TEXT NOT NULL, score INTEGER NOT NULL, note TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS messages_conversation ON messages(conversation_id, id);
            """)

    def is_deleted(self, ident):
        with self.connection() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='deletion_tombstones'").fetchone():
                return False
            return bool(db.execute("SELECT 1 FROM deletion_tombstones WHERE id=?", (str(ident),)).fetchone())

    def list_conversations(self):
        with self.connection() as db:
            return [dict(row) for row in db.execute(
                "SELECT id, title, created_at FROM conversations ORDER BY created_at DESC, id DESC") if not self.is_deleted(row["id"])]

    def history(self, conversation_id: str):
        if self.is_deleted(conversation_id):
            return None
        with self.connection() as db:
            if not db.execute("SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)).fetchone():
                return None
            rows = [dict(row) for row in db.execute(
                "SELECT role, content, created_at FROM messages WHERE conversation_id = ? ORDER BY id",
                (conversation_id,))]
            for row in rows:
                if row["role"] == "assistant":
                    row["content"] = history_final(row["content"])
            return rows

    def save_turn(self, conversation_id: str, user: str, assistant: str, *, request_id=None, metadata=None):
        # Both messages are committed together, only after a successful model response.
        assistant = split_response(assistant).final_content
        with self.connection() as db:
            db.execute("INSERT OR IGNORE INTO conversations (id, title) VALUES (?, ?)",
                       (conversation_id, user[:60]))
            db.executemany("INSERT INTO messages (conversation_id, role, content) VALUES (?, ?, ?)",
                           [(conversation_id, "user", user), (conversation_id, "assistant", assistant)])

            if metadata is not None and request_id is not None:
                db.execute("INSERT INTO chat_turn_metrics(request_id, conversation_id, metadata) VALUES(?,?,?)",
                           (str(request_id), conversation_id, json.dumps(metadata, ensure_ascii=False)))

    def evaluation(self, conversation_id):
        with self.connection() as db:
            metrics = [dict(row) for row in db.execute(
                "SELECT request_id, metadata, created_at FROM chat_turn_metrics WHERE conversation_id=? ORDER BY rowid DESC LIMIT 100", (conversation_id,))]
            for row in metrics:
                row["metadata"] = json.loads(row["metadata"])
            ratings = [dict(row) for row in db.execute(
                "SELECT category, score, note, created_at FROM conversation_ratings WHERE conversation_id=? ORDER BY id DESC LIMIT 100", (conversation_id,))]
        return {"metrics": metrics, "ratings": ratings}

    def rate(self, conversation_id, category, score, note):
        with self.connection() as db:
            db.execute("INSERT INTO conversation_ratings(conversation_id, category, score, note) VALUES(?,?,?,?)",
                       (conversation_id, category, score, note))
