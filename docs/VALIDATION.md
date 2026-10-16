# 公開準備版の検証

2026-10-16、独立した公開コピーと専用venvで実行。

- pytest：**945 PASS / 8 SKIP**。
- HOME/runtimeは専用一時領域、実APIキー・本番環境変数を継承しない。
- Pythonの外向きDNS/接続を拒否し、loopbackの隔離HTTP試験だけを許可。
- GPU/LLM/クラウドはmock。追加の実GPU生成や本番サービス再起動は行っていない。
- 非公開saved-run、実モデル、正式隔離環境が必要なケースは条件付きレーンとしてSKIP。合格へ読み替えない。
- ソース再承認の安全境界テストでは人工のディレクトリ/状態とmock runnerを使用し、実sudoを実行しない。

再現：`uv sync --locked --group dev` → `uv run --locked --group dev python -m pytest -q`。
外向き通信を禁止した試験結果であり、通常利用時すべての外部通信を禁止する仕組みの証明ではありません。

公開コピーの変更：依存ロック、個人パスの除去、private fixtureの明示指定、旧GPU経路の初期拒否。
Workの日本語長文PTY入力はreadlineが始まる前の行長制限を避け、端末設定をfinallyで復元します。
Chatは945 PASS/7 SKIPだった本番の過去結果とは別の公開コピー結果です。

実モデルの過去実績：LTX I2V 1本の成功はありますが、180秒の実生成と長い連続I2V品質は未検証。
公開準備時にモデルや生成物を再取得・同梱していません。Privateへのpush後の履歴・一覧照合は別の監査記録に保存します。


資格情報を継承せず専用HOME/runtimeで実行する入口は
`uv run --locked --group dev python scripts/test_release.py`です。
GitHub Actionsはworkflow_dispatchのみで、自動push/PR実行はありません。
Actions自体はこの準備作業で起動していません。手動起動時の利用枠・課金条件はアカウントで確認してください。
