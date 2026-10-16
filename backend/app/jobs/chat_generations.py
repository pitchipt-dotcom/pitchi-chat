"""Durable, idempotent Strata chat generations independent of HTTP subscribers."""

import json
import asyncio
import hashlib
from backend.app.models.response import split_response
from uuid import uuid4


class ChatGenerationStore:
    def __init__(self, history_store):
        self.store = history_store
        with self.store.connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS chat_generation_jobs (
                job_id TEXT PRIMARY KEY, client_request_id TEXT NOT NULL UNIQUE,
                conversation_id TEXT NOT NULL, request_json TEXT NOT NULL,
                request_fingerprint TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL, reply TEXT NOT NULL DEFAULT '', revision INTEGER NOT NULL DEFAULT 0,
                answer_json TEXT, error TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(chat_generation_jobs)")}
            if "request_fingerprint" not in columns:
                db.execute("ALTER TABLE chat_generation_jobs ADD COLUMN request_fingerprint TEXT NOT NULL DEFAULT ''")
            db.execute("DELETE FROM chat_generation_jobs WHERE status IN ('COMPLETED','ERROR','CANCELLED','INTERRUPTED') AND created_at < datetime('now','-30 days')")
            db.execute("""UPDATE chat_generation_jobs SET status='INTERRUPTED',
                error='host_restarted', request_json='{}', updated_at=CURRENT_TIMESTAMP
                WHERE status IN ('QUEUED','RUNNING','CANCELLING')""")

    def create(self, client_request_id, conversation_id, request):
        encoded = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        fingerprint = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        job_id = str(uuid4())
        with self.store.connection() as db:
            existing = db.execute("SELECT * FROM chat_generation_jobs WHERE client_request_id=?", (client_request_id,)).fetchone()
            if existing:
                row = dict(existing)
                # The idempotency key may only resume its original immutable submission.
                prior = json.loads(row["request_json"] or "{}")
                if row.get("request_fingerprint") and row["request_fingerprint"] != fingerprint:
                    raise ValueError("idempotency_conflict")
                if not row.get("request_fingerprint") and row["status"] in {"QUEUED", "RUNNING", "CANCELLING"} and prior != request:
                    raise ValueError("idempotency_conflict")
                return row, False
            active = db.execute("SELECT count(*) FROM chat_generation_jobs WHERE status IN ('QUEUED','RUNNING','CANCELLING')").fetchone()[0]
            same_conversation = db.execute("SELECT 1 FROM chat_generation_jobs WHERE conversation_id=? AND status IN ('QUEUED','RUNNING','CANCELLING') LIMIT 1", (conversation_id,)).fetchone()
            if active >= 32 or same_conversation:
                raise ValueError("generation_busy")
            db.execute("""INSERT INTO chat_generation_jobs(job_id,client_request_id,conversation_id,request_json,request_fingerprint,status)
                VALUES(?,?,?,?,?,'QUEUED')""", (job_id, client_request_id, conversation_id, encoded, fingerprint))
            return dict(db.execute("SELECT * FROM chat_generation_jobs WHERE job_id=?", (job_id,)).fetchone()), True

    def get(self, job_id):
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM chat_generation_jobs WHERE job_id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def by_client_request(self, client_request_id):
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM chat_generation_jobs WHERE client_request_id=?", (client_request_id,)).fetchone()
        return dict(row) if row else None

    def start(self, job_id):
        with self.store.connection() as db:
            result = db.execute("UPDATE chat_generation_jobs SET status='RUNNING',updated_at=CURRENT_TIMESTAMP WHERE job_id=? AND status='QUEUED'", (job_id,))
            return result.rowcount == 1

    def append(self, job_id, text):
        with self.store.connection() as db:
            result = db.execute("""UPDATE chat_generation_jobs SET reply=reply||?,revision=revision+1,
                updated_at=CURRENT_TIMESTAMP WHERE job_id=? AND status IN ('RUNNING','CANCELLING')""", (text, job_id))
            if result.rowcount != 1:
                raise asyncio.CancelledError

    def complete_turn(self, job_id, conversation_id, user, assistant, request_id, metadata, answer):
        assistant = split_response(assistant).final_content
        with self.store.connection() as db:
            row = db.execute("SELECT status FROM chat_generation_jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None or row["status"] != "RUNNING":
                raise asyncio.CancelledError
            db.execute("INSERT OR IGNORE INTO conversations(id,title) VALUES(?,?)", (conversation_id, user[:60]))
            db.executemany("INSERT INTO messages(conversation_id,role,content) VALUES(?,?,?)",
                [(conversation_id, "user", user), (conversation_id, "assistant", assistant)])
            if metadata is not None:
                db.execute("INSERT INTO chat_turn_metrics(request_id,conversation_id,metadata) VALUES(?,?,?)",
                    (str(request_id), conversation_id, json.dumps(metadata, ensure_ascii=False)))
            db.execute("""UPDATE chat_generation_jobs SET status='COMPLETED',request_json='{}',reply=?,
                revision=revision+1,answer_json=?,updated_at=CURRENT_TIMESTAMP WHERE job_id=? AND status='RUNNING'""",
                (assistant, json.dumps(answer, ensure_ascii=False), job_id))

    def fail(self, job_id, error):
        with self.store.connection() as db:
            db.execute("""UPDATE chat_generation_jobs SET status='ERROR',request_json='{}',error=?,
                updated_at=CURRENT_TIMESTAMP WHERE job_id=? AND status IN ('RUNNING','QUEUED')""", (error[:160], job_id))

    def request_cancel(self, job_id):
        with self.store.connection() as db:
            result = db.execute("UPDATE chat_generation_jobs SET status='CANCELLING',updated_at=CURRENT_TIMESTAMP WHERE job_id=? AND status IN ('QUEUED','RUNNING')", (job_id,))
            return result.rowcount == 1

    def cancelled(self, job_id):
        with self.store.connection() as db:
            db.execute("UPDATE chat_generation_jobs SET status='CANCELLED',request_json='{}',error=NULL,updated_at=CURRENT_TIMESTAMP WHERE job_id=? AND status='CANCELLING'", (job_id,))

    def interrupted(self, job_id):
        with self.store.connection() as db:
            db.execute("""UPDATE chat_generation_jobs SET status='INTERRUPTED',request_json='{}',
                error='host_shutdown',updated_at=CURRENT_TIMESTAMP WHERE job_id=? AND status IN ('QUEUED','RUNNING','CANCELLING')""", (job_id,))

    def public(self, row):
        answer = json.loads(row["answer_json"]) if row.get("answer_json") else None
        request = json.loads(row["request_json"] or "{}")
        return {"job_id": row["job_id"], "client_request_id": row["client_request_id"],
                "conversation_id": row["conversation_id"], "status": row["status"],
                "reply": row["reply"], "revision": row["revision"], "answer": answer,
                "message": request.get("message") if request else None,
                "model": request.get("model") or (answer or {}).get("model_selection", {}).get("actual_model_used"), "error": row.get("error")}
