"""Conversation-owned, decoded local images. No URLs or client paths are trusted."""
import hashlib
import io
import json
import os
import warnings
from pathlib import Path
from uuid import UUID, uuid4

from PIL import Image, ImageOps, UnidentifiedImageError
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import Field

from backend.app.harness.contracts import Contract

MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_PIXELS = 16 * 1024 * 1024
TEXT_ONLY_NOTICE = ("この会話には画像が添付されていますが、あなたには画像データを一切渡していません。"
    "画像を見る・認識することはできません。画像について聞かれた場合は画像理解モデルが未接続であると説明し、"
    "画像の内容を推測して説明しないでください。テキストについての会話は通常どおり行ってください。")


class ImageReference(Contract):
    id: UUID = Field(strict=False)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    width: int = Field(ge=16, le=1024, multiple_of=16, strict=True)
    height: int = Field(ge=16, le=1024, multiple_of=16, strict=True)

    @property
    def name(self):
        return f"pitch_chat_{self.id.hex}_{self.sha256}.png"


def decoded_image(data):
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("image_size_limit")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if (image.format not in {"PNG", "JPEG", "WEBP"} or getattr(image, "n_frames", 1) != 1
                        or image.width * image.height > MAX_PIXELS or min(image.size) < 16
                        or max(image.size) > 4096):
                    raise ValueError("image_dimensions_or_format")
                image.load()
                mode = "RGBA" if "A" in image.getbands() or "transparency" in image.info else "RGB"
                clean = ImageOps.exif_transpose(image).convert(mode)
                # A fresh image drops EXIF, prompts, GPS, comments and other metadata.
                return Image.frombytes(mode, clean.size, clean.tobytes())
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError,
            Image.DecompressionBombWarning):
        raise ValueError("image_decode_failed") from None


def encoded(image):
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


class ImageAssetStore:
    def __init__(self, history, root: Path):
        self.history, self.root = history, root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with history.connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS image_assets (
                id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id),
                parent_id TEXT REFERENCES image_assets(id), source_kind TEXT NOT NULL,
                source_job_id TEXT UNIQUE, sha256 TEXT NOT NULL, width INTEGER NOT NULL,
                height INTEGER NOT NULL, original_width INTEGER NOT NULL, original_height INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))
            """)
            db.execute("CREATE INDEX IF NOT EXISTS image_assets_conversation ON image_assets(conversation_id,created_at)")
            db.execute("""CREATE TABLE IF NOT EXISTS image_observations (
                job_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id),
                image_id TEXT NOT NULL REFERENCES image_assets(id), image_sha256 TEXT NOT NULL,
                question TEXT NOT NULL, answer TEXT NOT NULL, model TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")

    def create_conversation(self):
        cid = str(uuid4())
        with self.history.connection() as db:
            db.execute("INSERT INTO conversations(id,title) VALUES(?,?)", (cid, "画像の会話"))
        return cid

    def text_only_notice(self, conversation_id):
        with self.history.connection() as db:
            exists = db.execute("SELECT 1 FROM image_assets WHERE conversation_id=? LIMIT 1", (str(conversation_id),)).fetchone()
        return TEXT_ONLY_NOTICE if exists else None

    def get(self, image_id, conversation_id=None):
        image_id = str(UUID(str(image_id)))
        if self.history.is_deleted(image_id):
            raise ValueError("image_deleted")
        with self.history.connection() as db:
            row = db.execute("SELECT * FROM image_assets WHERE id=?", (image_id,)).fetchone()
        if row is None or (conversation_id is not None and row["conversation_id"] != str(conversation_id)):
            raise ValueError("image_not_in_conversation")
        return dict(row) | {"url": f"/api/images/attachments/{image_id}/content"}

    def reference(self, image_id, conversation_id):
        row = self.get(image_id, conversation_id)
        return ImageReference(id=UUID(row["id"]), sha256=row["sha256"], width=row["width"], height=row["height"])

    def path(self, image_id, *, reference=False):
        key = UUID(str(image_id)).hex
        path = self.root / (key + (".input.png" if reference else ".png"))
        if path.is_symlink() or not path.is_file():
            raise ValueError("image_file_missing")
        return path

    def reference_bytes(self, reference):
        ref = ImageReference.model_validate(reference.model_dump(mode="json"), strict=False)
        row = self.get(ref.id)
        if (row["sha256"], row["width"], row["height"]) != (ref.sha256, ref.width, ref.height):
            raise ValueError("image_integrity_failed")
        data = self.path(ref.id, reference=True).read_bytes()
        if len(data) > MAX_IMAGE_BYTES or hashlib.sha256(data).hexdigest() != ref.sha256:
            raise ValueError("image_integrity_failed")
        return data

    def save(self, data, conversation_id, *, parent_id=None, source_job_id=None, source_kind=None):
        if source_kind not in {None, 'video_frame'}:
            raise ValueError('invalid_image_source')
        cid = str(UUID(str(conversation_id)))
        if self.history.is_deleted(cid):
            raise ValueError("conversation_deleted")
        if parent_id is not None:
            parent_id = self.get(parent_id, cid)["id"]
        with self.history.connection() as db:
            if not db.execute("SELECT 1 FROM conversations WHERE id=?", (cid,)).fetchone():
                raise ValueError("conversation_missing")
            if source_job_id:
                old = db.execute("SELECT id FROM image_assets WHERE source_job_id=?", (source_job_id,)).fetchone()
                if old:
                    return self.get(old[0], cid)
            if db.execute("SELECT count(*) FROM image_assets WHERE conversation_id=?", (cid,)).fetchone()[0] >= 100:
                raise ValueError("conversation_image_limit")
        image = decoded_image(data)
        original = encoded(image)
        width, height = image.size
        if image.mode == "RGBA":
            background = Image.new("RGB", image.size, "white")
            background.paste(image, mask=image.getchannel("A"))
            image = background
        scale = min(1, 1024 / width, 1024 / height)
        size = tuple(max(16, round(v * scale / 16) * 16) for v in image.size)
        reference_data = encoded(image.resize(size, Image.Resampling.LANCZOS))
        ident = uuid4()
        paths = [self.root / (ident.hex + suffix) for suffix in (".png", ".input.png")]
        try:
            for path, payload in zip(paths, (original, reference_data)):
                with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as file:
                    file.write(payload)
            with self.history.connection() as db:
                db.execute("""INSERT INTO image_assets(id,conversation_id,parent_id,source_kind,source_job_id,
                    sha256,width,height,original_width,original_height) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (str(ident), cid, parent_id, source_kind or ("generated" if source_job_id else "upload"), source_job_id,
                     hashlib.sha256(reference_data).hexdigest(), *size, width, height))
        except Exception:
            for path in paths:
                path.unlink(missing_ok=True)
            raise
        return self.get(ident)

    def timeline(self, cid):
        with self.history.connection() as db:
            images = [self.get(r[0], cid) for r in db.execute(
                "SELECT id FROM image_assets WHERE conversation_id=? ORDER BY created_at,id", (cid,)) if not self.history.is_deleted(r[0])]
            rows = db.execute("""SELECT job_id,status,error,image_request_json FROM tool_jobs
                WHERE job_kind='media' AND json_extract(image_request_json,'$.conversation_id')=?
                ORDER BY created_at,job_id""", (cid,)).fetchall()
        return {"images": images, "jobs": [{"job_id": r["job_id"], "status": r["status"], "error": r["error"],
                "request": json.loads(r["image_request_json"])} for r in rows if not self.history.is_deleted(r["job_id"])]}


def same_origin(request):
    if request.headers.get("origin") not in {None, str(request.base_url).rstrip("/")}:
        raise HTTPException(403, "同じ画面から操作してください。")


def router():
    routes = APIRouter()

    @routes.post("/api/images/conversations", status_code=201)
    async def conversation(request: Request):
        same_origin(request)
        return {"conversation_id": request.app.state.image_assets.create_conversation()}

    @routes.post("/api/images/attachments", status_code=201)
    async def upload(request: Request, conversation_id: UUID):
        same_origin(request)
        if request.headers.get("content-type", "").split(";")[0] not in {"image/png", "image/jpeg", "image/webp"}:
            raise HTTPException(415, "PNG・JPEG・WebPを選んでください。")
        import asyncio
        try:
            async with request.app.state.image_upload_lock:
                data = bytearray()
                async with asyncio.timeout(60):
                    async for chunk in request.stream():
                        data.extend(chunk)
                        if len(data) > MAX_IMAGE_BYTES:
                            raise HTTPException(413, "画像は20MiBまでです。")
                return await asyncio.to_thread(request.app.state.image_assets.save, bytes(data), str(conversation_id))
        except TimeoutError:
            raise HTTPException(408, "画像の転送がタイムアウトしました。") from None
        except ValueError:
            raise HTTPException(422, "画像を読み取れません。静止画16〜4096px、16MP以下を選んでください。") from None

    @routes.get("/api/images/attachments/{image_id}/content")
    async def content(image_id: UUID, request: Request):
        try:
            request.app.state.image_assets.get(image_id)
            path = request.app.state.image_assets.path(image_id)
        except ValueError:
            raise HTTPException(404, "画像がありません。") from None
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @routes.get("/api/conversations/{conversation_id}/images")
    async def images(conversation_id: UUID, request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return request.app.state.image_assets.timeline(str(conversation_id))

    @routes.get("/api/images/vision")
    async def vision(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        definition = request.app.state.harness.registry.get('image.understand') if hasattr(request.app.state, 'harness') else None
        available = bool(definition and definition.enabled)
        return {"available": available, "reason": None if available else "vision_model_not_configured", "strata_images": False,
                "backend": next(iter(definition.allowed_backends)) if available else None,
                "model": 'Qwen3.5-9B-Uncensored' if available else None,
                "message": "画像理解はQwen3.5-9B-Uncensoredが担当します。Strataは編集指示の文章だけを扱います。" if available else
                    "画像理解モデルは未接続です。Strataは画像を見ず、編集指示の文章だけを最適化します。"}

    return routes
