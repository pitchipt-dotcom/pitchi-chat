# 設定と外部依存

## Chat

`config/settings.toml` / `resources.toml` / `services.toml` / `images.toml` / `work.toml`等は安全なテンプレートです。
endpointやowned profileが未設定なら対象処理は使えません。実行ファイル・API形式・必要メモリを実測して設定します。
`.local.toml`や`.local.env`はignoredです。実鍵をテンプレートへ書かないでください。
Strata、Vision、ComfyUIの同時占有を避け、原則1生成・単一worker・共有キューで運用します。
メモリ圧迫やcgroupを含む既存の安全判定は維持し、swap空きだけで解決しようとしないでください。
画像/動画/音声のサービスURLとprofileは各テンプレートのfieldを確認します。workflowやモデル名は自分の環境で検証してください。

## Work

通常CLIの`--endpoint`と`--model`で接続先を明示できます。localのHTTP endpointはloopbackに限定されます。
Chatの共通Strataを使う場合、`STRATA_ENV_FILE`が指すローカルファイルまたは環境変数で設定します。

```dotenv
STRATA_BASE_URL=http://127.0.0.1:8000/api/models/strata/v1
STRATA_MODEL=YOUR_CONFIGURED_MODEL
# STRATA_SHARED_TOKEN is a locally generated secret; never commit its value.
```

資格情報の実値はこの例に含めません。ファイルは利用者だけが読める権限にし、Git対象外に置きます。
LLMを使うjobではleaseを取得し、工程間も保持し、有限heartbeatで失効します。
承認済みsourceを変更したらsource pinを再レビューします。Chat本番の設定やpinをこの公開コピーへコピーしないでください。

旧BONSAI GPU managerに依存する検索改善workflowは初期無効です。
`PITCHI_ENABLE_LEGACY_GPU_WORKFLOW=1`は既存依存とscope・停止復旧をレビューした管理者だけが指定する任意設定です。
公開coreの導入に旧BONSAI repoは不要で、条件不成立時に別モデルへ自動fallbackしません。

## 任意ツール

FFmpeg/FFprobe、ComfyUI、Tesseract、Node/Chrome、bubblewrap、systemd、PHP、SSHは利用機能に応じて別導入です。
モデル本体やComfyUI用のTorch/CUDAをHostのvenvに混ぜないでください。
Cloud SDKはoptionalで、キー・課金通信の承認が別途必要です。


## 正式なWorkソース再承認

Chatの`reapprove_pitch_work.py`を使う正式検証には、利用者が保管する既存のsaved-run回帰を明示指定します。
`PITCHI_PARTIAL_RUN`と`PITCHI_SAVED_RUN`を設定し、private completed-runのstateが参照するworkspaceも用意してください。
この検証は入力をread-onlyに扱い、未設定時は拒否します。公開のportableテスト成功だけで本番source pinを承認しません。
任意の隔離回帰を省略するために安全条件やroot acceptanceを変更しないでください。
