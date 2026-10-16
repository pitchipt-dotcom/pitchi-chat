"""Check publication candidates without printing sensitive content. Not a PII detector."""

import argparse
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tomllib

MAX_BYTES = 5 * 1024 * 1024
PRIVATE_DIRS = {".venv", "venv", ".aws", ".ssh", "data", "logs", "tmp", "private", "datasets", "credentials", "secrets", "weights"}
PRIVATE_SUFFIXES = {".gguf", ".ggml", ".safetensors", ".ckpt", ".pt", ".pth", ".onnx", ".bin",
                    ".db", ".sqlite", ".sqlite3", ".log", ".jsonl", ".pem", ".key", ".p12", ".pfx",
                    ".zip", ".tar", ".gz", ".7z", ".wav", ".mp3", ".flac", ".ogg", ".mp4", ".webm", ".gif"}
SCREENSHOT_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
PRIVATE_TEXT = (
    re.compile(rb"/home/[a-zA-Z0-9_.-]+/"),
    re.compile(rb"\b[a-zA-Z0-9-]+\.[a-zA-Z0-9.-]+\.ts\.net\b"),
    re.compile(rb"\b(?!100\.64\.0\.0/10\b)100\.(?:6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]{1,3}\.[0-9]{1,3}\b"),
)


def path_issue(name: str) -> str | None:
    path = PurePosixPath(name)
    if any(part in PRIVATE_DIRS for part in path.parts) or path.parts[:1] == ("models",):
        return "private/runtime directory"
    if (path.name == ".env" or (path.name.startswith(".env.") and path.name != ".env.example")
            or path.name.endswith((".local.toml", ".local.env", ".host.lock")) or path.name.startswith(("id_rsa", "id_ed25519"))
            or path.name in {"credentials.json", "token.json", "tokens.json"}):
        return "private configuration or credential filename"
    if path.suffix.lower() in PRIVATE_SUFFIXES or re.search(r"\.(?:db|sqlite3?)-(?:wal|shm|journal)$", name):
        return "runtime/weight/generated artifact"
    if path.suffix.lower() in SCREENSHOT_SUFFIXES and path.parts[:2] not in {("docs", "screenshots"), ("frontend", "icons")}:
        return "unreviewed image outside documentation screenshots or app icons"
    return None


def content_issue(name: str, data: bytes) -> str | None:
    if len(data) > MAX_BYTES:
        return "file exceeds 5 MiB publication limit"
    if PurePosixPath(name).suffix.lower() in SCREENSHOT_SUFFIXES:
        return None  # Explicit screenshot location; still requires human privacy review.
    if b"\0" in data:
        return "unexpected binary file"
    if any(pattern.search(data) for pattern in PRIVATE_TEXT):
        return "personal path or Tailscale hostname/IP"
    if name in {"config/settings.toml", "config/services.toml", "config/resources.toml"}:
        try:
            config = tomllib.loads(data.decode("utf-8"))
            if name == "config/resources.toml" and (config.get("profiles") or config.get("gpu", {}).get("inspect", False)):
                return "public resource template must contain no live profiles"
            if name == "config/settings.toml" and config["model"]["endpoint"] != "":
                return "public endpoint template must remain empty"
            if any(model.get("endpoint", "") != "" for model in config.get("models", {}).values()):
                return "public registry endpoint templates must remain empty"
            if any(service.get("ui_url", "") or service.get("api_base_url", "") or service.get("enabled", False)
                   for service in config.get("services", {}).values()):
                return "public service locations must remain empty and disabled"
        except (UnicodeDecodeError, ValueError, KeyError, TypeError, AttributeError):
            return "invalid public configuration template"
    return None


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args], stderr=subprocess.DEVNULL)


def candidates(worktree: bool):
    if worktree:
        names = sorted(set(git("ls-files", "--cached", "--others", "--exclude-standard", "-z").split(b"\0")) - {b""})
        for raw in names:
            name = raw.decode("utf-8")
            path = Path(name)
            if not path.exists() and not path.is_symlink():
                continue
            yield name, path.is_symlink(), path.stat().st_size if not path.is_symlink() else 0, path
    else:
        for record in git("ls-files", "--stage", "-z").split(b"\0"):
            if not record:
                continue
            metadata, raw = record.split(b"\t", 1)
            mode, object_id, stage = metadata.decode("ascii").split()
            name = raw.decode("utf-8")
            if stage != "0":
                yield name, True, 0, None  # Unmerged files must not be published.
            else:
                yield name, mode != "100644" and mode != "100755", int(git("cat-file", "-s", object_id)), object_id


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worktree", action="store_true", help="check tracked + nonignored working files instead of the index")
    args = parser.parse_args()
    count = 0
    findings = []
    try:
        for name, unsafe_mode, size, source in candidates(args.worktree):
            count += 1
            issue = path_issue(name)
            if issue is None and unsafe_mode:
                issue = "symlink, submodule or unresolved index entry"
            if issue is None and size > MAX_BYTES:
                issue = "file exceeds 5 MiB publication limit"
            if issue is None:
                data = source.read_bytes() if args.worktree else git("cat-file", "blob", source)
                issue = content_issue(name, data)
            if issue:
                findings.append((name, issue))
    except (OSError, subprocess.CalledProcessError, UnicodeError):
        print("Publication check could not complete; do not commit.", file=sys.stderr)
        return 2
    for name, issue in findings:
        print(f"BLOCK: {name!r}: {issue}", file=sys.stderr)
    if findings:
        return 1
    print(f"Publication structure check passed: {count} files. Run redacted Gitleaks and review private data separately.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
