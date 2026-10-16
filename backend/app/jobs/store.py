"""Private SQLite job state. Restart recovery never replays uncertain submissions."""

from uuid import uuid4

from backend.app.storage.sqlite import HistoryStore


class JobStore:
    def __init__(self, store: HistoryStore):
        self.store = store
        with store.connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS tool_jobs (
                job_id TEXT PRIMARY KEY, tool TEXT NOT NULL, status TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, started_at TEXT, finished_at TEXT,
                result_reference TEXT, error TEXT, call_json TEXT NOT NULL, result_json TEXT,
                remote_run_id TEXT, remote_phase TEXT, requires_gpu INTEGER NOT NULL
            )""")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(tool_jobs)")}
            for name, declaration in {"job_kind": "TEXT NOT NULL DEFAULT 'tool'", "image_request_json": "TEXT",
                                      "image_spec_json": "TEXT", "image_phase": "TEXT", "media_phase": "TEXT"}.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE tool_jobs ADD COLUMN {name} {declaration}")
            db.execute("""CREATE TABLE IF NOT EXISTS media_events (
                id INTEGER PRIMARY KEY, job_id TEXT NOT NULL, phase TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, details_json TEXT NOT NULL)""")

    def recover(self):
        with self.store.connection() as db:
            media = [row[0] for row in db.execute("SELECT job_id FROM tool_jobs WHERE job_kind='media' AND status IN ('QUEUED','RUNNING')")]
            db.execute("UPDATE tool_jobs SET status='RECOVERY_REQUIRED',error='host_interrupted',finished_at=CURRENT_TIMESTAMP WHERE status IN ('QUEUED','RUNNING')")
        for job_id in media:
            self.media_phase(job_id, "FAILED", {"code": "host_interrupted"})

    def create_media(self, request, plan=None, call=None):
        import json
        from backend.app.harness.contracts import ToolCall
        job_id = str(uuid4())
        call = call or ToolCall(tool="image.understand" if request.kind == 'vision' else "media.video.generate" if request.kind == "video" else "image.generate", backend=request.backend, arguments={})
        with self.store.connection() as db:
            db.execute("""INSERT INTO tool_jobs(job_id,tool,status,call_json,requires_gpu,
                job_kind,image_request_json,image_spec_json) VALUES(?,?,'QUEUED',?,1,'media',?,?)""",
                (job_id, call.tool, call.model_dump_json(), request.model_dump_json(), json.dumps(plan) if plan else None))
        if plan:
            self.media_phase(job_id, "PROMPT_SAVED")
        return job_id

    def create_video_concat(self, project_id, clip_ids):
        import json
        job_id = str(uuid4())
        with self.store.connection() as db:
            db.execute('''INSERT INTO tool_jobs(job_id,tool,status,call_json,requires_gpu,job_kind,image_request_json)
                VALUES(?,'media.video.generate','QUEUED','{}',0,'video_concat',?)''',
                (job_id,json.dumps({'project_id':project_id,'clip_ids':clip_ids})))
        return job_id

    def prepare_media(self, job_id, plan, call):
        import json
        with self.store.connection() as db:
            result = db.execute("""UPDATE tool_jobs SET image_spec_json=?,call_json=?
                WHERE job_id=? AND job_kind='media' AND status='RUNNING' AND image_spec_json IS NULL""",
                (json.dumps(plan), call.model_dump_json(), job_id))
            if result.rowcount != 1:
                raise ValueError("Media plan is not preparing")
        self.media_phase(job_id, "PROMPT_SAVED")

    def media_phase(self, job_id, phase, details=None):
        import json
        phases = ("PROMPT_GENERATING", "PROMPT_SAVED", "STRATA_STOPPING", "RESOURCE_CHECK",
                  "COMFY_STARTING", "GENERATING", "COMPLETED", "FAILED")
        if phase not in phases:
            raise ValueError("Invalid media phase")
        with self.store.connection() as db:
            row = db.execute("SELECT media_phase FROM tool_jobs WHERE job_id=? AND job_kind='media'", (job_id,)).fetchone()
            if row is None:
                raise ValueError("Unknown media job")
            if row[0] == phase:
                return
            if row[0] and phases.index(phase) < phases.index(row[0]):
                raise ValueError("Media phase cannot move backwards")
            db.execute("UPDATE tool_jobs SET media_phase=? WHERE job_id=?", (phase, job_id))
            db.execute("INSERT INTO media_events(job_id,phase,details_json) VALUES(?,?,?)",
                       (job_id, phase, json.dumps(details or {})))

    def media_events(self, job_id):
        import json
        with self.store.connection() as db:
            return [{"phase": r["phase"], "at": r["created_at"], "details": json.loads(r["details_json"])}
                    for r in db.execute("SELECT * FROM media_events WHERE job_id=? ORDER BY id LIMIT 32", (job_id,))]

    def create(self, call, requires_gpu: bool):
        job_id = str(uuid4())
        with self.store.connection() as db:
            db.execute("INSERT INTO tool_jobs(job_id,tool,status,call_json,requires_gpu) VALUES(?,?,'QUEUED',?,?)",
                       (job_id, call.tool, call.model_dump_json(), int(requires_gpu)))
        return job_id

    def transition(self, job_id: str, status: str, *, error: str | None = None, result=None):
        if status not in {"RUNNING", "SUCCESS", "ERROR", "RECOVERY_REQUIRED"}:
            raise ValueError("Invalid HOST job state")
        with self.store.connection() as db:
            row = db.execute("SELECT status,job_kind,image_request_json FROM tool_jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None or (row["status"] != "QUEUED" if status == "RUNNING" else row["status"] != "RUNNING"):
                raise ValueError("Invalid job transition")
            if status == 'SUCCESS' and row['job_kind'] == 'media':
                import json
                request = json.loads(row['image_request_json'])
                if request['kind'] == 'vision':
                    from backend.app.tools.vision import VisionOutput
                    output = VisionOutput.model_validate(result.tool_result.output)
                    cid = request['conversation_id']
                    if output.image_id != request['reference_image_id']:
                        raise ValueError('vision_reference_changed')
                    db.execute('INSERT INTO image_observations(job_id,conversation_id,image_id,image_sha256,question,answer,model) VALUES(?,?,?,?,?,?,?)',
                        (job_id,cid,output.image_id,output.image_sha256,request['prompt'],output.answer,output.model))
                    db.executemany('INSERT INTO messages(conversation_id,role,content) VALUES(?,?,?)',
                        [(cid,'user',request['prompt']),(cid,'assistant',output.answer)])
            db.execute("""UPDATE tool_jobs SET status=?,error=?,
                started_at=CASE WHEN ?='RUNNING' THEN CURRENT_TIMESTAMP ELSE started_at END,
                finished_at=CASE WHEN ?='RUNNING' THEN NULL ELSE CURRENT_TIMESTAMP END,
                result_reference=?,result_json=? WHERE job_id=?""",
                       (status, error, status, status, f"/api/jobs/{job_id}/result" if result else None,
                        result.model_dump_json() if result else None, job_id))

    def create_image(self, request):
        from backend.app.harness.contracts import ToolCall
        job_id = str(uuid4())
        call = ToolCall(tool="image.generate", arguments={})
        with self.store.connection() as db:
            db.execute("""INSERT INTO tool_jobs(job_id,tool,status,call_json,requires_gpu,
                job_kind,image_request_json,image_phase) VALUES(?,?,'QUEUED',?,?,'image',?,'COMPILING')""",
                       (job_id, call.tool, call.model_dump_json(), int(not request.compile_only),
                        request.model_dump_json()))
        return job_id

    def prepare_image(self, job_id, spec, call):
        with self.store.connection() as db:
            update = db.execute("""UPDATE tool_jobs SET image_spec_json=?,call_json=?,image_phase='SPEC_READY'
                WHERE job_id=? AND status='RUNNING' AND job_kind='image' AND image_spec_json IS NULL""",
                (spec.model_dump_json(), call.model_dump_json(), job_id))
            if update.rowcount != 1:
                raise ValueError("Image job is not preparing")

    def create_image_render(self, compiler_job_id, request, spec, call):
        import json
        job_id = str(uuid4())
        lineage = {"compiler_job_id": compiler_job_id, "request": request.model_dump(mode="json")}
        with self.store.connection() as db:
            db.execute("""INSERT INTO tool_jobs(job_id,tool,status,call_json,requires_gpu,
                job_kind,image_request_json,image_spec_json,image_phase)
                VALUES(?,?,'QUEUED',?,1,'image_render',?,?,'SPEC_READY')""",
                (job_id, call.tool, call.model_dump_json(), json.dumps(lineage), spec.model_dump_json()))
        return job_id

    def generating_image(self, job_id):
        with self.store.connection() as db:
            update = db.execute("UPDATE tool_jobs SET image_phase='GENERATING' WHERE job_id=? AND status='RUNNING' AND image_spec_json IS NOT NULL",
                                (job_id,))
            if update.rowcount != 1:
                raise ValueError("Image spec not persisted")

    def remote(self, job_id: str, phase: str, run_id: str | None):
        if phase not in {"SUBMITTING", "SUBMITTED", "TERMINAL"}:
            raise ValueError("Invalid remote phase")
        with self.store.connection() as db:
            db.execute("UPDATE tool_jobs SET remote_phase=?,remote_run_id=COALESCE(?,remote_run_id) WHERE job_id=? AND status='RUNNING'", (phase, run_id, job_id))

    def get(self, job_id: str, *, private=False):
        if self.store.is_deleted(job_id):
            return None
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM tool_jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            return None
        data = dict(row)
        if not private:
            for key in ("call_json", "result_json", "requires_gpu", "image_request_json", "image_spec_json", "job_kind"):
                data.pop(key)
        return data

    def list(self):
        with self.store.connection() as db:
            ids = [row["job_id"] for row in db.execute("SELECT job_id FROM tool_jobs ORDER BY created_at DESC,rowid DESC LIMIT 100")]
        return [row for job_id in ids if (row := self.get(job_id)) is not None]
