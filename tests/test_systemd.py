"""HOST-only unit template checks, not changes to the running service."""

from pathlib import Path


def test_unit_is_host_only_and_uses_private_configuration():
    unit = (Path(__file__).resolve().parents[1] / "deploy/pitch-chat-host.service").read_text()
    assert "python -m backend.app.serve" in unit
    assert "EnvironmentFile=-%h/pitch-chat/config/host.local.env" in unit
    assert "Restart=on-failure" in unit and "Restart=always" not in unit
    assert "WantedBy=default.target" in unit and "RestartSec=5" in unit
    assert "UMask=0077" in unit and "NoNewPrivileges=true" in unit
    assert "sudo" not in unit and "nvidia-smi" not in unit
    assert "0.0.0.0" not in unit and ".ts.net" not in unit
