"""Trusted resource config validation; no process execution."""

import pytest

from backend.app.resources.config import load_resources
from scripts.check_public_repo import content_issue, path_issue


@pytest.mark.parametrize("setting", ["queue_timeout=nan", "release_timeout=0", "queue_timeout=601",
                                     "inspect=1", "max_idle_mib=9000", "unknown=true"])
def test_resource_policy_fail_closed(tmp_path, setting):
    path = tmp_path / "resources.local.toml"
    path.write_text("[gpu]\n" + setting + "\n")
    with pytest.raises(ValueError):
        load_resources(path)


def test_public_resource_and_env_templates_are_not_live():
    assert content_issue("config/resources.toml", b"[gpu]\ninspect=false\n") is None
    assert content_issue("config/resources.toml", b"[gpu]\ninspect=true\n")
    assert path_issue("config/host.local.env")
    assert path_issue("gpu.host.lock")
