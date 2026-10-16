from pathlib import Path
from unittest.mock import patch
import pytest
from backend.app.resources.memory import check_memory, MemoryAdmissionFailure
from backend.app.resources.memory_recovery import recovery_assessment, admission_message, linux_memory_details


def test_swap_refusal_keeps_snapshot_and_threshold():
    state = {'ram_available_mib': 52766, 'swap_free_mib': 301, 'swap_used_mib': 7891}
    with patch('backend.app.resources.memory.memory_state', return_value=state):
        with pytest.raises(MemoryAdmissionFailure) as found:
            check_memory(43008, 1024)
    error = found.value
    assert str(error) == 'insufficient_swap_reserve'
    assert error.memory_details['stage'] == 'preflight'
    assert error.memory_details['min_swap_free_mib'] == 1024
    assert 'モデル起動処理には進んでいません' in admission_message(str(error), state, 43008, 1024)


def test_runtime_failure_not_mislabeled_as_preflight():
    with patch('backend.app.resources.memory.memory_state', return_value={
            'ram_available_mib': 10000, 'swap_free_mib': 100, 'swap_used_mib': 8092}):
        with pytest.raises(MemoryAdmissionFailure) as found:
            check_memory(5120, 256, stage='runtime')
    assert found.value.memory_details['stage'] == 'runtime'


def test_recovery_assessment_is_not_permission_and_starts_nothing():
    data = recovery_assessment({'ram_available_mib': 52766, 'swap_used_mib': 7891}, 43008)
    assert data['estimated_ram_after_swapin_mib'] == 44875
    assert data['ram_headroom_for_swapin']
    assert data['estimated_model_ram_gate_after_swapin']
    assert not data['automatic_recovery'] and not data['assessment_is_execution_permission']
    assert data['administrator_approval_required']


def test_recovery_refuses_insufficient_ram():
    data = recovery_assessment({'ram_available_mib': 10000, 'swap_used_mib': 7891}, 43008)
    assert not data['ram_headroom_for_swapin']
    assert not data['estimated_model_ram_gate_after_swapin']


def test_unknown_cause_is_not_guessed():
    assert admission_message('owned_process_spawn_failed', {}, 43008, 1024) is None


def test_incomplete_telemetry_refused(tmp_path):
    file = tmp_path / 'meminfo'
    file.write_text('MemAvailable: 1024 kB\n')
    with pytest.raises(ValueError): linux_memory_details(file)
