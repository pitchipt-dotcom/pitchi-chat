import pytest

from backend.app.config import Settings


@pytest.fixture(autouse=True)
def isolated_host_runtime(tmp_path, monkeypatch):
    # Tests must not contend with or alter the live HOST's OS GPU lock.
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))


@pytest.fixture
def settings(tmp_path):
    return Settings(tmp_path / "history.sqlite3", "", "test-qwen", "TEST_MODEL_KEY", 1)
