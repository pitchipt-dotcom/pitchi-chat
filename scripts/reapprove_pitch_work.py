#!/usr/bin/env python3
"""Explicit Host-admin review and re-pin transaction for Pitchi-work."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from urllib.request import Request, ProxyHandler, HTTPRedirectHandler, build_opener
import tomllib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.app.tools.work_config import source_digest

PIN_KEY = "reviewed_source_sha256"
SERVICE = "pitch-chat-host.service"
HEALTH_URL = "http://127.0.0.1:8000/health"
CATALOG_URL = "http://127.0.0.1:8000/api/work/catalog"
TEST_TIMEOUT = 900

class RejectRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, new_url):
        raise ApprovalError("HOST verification endpoint redirected")


LOCAL_HTTP = build_opener(ProxyHandler({}), RejectRedirectHandler())


class ApprovalError(RuntimeError):
    pass


def run(argv: list[str], *, cwd: Path | None = None, timeout: int = 30, capture=True):
    return subprocess.run(argv, cwd=cwd, text=True, capture_output=capture, timeout=timeout, check=False)


def git(source: Path, *args: str) -> str:
    result = run(["git", "-C", str(source), *args], timeout=60)
    if result.returncode:
        raise ApprovalError("Git確認に失敗しました: " + " ".join(args))
    return result.stdout.strip()


def source_state(source: Path) -> dict:
    branch = git(source, "branch", "--show-current")
    head = git(source, "rev-parse", "HEAD")
    dirty = git(source, "status", "--porcelain=v1", "--untracked-files=all")
    digest = source_digest(source)
    return {"branch": branch, "head": head, "clean": not bool(dirty), "status": dirty, "digest": digest}


def digest_at(source: Path, commit: str) -> str:
    archive = subprocess.run(["git", "-C", str(source), "archive", "--format=tar", commit, "src/pitchi_work"],
                             capture_output=True, timeout=60, check=False)
    if archive.returncode:
        raise ApprovalError("履歴sourceを読み取れません")
    import io
    members: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as tar:
        for member in tar.getmembers():
            if member.isfile() and member.name.startswith("src/pitchi_work/") and member.name.count("/") == 2:
                name = member.name.rsplit("/", 1)[-1]
                if name.endswith(".py"):
                    item = tar.extractfile(member)
                    if item is None or member.issym() or member.size > 1024 * 1024:
                        raise ApprovalError("履歴sourceに安全でないファイルがあります")
                    members[name] = item.read()
    if not members or len(members) > 100:
        raise ApprovalError("履歴sourceの構成が不正です")
    digest = hashlib.sha256()
    for name in sorted(members):
        digest.update(name.encode())
        digest.update(members[name])
    return digest.hexdigest()


def resolve_pin_head(source: Path, pin: str) -> list[str]:
    if not re.fullmatch(r"[a-f0-9]{64}", pin):
        return []
    commits = git(source, "log", "--all", "--format=%H", "--", "src/pitchi_work").splitlines()
    matches = []
    for commit in commits:
        if digest_at(source, commit) == pin:
            matches.append(commit)
    return matches


def private_config_path() -> Path:
    configured = os.environ.get("PITCH_CHAT_HEADLESS_CONFIG")
    path = Path(configured).expanduser() if configured else ROOT / "config/headless.local.toml"
    if path.resolve().parent != (ROOT / "config").resolve():
        raise ApprovalError("headless private config must be in this repository's config directory")
    if path.is_symlink() or not path.is_file():
        raise ApprovalError("private headless configが見つからないかsymlinkです")
    info = path.stat()
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise ApprovalError("private headless configは実行ユーザー所有・mode 600が必要です")
    return path


def state_dir() -> Path:
    path = Path.home() / ".local/state/pitch-chat/pitchi-work-source-approvals"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.stat()
    if path.is_symlink() or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ApprovalError("approval state directoryはowner-only mode 700が必要です")
    return path


def atomic_replace_pin(path: Path, expected: str, new: str) -> None:
    if not re.fullmatch(r"[a-f0-9]{64}", new):
        raise ApprovalError("source digestの形式が不正です")
    before = path.read_text()
    config = tomllib.loads(before)
    if config.get(PIN_KEY) != expected:
        raise ApprovalError("更新直前にcurrent pinが変化しました")
    matches = list(re.finditer(r'(?m)^reviewed_source_sha256[ \t]*=[ \t]*"(?:[a-f0-9]{64})?"[ \t]*(?:#.*)?$', before))
    if len(matches) != 1:
        raise ApprovalError("pin設定行が一意ではありません")
    updated = before[:matches[0].start()] + f'{PIN_KEY} = "{new}"' + before[matches[0].end():]
    after = tomllib.loads(updated)
    config[PIN_KEY] = new
    if after != config:
        raise ApprovalError("pin以外のTOML設定が変化します")
    _atomic_bytes(path, updated.encode(), 0o600)


def _atomic_bytes(path: Path, data: bytes, mode: int) -> None:
    if path.is_symlink():
        raise ApprovalError("private configがsymlinkです")
    info = path.stat()
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise ApprovalError("private config owner/modeが変化しました")
    fd, temp_name = tempfile.mkstemp(prefix=".headless.pin.", dir=path.parent)
    temp = Path(temp_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        os.chmod(path, mode)
        dirfd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def append_event(folder: Path, event: dict) -> None:
    path = folder / "approvals.jsonl"
    if path.exists() and (path.is_symlink() or path.stat().st_uid != os.geteuid() or stat.S_IMODE(path.stat().st_mode) != 0o600):
        raise ApprovalError("approval historyのowner/modeが不正です")
    payload = json.dumps(event, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ApprovalError("approval historyはowner-only mode 600が必要です")
        os.write(fd, payload.encode())
        os.fsync(fd)
    finally:
        os.close(fd)


def previous_approval(folder: Path):
    path = folder / "approvals.jsonl"
    if not path.exists():
        return None
    for line in reversed(path.read_text().splitlines()):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("outcome") == "approved":
            return event
    return None


def has_baseline(folder: Path) -> bool:
    path = folder / "approvals.jsonl"
    if not path.exists():
        return False
    for line in path.read_text().splitlines():
        try:
            if json.loads(line).get("event") == "baseline":
                return True
        except ValueError:
            continue
    return False


def canonical_verification(source: Path, python: str, *, system_verification: bool = False) -> dict:
    py = Path(python)
    if not py.is_absolute() or not py.is_file():
        raise ApprovalError("設定済みPitchi-work Python interpreterが見つかりません")
    def fixture(name: str) -> Path:
        variable = "PITCHI_PARTIAL_RUN" if name == "PARTIAL" else "PITCHI_SAVED_RUN"
        configured = os.environ.get(variable)
        if not configured:
            raise ApprovalError("Explicit private saved-run fixture configuration is required")
        path = Path(configured)
        home = Path.home().resolve()
        if not path.is_absolute() or path.is_symlink() or home not in path.resolve().parents or not path.is_dir():
            raise ApprovalError("既存saved-run回帰fixtureが利用できません")
        return path.resolve()
    partial_fixture, completed_fixture = fixture("PARTIAL"), fixture("COMPLETED")
    if source not in partial_fixture.parents and partial_fixture != source:
        raise ApprovalError("partial saved-run fixture is outside the mounted Pitchi-work source")
    saved_state = json.loads((completed_fixture / "state.json").read_text())
    saved_workspace = Path(saved_state["workspace"]).resolve()
    if Path.home().resolve() not in saved_workspace.parents or not saved_workspace.is_dir():
        raise ApprovalError("saved-run workspace fixture is unavailable")
    # This is Pitchi-work's existing fixed unittest discovery/bootstrap, run
    # with its Host validator isolation and read-only saved regression inputs.
    system_proof = ''
    if system_verification:
        if os.geteuid() == 0:
            raise ApprovalError('Run the review CLI as the ordinary operator, never root')
        # The system manager creates namespaces, then drops to the operator.
        # No outer user namespace/profile stack surrounds the existing validator.
        mounted = list(dict.fromkeys([source, completed_fixture, saved_workspace]))
        system_proof = (
            f'assert os.stat("/proc/self/ns/pid").st_ino != {os.stat("/proc/self/ns/pid").st_ino}, "PID_NAMESPACE_NOT_ISOLATED";'
            f'assert os.stat("/proc/self/ns/mnt").st_ino != {os.stat("/proc/self/ns/mnt").st_ino}, "MOUNT_NAMESPACE_NOT_ISOLATED";'
            'assert os.getpid()==1, "PRIVATE_PID_INIT_REQUIRED";'
            'assert next(x.split()[1] for x in open("/proc/self/status") if x.startswith("NoNewPrivs:"))=="1", "NO_NEW_PRIVILEGES_REQUIRED";'
            f'assert all(os.statvfs(p).f_flag & os.ST_RDONLY for p in {list(map(str,mounted))!r}), "READ_ONLY_INPUTS_REQUIRED";'
            'print("PITCHI_HOST_SANDBOX_PROOF="+json.dumps({"network":True,"pid":True,"mount":True,"readonly":True,"nonroot":True,"capabilities":0,"no_new_privileges":True}));'
        )
    network_guard = '' if system_verification else '''
import ipaddress
def _local_host(host):
    if host is None:
        return False
    host_text = str(host).split("%", 1)[0]
    try:
        return ipaddress.ip_address(host_text).is_loopback
    except ValueError:
        return str(host).lower().rstrip(".") == "localhost"
def _network_audit(event, args):
    host = None
    if event == "socket.getaddrinfo" and args:
        host = args[0]
    elif event in ("socket.connect", "socket.sendto") and len(args) > 1 and isinstance(args[1], tuple):
        host = args[1][0]
    if host is not None and not _local_host(host):
        raise PermissionError("external API/network access disabled during canonical verification")
sys.addaudithook(_network_audit)
'''
    namespace_proof = '' if not system_verification else (
        f'assert os.stat("/proc/self/ns/net").st_ino != {os.stat("/proc/self/ns/net").st_ino}, "NETWORK_NAMESPACE_NOT_ISOLATED";'
        'assert int(next(line.split()[1] for line in open("/proc/self/status") if line.startswith("CapEff:")),16) == 0, "VERIFICATION_CAPABILITIES_PRESENT";'
        'assert {name for _,name in socket.if_nameindex()} <= {"lo"}, "EXTERNAL_NETWORK_INTERFACE_VISIBLE";'
    )
    bootstrap = (
        'import sys,unittest,json,os,socket;'
        f'assert os.geteuid() == {os.geteuid()}, "VERIFICATION_UID_CHANGED";'
        + namespace_proof + network_guard + system_proof +
        'sys.path.insert(0,sys.argv[1]);'
        'suite=unittest.defaultTestLoader.discover(sys.argv[2],pattern=sys.argv[3]);'
        'r=unittest.TextTestRunner(verbosity=2).run(suite);'
        'print("PITCHI_HOST_TEST_RESULT="+json.dumps({"completed":True,"tests_run":r.testsRun,"skipped":len(r.skipped)}));'
        'sys.exit(0 if r.wasSuccessful() else 1)'
    )
    # Pitchi-work's standard discovery runs as a transient user unit. The
    # cgroup IP filter blocks external traffic while allowing loopback; unlike
    # PrivateNetwork, it introduces no parent network/user namespace that
    # would interfere with Host-validator bwrap. The Python audit hook is a
    # second guard for this fixed suite.
    launch_env = {"PATH": "/usr/bin:/bin", "HOME": "/tmp", "LANG": "C.UTF-8"}
    env = {"PATH": "/usr/bin:/bin", "HOME": "/tmp", "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
           "PITCHI_SAVED_RUN": str(completed_fixture), "PITCHI_PARTIAL_RUN": str(partial_fixture)}
    for key in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"):
        value = os.environ.get(key)
        if value:
            launch_env[key] = value
    if system_verification:
        command = ['/usr/bin/sudo', '-n', '/usr/bin/systemd-run', '--wait', '--pipe', '--collect',
            '--property=Type=exec', '--property=PrivateNetwork=yes', '--property=PrivatePIDs=yes',
            '--property=PrivateMounts=yes', '--property=PrivateUsers=no', '--property=PrivateTmp=yes', '--property=PrivateDevices=yes',
            '--property=ProtectSystem=strict', '--property=ProtectHome=tmpfs',
            '--property=TemporaryFileSystem=/run /mnt /media',
            '--property=NoNewPrivileges=yes', '--property=CapabilityBoundingSet=',
            f'--property=User={os.geteuid()}', f'--property=Group={os.getegid()}',
            f'--property=WorkingDirectory={source}', f'--property=RuntimeMaxSec={TEST_TIMEOUT}']
        for path in mounted:
            if any(ch.isspace() for ch in str(path)) or ':' in str(path):
                raise ApprovalError('Whitespace/colon in an isolated mount path is unsupported')
        command.append('--property=BindReadOnlyPaths=' + ' '.join(map(str,mounted)))
        command += ['--', '/usr/bin/env', '-i', *[key+'='+value for key,value in env.items()],
            str(py), '-I', '-B', '-c', bootstrap, str(source/'src'), str(source/'tests'), 'test_*.py']
    else:
        command = user_verification_command(source, py, bootstrap, env, launch_env)
    started = time.monotonic()
    # The transient unit owns the exact 900-second test timeout; allow the
    # systemd-run client a short grace window to report the unit's exit status.
    runner_timeout = TEST_TIMEOUT + 15
    result = subprocess.run(command, cwd=source, env=launch_env, text=True, capture_output=True,
                            timeout=runner_timeout, check=False)
    reports = [line.split("PITCHI_HOST_TEST_RESULT=", 1)[1] for line in result.stdout.splitlines()
               if line.startswith("PITCHI_HOST_TEST_RESULT=")]
    observation = json.loads(reports[0]) if len(reports) == 1 else {}
    proofs = [line.split('PITCHI_HOST_SANDBOX_PROOF=',1)[1] for line in result.stdout.splitlines()
              if line.startswith('PITCHI_HOST_SANDBOX_PROOF=')]
    sandbox_proven = not system_verification or (len(proofs)==1 and json.loads(proofs[0]) == {
        'network':True,'pid':True,'mount':True,'readonly':True,'nonroot':True,'capabilities':0,'no_new_privileges':True})
    valid = sandbox_proven and observation.get("completed") is True and type(observation.get("tests_run")) is int and observation["tests_run"] > 0
    stdout = result.stdout
    failure_ids = sorted(set(re.findall(r"^(?:FAIL|ERROR): ([A-Za-z0-9_.]+) \([^)]*\)$",
                                       result.stderr, re.MULTILINE)))
    failure_types = sorted(set(re.findall(r"^([A-Za-z][A-Za-z0-9_.]*(?:Error|Exception|Failure))(?::|$)",
                                          result.stderr, re.MULTILINE)))
    summary_match = re.search(r"^FAILED \(([^\n]+)\)$", result.stderr, re.MULTILINE)
    launch_failure = None
    if system_verification and result.returncode and not observation:
        launch_failure = ('ADMIN_AUTHENTICATION_REQUIRED' if any(text in result.stderr for text in ('a password is required','interactive authentication is required')) else
                          'ADMIN_ISOLATION_LAUNCH_REJECTED' if 'sudo:' in result.stderr else 'ISOLATION_PROOF_OR_LAUNCH_FAILED')
    return {"canonical_id": "pitchi-work-stdlib-unittest-discover-test_*.py-v1",
            "passed": result.returncode == 0 and valid, "exit_code": result.returncode,
            "tests_run": observation.get("tests_run", 0), "skipped": observation.get("skipped", 0),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "output_sha256": hashlib.sha256((stdout + result.stderr).encode()).hexdigest(),
            "failed_tests": failure_ids, "failure_types": failure_types,
            "failure_summary": summary_match.group(1) if summary_match else None,
            "launch_failure": launch_failure,
            "sandbox": ("administrator-created transient system unit; nonroot/capability-zero; verified network/pid/mount; read-only inputs; private homes/tmp; unchanged nested Host validator" if system_verification else
                "Pitchi-work ordinary unittest discovery in a transient cgroup IP-filtered unit; external traffic denied, loopback retained; inherited environment and credentials removed; Python egress audit hook; nested Host-validator bwrap unchanged"),
            "report_valid": valid}


def user_verification_command(source, py, bootstrap, env, launch_env):
    manager_env = subprocess.run(["/usr/bin/systemctl", "--user", "show-environment"],
                                 env=launch_env, text=True, capture_output=True,
                                 timeout=15, check=False)
    if manager_env.returncode:
        raise ApprovalError("canonical verificationにはuser systemd cgroup network filteringが必要です")
    manager_names = sorted(({line.split("=", 1)[0] for line in manager_env.stdout.splitlines()
                            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", line)}
                           | {"USER", "LOGNAME", "SHELL", "TMPDIR", "PWD"}) - set(env))
    command = ["/usr/bin/systemd-run", "--user", "--wait", "--pipe", "--collect",
               "--property=IPAddressDeny=any", "--property=IPAddressAllow=localhost",
               f"--property=WorkingDirectory={source}", f"--property=RuntimeMaxSec={TEST_TIMEOUT}"]
    if manager_names:
        command.append("--property=UnsetEnvironment=" + " ".join(manager_names))
    command.extend("--setenv=" + key + "=" + value for key, value in env.items())
    command += ["--", str(py), "-I", "-B", "-c", bootstrap,
                str(source / "src"), str(source / "tests"), "test_*.py"]
    return command


def require_verified(result: dict) -> None:
    if result.get("passed") is not True or result.get("report_valid") is not True or result.get("tests_run", 0) <= 0:
        raise ApprovalError("canonical verification failed; pin is unchanged")


def dry_run_result(config_path: Path, source: Path, initial: dict, pin: str, verification: dict) -> dict:
    require_verified(verification)
    latest = source_state(source)
    current_pin = tomllib.loads(config_path.read_text()).get(PIN_KEY, "")
    if (not latest["clean"] or latest["head"] != initial["head"]
            or latest["digest"] != initial["digest"] or current_pin != pin):
        raise ApprovalError("dry-run後のGit/pin再確認に失敗しました; pin is unchanged")
    return {"head_unchanged": True, "digest_unchanged": True, "clean": True,
            "pin_unchanged": True, "pin": current_pin, "would_approve": True}


def endpoint_check(url: str, expected_json=None) -> int:
    request = Request(url, headers={"Accept": "application/json"})
    with LOCAL_HTTP.open(request, timeout=5) as response:
        body = response.read(1024 * 1024 + 1)
        if len(body) > 1024 * 1024:
            raise ApprovalError("HOST response exceeds limit")
        if expected_json:
            data = json.loads(body)
            for key, value in expected_json.items():
                if data.get(key) != value:
                    raise ApprovalError("HOST health contract mismatch")
        return response.status


def verify_endpoints(digest: str) -> dict:
    health = endpoint_check(HEALTH_URL, {"status": "ok", "storage": "ok"})
    with LOCAL_HTTP.open(Request(CATALOG_URL, headers={"Accept": "application/json"}), timeout=5) as response:
        catalog = response.status
        data = json.loads(response.read(1024 * 1024 + 1))
    if catalog != 200 or data.get("enabled") is not True or not isinstance(data.get("projects"), list):
        raise ApprovalError("Work catalog contract/HTTP status mismatch")
    if data.get("source_digest") != digest:
        raise ApprovalError("Pitchi-work gatewayが期待するsource digestを認識していません")
    return {"health_status": health, "catalog_status": catalog, "gateway_digest_matches": True}


def unit_active() -> bool:
    result = run(["systemctl", "--user", "is-active", "--quiet", SERVICE], timeout=15)
    return result.returncode == 0


def restart_unit() -> None:
    result = run(["systemctl", "--user", "restart", SERVICE], timeout=60)
    if result.returncode:
        raise ApprovalError("Pitch-chat HOST service restart failed")


def transactional_repin(path: Path, folder: Path, *, old_pin: str, new_pin: str, event: dict,
                        restart=restart_unit, check=verify_endpoints) -> dict:
    record = {**event, "event": "approval_transaction", "previous_pin": old_pin,
              "digest": new_pin, "outcome": "started"}
    append_event(folder, record)
    changed = False
    try:
        atomic_replace_pin(path, old_pin, new_pin)
        changed = True
        restart()
        endpoints = check(new_pin)
        record.update(outcome="approved", approved_at=datetime.now(timezone.utc).isoformat(), endpoints=endpoints)
        append_event(folder, record)
        return endpoints
    except Exception as failure:
        # A failure after replace restores exactly the previous pin, then
        # restarts the same HOST service so it rereads that restored setting.
        try:
            current = tomllib.loads(path.read_text()).get(PIN_KEY)
            if current == new_pin:
                atomic_replace_pin(path, new_pin, old_pin)
                changed = False
                restart()
                rollback_service = "restarted"
            elif current == old_pin:
                rollback_service = "not_needed"
            else:
                rollback_service = "pin_changed_elsewhere"
                raise ApprovalError("rollback時にpinが第三者変更されています")
        except Exception:
            rollback_service = "restart_or_restore_failed"
        record.update(outcome="rolled_back", failure_class=type(failure).__name__,
                      rollback_service=rollback_service, pin_restored=not changed)
        append_event(folder, record)
        raise ApprovalError("検証失敗。旧pinへのrollback結果: " + rollback_service) from None


def main(*, dry_run: bool = False, system_verification: bool = False) -> int:
    config_path = private_config_path()
    config = tomllib.loads(config_path.read_text())
    if config.get("enabled") is not True:
        raise ApprovalError("headless Work gateway is disabled")
    source = Path(config["source_root"])
    if source.is_symlink() or not source.is_dir() or git(source, "rev-parse", "--show-toplevel") != str(source.resolve()):
        raise ApprovalError("configured Pitchi-work source root is unsafe or not the Git root")
    folder = state_dir()
    lock_path = folder / "approval.lock"
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    lock_info = os.fstat(lock_fd)
    if lock_info.st_uid != os.geteuid() or stat.S_IMODE(lock_info.st_mode) != 0o600:
        os.close(lock_fd)
        raise ApprovalError("approval lockはowner-only mode 600が必要です")
    with os.fdopen(lock_fd, "r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = source_state(source)
        pin = config.get(PIN_KEY, "")
        matches = resolve_pin_head(source, pin)
        previous = previous_approval(folder)
        print(f"branch: {current['branch'] or '(detached HEAD)'}\nHEAD: {current['head']}\n"
              f"Git clean: {'yes' if current['clean'] else 'no'}\nsource digest: {current['digest']}\ncurrent pin: {pin or '(empty)'}")
        if not current["clean"]:
            print("Git status:\n" + current["status"])
            raise ApprovalError("Pitchi-work Git worktreeがcleanではありません")
        if not current["branch"]:
            raise ApprovalError("Pitchi-work HEAD is detached; a named branch is required")
        if not has_baseline(folder):
            known_head = matches[0] if len(matches) == 1 else None
            append_event(folder, {"event": "baseline", "at": datetime.now(timezone.utc).isoformat(),
                                  "uid": os.geteuid(), "pin": pin, "head": known_head,
                                  "status": "source_digest_exact_match" if known_head else "旧HEAD不明"})
        if previous:
            print("前回承認: " + json.dumps({k: previous.get(k) for k in ("head", "digest", "approved_at", "verification")}, ensure_ascii=False))
        elif len(matches) == 1:
            print(f"前回pinに一致する履歴HEAD: {matches[0]}")
        else:
            print("前回pin HEAD: 旧HEAD不明")
        if matches and current["head"] not in matches:
            base = next((commit for commit in matches if run(["git", "-C", str(source), "merge-base", "--is-ancestor", commit, current["head"]]).returncode == 0), matches[0])
            print(f"\n--- source差分 {base}..{current['head']} ---")
            diff = run(["git", "-C", str(source), "diff", "--no-ext-diff", "--no-color", base, current["head"], "--", "src/pitchi_work"], timeout=60)
            print(diff.stdout)
        elif not matches:
            print("\n--- 旧HEAD不明: prior diff cannot be established; review the complete current source tree ---")
            parent = git(source, "rev-parse", "HEAD^")
            print(run(["git", "-C", str(source), "diff", "--no-ext-diff", "--no-color", parent, current["head"], "--", "src/pitchi_work"], timeout=60).stdout)
        if current["digest"] == pin and not dry_run:
            print("pin already matches; no approval, write, verification run, or restart required")
            print(json.dumps(verify_endpoints(current["digest"]), ensure_ascii=False))
            return 0
        try:
            verification = canonical_verification(source, config["python_executable"], system_verification=system_verification)
        except Exception as failure:
            append_event(folder, {"event": "verification", "at": datetime.now(timezone.utc).isoformat(),
                                  "branch": current["branch"], "head": current["head"], "digest": current["digest"],
                                  "verification": {"passed": False, "failure_class": type(failure).__name__},
                                  "uid": os.geteuid()})
            raise
        append_event(folder, {"event": "verification", "at": datetime.now(timezone.utc).isoformat(),
                              "branch": current["branch"], "head": current["head"], "digest": current["digest"],
                              "verification": verification, "uid": os.geteuid()})
        print("canonical verification: " + json.dumps(verification, ensure_ascii=False))
        require_verified(verification)
        if dry_run:
            outcome = dry_run_result(config_path, source, current, pin, verification)
            print("DRY RUN (pin update / approval / restart omitted): " + json.dumps(outcome, ensure_ascii=False))
            return 0
        if not unit_active():
            raise ApprovalError("pitch-chat-host.service is not active; pin was not changed")
        phrase = f"APPROVE {current['head']} {current['digest']}"
        print(f"承認する場合は次をそのまま入力してください: {phrase}")
        if not sys.stdin.isatty():
            raise ApprovalError("interactive terminal is required; approval was not requested")
        if input("> ").strip() != phrase:
            raise ApprovalError("human approval did not match; pin is unchanged")
        immediately_before = source_state(source)
        config_now = tomllib.loads(config_path.read_text())
        if (not immediately_before["clean"] or immediately_before["head"] != current["head"]
                or immediately_before["digest"] != current["digest"] or config_now.get(PIN_KEY) != pin):
            raise ApprovalError("承認直前のGit/pin再確認に失敗しました")
        event = {"at": datetime.now(timezone.utc).isoformat(),
                 "uid": os.geteuid(), "branch": current["branch"], "head": current["head"],
                 "digest": current["digest"], "previous_pin": pin, "previous_head": matches[0] if len(matches) == 1 else None,
                 "previous_head_status": "known" if len(matches) == 1 else "旧HEAD不明",
                 "verification": verification, "outcome": "started"}
        endpoints = transactional_repin(config_path, folder, old_pin=pin, new_pin=current["digest"],
                                        event=event, restart=restart_unit, check=verify_endpoints)
        print("承認完了: " + json.dumps(endpoints, ensure_ascii=False))
        return 0


if __name__ == "__main__":
    try:
        parser = argparse.ArgumentParser(description="Review and explicitly re-pin Pitchi-work source")
        parser.add_argument("--dry-run", action="store_true", help="run canonical verification and state recheck without approval or changes")
        parser.add_argument('--system-verification', action='store_true', help='explicit administrator-created transient isolation; requires prior OS sudo authentication; no fallback')
        args = parser.parse_args()
        raise SystemExit(main(dry_run=args.dry_run, system_verification=args.system_verification))
    except (ApprovalError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
