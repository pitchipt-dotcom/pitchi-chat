"""Explicit, reviewed external-agent bindings. Real locations live in ignored TOML."""

import hashlib
import os
from pathlib import Path
import tomllib
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from backend.app.config import ROOT
from backend.app.harness.contracts import Contract


def source_digest(root: Path):
    digest = hashlib.sha256()
    files = sorted((root / "src/pitchi_work").glob("*.py"))
    if not files or len(files) > 100:
        raise ValueError("External source is unavailable")
    for path in files:
        if path.is_symlink() or path.stat().st_size > 1024 * 1024:
            raise ValueError("Unsafe external source")
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


class WorkSettings(Contract):
    enabled: bool = False
    source_root: str = ""
    python_executable: str = ""
    reviewed_source_sha256: str = Field(default="", pattern=r"^(?:[a-f0-9]{64})?$")
    model_profile: str = Field(default="", pattern=r"^(?:[a-z][a-z0-9_.-]{0,79})?$")
    model_endpoint: str = ""
    model_name: str = Field(default="", max_length=120)
    timeout_seconds: float = Field(default=600, gt=0, le=600, allow_inf_nan=False)
    # Logical project names, never request-supplied filesystem paths.
    projects: dict[str, str] = {}

    @model_validator(mode="after")
    def reviewed_config(self):
        import re
        if any(not re.fullmatch(r"[a-z][a-z0-9_.-]{0,79}", n) or not Path(p).is_absolute() for n, p in self.projects.items()):
            raise ValueError("Project registry requires logical IDs and absolute HOST paths")
        if self.source_root and not Path(self.source_root).is_absolute():
            raise ValueError("External source root must be absolute")
        if self.python_executable and not Path(self.python_executable).is_absolute():
            raise ValueError("Python executable must be absolute")
        if self.model_endpoint:
            p = urlsplit(self.model_endpoint)
            if (p.scheme != "http" or p.hostname not in {"localhost", "127.0.0.1", "::1"}
                    or p.username or p.password or p.query or p.fragment or p.path.rstrip("/") != "/v1"):
                raise ValueError("Work model endpoint requires an explicit owned loopback /v1 origin")
        if self.enabled and not all((self.source_root, self.python_executable, self.reviewed_source_sha256,
                                     self.model_profile, self.model_endpoint, self.model_name, self.projects)):
            raise ValueError("Work enable requires reviewed source and owned model/project bindings")
        return self


def load_work(path: Path | None = None):
    path = path or Path(os.environ.get("PITCH_CHAT_WORK_CONFIG", ROOT / "config/work.toml"))
    with path.open("rb") as file:
        return WorkSettings.model_validate(tomllib.load(file))
