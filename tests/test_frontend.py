"""Optional Node DOM/mock checks; no real browser or external model is used."""

from pathlib import Path
import shutil
import subprocess

import pytest


def test_model_selector_dom_mock():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the frontend DOM/mock test")
    result = subprocess.run([node, "tests/frontend_models.cjs"],
                            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "checks passed" in result.stdout


def test_work_reapproval_dom_distinguishes_network_failure():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the frontend DOM/mock test")
    result = subprocess.run([node, "tests/work_reapproval_ui.cjs"],
                            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS source reapproval is distinct" in result.stdout


def test_image_preview_edits_render_once_and_unverified_backend_is_blocked():
    node=shutil.which('node')
    if node is None:pytest.skip('Node.js required')
    result=subprocess.run([node,'tests/frontend_images.cjs'],cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'Image preview/edit/render DOM checks passed' in result.stdout


def test_media_direct_video_save_retry_dom():
    node=shutil.which('node')
    if node is None:pytest.skip('Node.js required')
    result=subprocess.run([node,'tests/frontend_media.cjs'],cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'Media direct/video/preview/save/retry DOM checks passed' in result.stdout


def test_flux_preview_edit_before_render():
    node=shutil.which('node')
    if node is None:pytest.skip('Node.js required')
    result=subprocess.run([node,'tests/frontend_flux.cjs'],cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'FLUX preview/edit/render DOM checks passed' in result.stdout
