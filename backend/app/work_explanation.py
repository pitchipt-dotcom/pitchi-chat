"""Presentation only: established Work facts become bounded Japanese wording.

No free paraphrase is accepted: an optional local model selects one of two
Host-reviewed equivalent expressions. Raw results never enter the model.
"""
import asyncio
from collections import OrderedDict
import hashlib
import json
import re


LABELS = {'RUNNING': '作業中', 'COMPLETED': '完了', 'FAILED': '完了できませんでした',
          'NEEDS_HUMAN': '確認が必要です', 'NEEDS_CAPABILITY': '必要な機能が不足しています'}


def explanation(result):
    status = result.get('status')
    message = {
        'RUNNING': '依頼された作業を進めています。',
        'COMPLETED': '作業が完了しました。',
        'FAILED': '今回の作業を完了できませんでした。完成扱いにはしていません。',
        'NEEDS_HUMAN': '作業を続ける前に、人間による確認が必要です。',
        'NEEDS_CAPABILITY': '現在のPitchi-workには、この依頼を完了するための機能が不足しています。',
    }.get(status, '現在の作業状況を確認できません。')
    if result.get('tool_task'):
        message = ('登録・検証を完了できませんでした。失敗の詳細を保存しています。本番への反映は完了扱いにしていません。' if status == 'FAILED' else result.get('message') or message)
    facts = ['status']
    if status == 'FAILED' and result.get('failure_class') == 'HOST_PROFILE_BROWSER_INTERACTION_FAIL':
        message = '最後の画面操作の確認で問題が見つかったため、作業を完成扱いにはしていません。'
        facts.append('failure_class')
        try:
            evidence = json.loads(result.get('message', ''))
        except (ValueError, TypeError):
            evidence = {}
        observations = evidence.get('observations') if isinstance(evidence, dict) else None
        if isinstance(observations, list) and any(
            isinstance(row, dict) and isinstance(row.get('errors'), list)
            and 'Visible app and registered interaction elements required' in row['errors']
            for row in observations
        ):
            message = ('動作確認で、アプリが画面に表示されていることと、操作に使う登録済みの要素を'
                       '確認できませんでした。そのため完成条件を満たさず、完成扱いにはしていません。'
                       '確認できなかった理由までは、この証拠だけでは断定できません。')
            facts.append('message.observations.errors')
    if status == 'FAILED' and result.get('native_status') == 'STOPPED_BY_USER':
        message = '作業を中止しました。完成扱いにはしていません。'
        facts.append('native_status')
    if status == 'COMPLETED' and (result.get('artifact') or {}).get('preview_url'):
        message += '「開く」から成果物を確認できます。'
        facts.append('artifact.preview_url')
    advice = result.get('capability_advice') or {}
    missing = advice.get('missing_capabilities') or []
    if status == 'NEEDS_CAPABILITY' and any(
        isinstance(item, dict) and item.get('name') == 'python_new_file_creation' for item in missing
    ):
        message = ('新しいPythonプログラムを安全に作成・検証する機能が不足しているため、'
                   'この作業を続けられませんでした。')
        facts.append('capability_advice.missing_capabilities.name')
    value = {'title': LABELS.get(status, '状況を確認中'), 'message': message,
             'source': 'template', 'fact_fields': facts}
    if status == 'NEEDS_HUMAN':
        confirmation = result.get('human_confirmation') or {}
        def natural(text):
            return isinstance(text, str) and 0 < len(text) <= 400 and bool(re.fullmatch(
                r'[\u3000-\u30ff\u3400-\u9fff\uff01-\uffef0-9 、。？！「」・（）\n]+', text))
        if natural(confirmation.get('question')):
            value['question'] = confirmation['question']
            facts.append('human_confirmation.question')
        choices = confirmation.get('choices')
        if isinstance(choices, list) and len(choices) <= 10 and all(natural(c) for c in choices):
            value['choices'] = list(choices)
            facts.append('human_confirmation.choices')
    return value


class HumanExplanationLayer:
    def __init__(self, selector=None, timeout=10):
        self.selector = selector
        self.timeout = timeout
        self.cache = OrderedDict()
        self.lock = asyncio.Lock()

    async def explain(self, result):
        base = explanation(result)
        if self.selector is None or result.get('status') == 'RUNNING':
            return base
        # Cache explanations only in bounded request memory, not history or Work.
        digest = hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        async with self.lock:
            if digest in self.cache:
                self.cache.move_to_end(digest)
                return dict(self.cache[digest])
            variants = [base['message'], '状況をお知らせします。' + base['message']]
            try:
                choice = await asyncio.wait_for(self.selector(variants), self.timeout)
                # Strict enum, not a model-authored sentence/status/recommendation.
                if choice in ('0', '1'):
                    base = {**base, 'message': variants[int(choice)], 'source': 'local_wording_selection'}
            except Exception:
                pass  # Model unavailable/invalid/timeout: facts and template stay intact.
            self.cache[digest] = dict(base)
            if len(self.cache) > 64:
                self.cache.popitem(last=False)
            return base
