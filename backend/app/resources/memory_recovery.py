"""Read-only recovery assessment. Never changes swap or stops a process."""
from pathlib import Path


def recovery_assessment(state, required_ram_mib, *, reserve_mib=8192):
    available = state['ram_available_mib']
    used = state['swap_used_mib']
    remaining = available - used
    return {
        'automatic_recovery': False, 'administrator_approval_required': True,
        'estimated_ram_after_swapin_mib': remaining,
        'recovery_ram_reserve_mib': reserve_mib,
        'ram_headroom_for_swapin': remaining >= reserve_mib,
        'estimated_model_ram_gate_after_swapin': remaining >= required_ram_mib,
        'assessment_is_execution_permission': False,
        'instructions': '管理者が新規要求を止め、全ジョブ・モデル停止と現在のRAM/Swapを再確認する。復旧後も通常の起動安全判定を通す。自動swapoffや他プロセス強制終了は行わない。',
    }


def admission_message(reason, state, required_ram_mib=0, min_swap_free_mib=0):
    if reason is None and state.get('admission_mode') == 'physical_ram':
        return f"Swap残量は少ないですが、実測に基づくRAM起動条件を満たしています。MemAvailable {state['ram_available_mib']} MiB / 必要 {required_ram_mib} MiB。swap初期化やsudo操作は不要です。"
    if reason in {'memory_pressure', 'insufficient_cgroup_ram', 'memory_safety_telemetry_unavailable'}:
        return {'memory_pressure': 'メモリ圧迫を検出したため起動を見合わせました。処理完了後に再確認してください。',
                'insufficient_cgroup_ram': 'サービスのメモリ制限内に必要なRAM余裕がないため起動を見合わせました。',
                'memory_safety_telemetry_unavailable': 'メモリ圧迫またはサービス制限を確認できないため起動を見合わせました。'}[reason]
    if reason == 'media_memory_shape_unreviewed':
        return '同時生成枚数に対する実測済みメモリ予算がありません。1枚ずつ生成するか追加検証が必要です。起動は行っていません。'
    if reason == 'insufficient_swap_reserve':
        return (f"起動前のSwap安全判定で拒否しました。SwapFree {state['swap_free_mib']} MiB / "
                f"必要 {min_swap_free_mib} MiB。MemAvailable {state['ram_available_mib']} MiB / "
                f"必要 {required_ram_mib} MiB。モデル起動処理には進んでいません。他の処理の完了後に再確認してください。低Swap用RAM条件が未設定の場合だけ、管理者の復旧確認を検討してください。")
    if reason == 'insufficient_ram':
        return (f"起動前のRAM安全判定で拒否しました。MemAvailable {state['ram_available_mib']} MiB / "
                f"必要 {required_ram_mib} MiB。モデル起動処理には進んでいません。")
    return None


def linux_memory_details(path=Path('/proc/meminfo')):
    wanted = {'MemTotal', 'MemAvailable', 'SwapTotal', 'SwapFree', 'SwapCached',
              'Shmem', 'Mlocked', 'CommitLimit', 'Committed_AS'}
    result = {}
    for line in path.read_text().splitlines():
        name, _, value = line.partition(':')
        if name in wanted:
            result[name] = int(value.split()[0]) // 1024
    if not wanted <= result.keys():
        raise ValueError('incomplete memory telemetry')
    return result


def failure_detail(code, details):
    message = admission_message(details['reason'], details, details['required_ram_mib'], details['min_swap_free_mib'])
    if details['stage'] != 'preflight':
        phase = '起動中' if details['stage'] == 'startup' else '生成中' if details['stage'] == 'generation' else '生成前'
        reason = details['reason']
        cause = {'memory_pressure': '継続的なメモリ圧迫',
                 'insufficient_cgroup_ram': 'サービスのメモリ制限内の余裕不足',
                 'oom_observed': 'OOMカウンター増加',
                 'allocation_failed': 'RAM割り当て失敗',
                 'gpu_allocation_failed': 'GPUメモリ割り当て失敗',
                 'profile_memory_budget_exceeded': 'モデルのVRAM予算超過',
                 'insufficient_vram': 'VRAM安全余裕不足',
                 'memory_safety_telemetry_unavailable': '安全条件の計測不能',
                 'insufficient_ram': 'RAM余裕不足',
                 'insufficient_swap_reserve': 'Swap余裕不足'}.get(reason, 'メモリ安全条件の不一致')
        message = (f"{phase}に{cause}を検出し、生成を見合わせました。MemAvailable {details['ram_available_mib']} MiB / "
                   f"必要 {details['required_ram_mib']} MiB。所有モデルの停止確認・隔離状態と診断ログを確認してください。")
    return {'code': code, 'message': message, 'resource': details}
