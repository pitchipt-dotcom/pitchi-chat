import json
import os
from pathlib import Path
import stat
import subprocess

import pytest

from scripts import reapprove_pitch_work as approval


@pytest.fixture
def verification_source(tmp_path, monkeypatch):
    """Artificial directories/state for mocked runner checks, never live evidence."""
    source=tmp_path/'pitchi-work'
    for folder in [source/'src',source/'tests',source/'runs/partial',tmp_path/'saved',tmp_path/'workspace']:
        folder.mkdir(parents=True,exist_ok=True)
    (tmp_path/'saved/state.json').write_text(json.dumps({'workspace':str(tmp_path/'workspace')}))
    monkeypatch.setattr(Path,'home',classmethod(lambda cls:tmp_path))
    monkeypatch.setenv('PITCHI_PARTIAL_RUN',str(source/'runs/partial'))
    monkeypatch.setenv('PITCHI_SAVED_RUN',str(tmp_path/'saved'))
    return source


def private_config(path: Path, pin: str) -> Path:
    path.write_text(f'enabled = true\nreviewed_source_sha256 = "{pin}"\n')
    path.chmod(0o600)
    return path


def test_pin_update_changes_only_the_pin_and_keeps_private_mode(tmp_path):
    old, new = "a" * 64, "b" * 64
    config = private_config(tmp_path / "headless.toml", old)
    before = config.read_text()
    approval.atomic_replace_pin(config, old, new)
    assert config.read_text() == before.replace(old, new)
    assert stat.S_IMODE(config.stat().st_mode) == 0o600


def test_first_pin_can_be_registered_from_an_empty_pin(tmp_path):
    new = "b" * 64
    config = private_config(tmp_path / "headless.toml", "")
    approval.atomic_replace_pin(config, "", new)
    assert 'reviewed_source_sha256 = "' + new + '"' in config.read_text()
    assert stat.S_IMODE(config.stat().st_mode) == 0o600


def test_pin_update_refuses_changed_value_and_symlink(tmp_path):
    config = private_config(tmp_path / "headless.toml", "a" * 64)
    with pytest.raises(approval.ApprovalError):
        approval.atomic_replace_pin(config, "c" * 64, "b" * 64)
    link = tmp_path / "link.toml"
    link.symlink_to(config)
    with pytest.raises(approval.ApprovalError):
        approval.atomic_replace_pin(link, "a" * 64, "b" * 64)


def test_transaction_rolls_back_pin_and_restarts_after_failed_endpoint_check(tmp_path):
    old, new = "a" * 64, "b" * 64
    config = private_config(tmp_path / "headless.toml", old)
    history = tmp_path / "history"
    history.mkdir(mode=0o700)
    restarted = []

    def fail(_digest):
        raise RuntimeError("synthetic endpoint failure")

    with pytest.raises(approval.ApprovalError, match="rollback結果: restarted"):
        approval.transactional_repin(config, history, old_pin=old, new_pin=new,
            event={"head": "c" * 40, "branch": "review"},
            restart=lambda: restarted.append(config.read_text()), check=fail)
    assert 'reviewed_source_sha256 = "' + old + '"' in config.read_text()
    assert len(restarted) == 2
    rows = [json.loads(line) for line in (history / "approvals.jsonl").read_text().splitlines()]
    assert rows[-1]["outcome"] == "rolled_back"
    assert rows[-1]["pin_restored"] is True
    assert rows[-1]["rollback_service"] == "restarted"
    assert all(not any(word in key.lower() for word in ("token", "secret", "password", "credential"))
               for row in rows for key in row)


def test_verification_failure_never_writes_pin(tmp_path):
    old, new = "a" * 64, "b" * 64
    config = private_config(tmp_path / "headless.toml", old)
    history = tmp_path / "history"
    history.mkdir(mode=0o700)
    before = config.read_bytes()
    with pytest.raises(approval.ApprovalError, match="canonical verification failed"):
        approval.dry_run_result(config, tmp_path, {"head": "c" * 40, "digest": new}, old,
                                {"passed": False, "report_valid": True, "tests_run": 10})
    assert config.read_bytes() == before


def test_canonical_verification_uses_direct_fixed_discovery_and_scrubbed_environment(monkeypatch, verification_source):
    source = verification_source
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        class Result:
            returncode = 0
            stderr = ""
            stdout = ('OPENAI_API_KEY=synthetic-secret\n' if argv[0].endswith("systemctl") else
                      'PITCHI_HOST_TEST_RESULT={"completed":true,"tests_run":1,"skipped":0}\n')
        return Result()

    monkeypatch.setattr(approval.subprocess, "run", fake_run)
    result = approval.canonical_verification(source, "/usr/bin/python3")
    argv, kwargs = calls[-1]
    assert calls[0][0] == ["/usr/bin/systemctl", "--user", "show-environment"]
    assert argv[0:3] == ["/usr/bin/systemd-run", "--user", "--wait"]
    assert "--property=IPAddressDeny=any" in argv
    assert "--property=IPAddressAllow=localhost" in argv
    assert not any("PrivateNetwork" in arg for arg in argv)
    assert "bwrap" not in argv
    assert argv[-3:] == [str(source / "src"), str(source / "tests"), "test_*.py"]
    assert kwargs["timeout"] == approval.TEST_TIMEOUT + 15
    assert set(kwargs["env"]) <= {"PATH", "HOME", "LANG", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"}
    unset = next(arg for arg in argv if arg.startswith("--property=UnsetEnvironment="))
    assert "OPENAI_API_KEY" in unset
    passed_names = {arg.split("=", 2)[1] for arg in argv if arg.startswith("--setenv=")}
    assert passed_names == {"PATH", "HOME", "LANG", "PYTHONDONTWRITEBYTECODE", "PITCHI_SAVED_RUN", "PITCHI_PARTIAL_RUN"}
    assert "--setenv=OPENAI_API_KEY=synthetic-secret" not in argv
    assert result["passed"] is True


def test_digest_at_matches_existing_source_digest_for_current_head():
    source = Path(os.environ.get("PITCHI_WORK_TEST_SOURCE", "/path/to/optional-work-source"))
    if not source.is_dir():
        pytest.skip("Pitchi-work source is unavailable in this checkout")
    status = subprocess.run(["git", "-C", str(source), "status", "--porcelain=v1"],
                            capture_output=True, text=True, check=True).stdout
    if status:
        pytest.skip("Pitchi-work source is changing; archive/worktree digest comparison requires a clean source")
    head = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    assert approval.digest_at(source, head) == approval.source_digest(source)


def test_external_network_guard_is_installed_before_test_discovery(monkeypatch, verification_source):
    calls = []
    actual_run = subprocess.run
    def fake_run(argv, **kwargs):
        calls.append(argv)
        class Result:
            returncode = 0
            stderr = ''
            stdout = '' if argv[0].endswith('systemctl') else 'PITCHI_HOST_TEST_RESULT={"completed":true,"tests_run":1,"skipped":0}\n'
        return Result()
    monkeypatch.setattr(approval.subprocess, 'run', fake_run)
    approval.canonical_verification(verification_source, '/usr/bin/python3')
    program = calls[-1][calls[-1].index('-c') + 1]
    prefix = program.split('sys.path.insert', 1)[0]
    rejected = actual_run(['/usr/bin/python3', '-I', '-B', '-c', prefix + '\nsocket.getaddrinfo("external.example", 443)'], capture_output=True, text=True)
    assert rejected.returncode != 0
    assert 'external API/network access disabled' in rejected.stderr
    assert 'sys.addaudithook(_network_audit)' in prefix
    assert program.index('sys.addaudithook(_network_audit)') < program.index('sys.path.insert')


def test_canonical_runner_refuses_to_fall_back_when_cgroup_filter_is_unavailable(monkeypatch, verification_source):
    calls=[]
    def unavailable(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, stdout='', stderr='user manager unavailable')
    monkeypatch.setattr(approval.subprocess, 'run', unavailable)
    with pytest.raises(approval.ApprovalError, match='cgroup network filtering'):
        approval.canonical_verification(verification_source, '/usr/bin/python3')
    assert calls == [['/usr/bin/systemctl', '--user', 'show-environment']]


def test_system_isolation_keeps_formal_discovery_and_denies_unproven_success(monkeypatch, verification_source):
    source=verification_source
    calls=[]
    proof={'network':True,'pid':True,'mount':True,'readonly':True,'nonroot':True,'capabilities':0,'no_new_privileges':True}
    output={'text':''}
    def fake_run(argv,**kwargs):
        calls.append((argv,kwargs))
        return subprocess.CompletedProcess(argv,0,stdout=output['text'],stderr='')
    monkeypatch.setattr(approval.subprocess,'run',fake_run)
    report='PITCHI_HOST_TEST_RESULT={"completed":true,"tests_run":10,"skipped":0}\n'
    output['text']=report
    assert not approval.canonical_verification(source,'/usr/bin/python3',system_verification=True)['passed']
    output['text']='PITCHI_HOST_SANDBOX_PROOF='+json.dumps(proof)+'\n'+report
    assert approval.canonical_verification(source,'/usr/bin/python3',system_verification=True)['passed']
    argv,kwargs=calls[-1]
    assert argv[:4]==['/usr/bin/sudo','-n','/usr/bin/systemd-run','--wait']
    assert '--user' not in argv
    for prop in ('PrivateNetwork=yes','PrivatePIDs=yes','PrivateMounts=yes','PrivateTmp=yes',
                 'ProtectHome=tmpfs','ProtectSystem=strict','NoNewPrivileges=yes','CapabilityBoundingSet='):
        assert '--property='+prop in argv
    assert '--property=User='+str(os.geteuid()) in argv
    assert '--property=Group='+str(os.getegid()) in argv
    assert argv[argv.index('--')+1:argv.index('--')+3]==['/usr/bin/env','-i']
    assert argv[-3:]==[str(source/'src'),str(source/'tests'),'test_*.py']
    program=argv[argv.index('-c')+1]
    for check in ('NETWORK_NAMESPACE_NOT_ISOLATED','PID_NAMESPACE_NOT_ISOLATED',
                  'MOUNT_NAMESPACE_NOT_ISOLATED','VERIFICATION_UID_CHANGED',
                  'VERIFICATION_CAPABILITIES_PRESENT','EXTERNAL_NETWORK_INTERFACE_VISIBLE',
                  'READ_ONLY_INPUTS_REQUIRED','NO_NEW_PRIVILEGES_REQUIRED'):
        assert check in program
    assert 'unittest.defaultTestLoader.discover' in program
    assert kwargs['cwd']==source


def test_system_isolation_failure_has_no_user_or_plain_fallback(monkeypatch, verification_source):
    calls=[]
    def denied(argv,**kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv,1,stdout='',stderr='sudo: interactive authentication is required')
    monkeypatch.setattr(approval.subprocess,'run',denied)
    result=approval.canonical_verification(verification_source,'/usr/bin/python3',system_verification=True)
    assert not result['passed'] and result['tests_run']==0
    assert result['launch_failure']=='ADMIN_AUTHENTICATION_REQUIRED'
    assert len(calls)==1
    assert calls[0][:2]==['/usr/bin/sudo','-n']


def test_system_proof_fails_outside_required_namespaces_before_tests(monkeypatch, verification_source):
    calls=[]
    def fake_run(argv,**kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv,0,stdout='',stderr='')
    actual_run=subprocess.run
    monkeypatch.setattr(approval.subprocess,'run',fake_run)
    approval.canonical_verification(verification_source,'/usr/bin/python3',system_verification=True)
    argv=calls[-1];program=argv[argv.index('-c')+1]
    prefix=program.split('sys.path.insert',1)[0]
    result=actual_run(['/usr/bin/python3','-I','-B','-c',prefix],capture_output=True,text=True)
    assert result.returncode!=0
    assert 'NETWORK_NAMESPACE_NOT_ISOLATED' in result.stderr


def test_canonical_verification_requires_explicit_private_fixture_paths(monkeypatch):
    monkeypatch.delenv('PITCHI_SAVED_RUN',raising=False)
    monkeypatch.delenv('PITCHI_PARTIAL_RUN',raising=False)
    with pytest.raises(approval.ApprovalError,match='Explicit private saved-run'):
        approval.canonical_verification(Path('/tmp/synthetic-source'),'/usr/bin/python3')
