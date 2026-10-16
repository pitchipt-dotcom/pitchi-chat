# 外部ソフトとモデルのライセンス

本体コードのライセンスは未決定です。以下のモデル・サービスは別途導入し、weights/実行ファイル/生成物をこのrepoから配布しません。

| 依存・モデル | 確認した公開元・条件 | このrepoでの扱い |
|---|---|---|
| ComfyUI | [公式LICENSE](https://github.com/Comfy-Org/ComfyUI/blob/master/LICENSE)、GPLv3 | 外部API。ComfyUI/nodeソースは同梱しない |
| FFmpeg | [公式案内](https://ffmpeg.org/legal.html)、ビルド構成によりLGPL/GPL等が異なる | CLI外部依存、バイナリは配布しない |
| FLUX.2 Klein **4B** | [公式model card](https://huggingface.co/black-forest-labs/FLUX.2-klein-4B)、Apache-2.0 | weights/encoder/VAEを別導入。9Bや他版へ条件を流用しない |
| Qwen-Image（初代） | [公式配布元](https://huggingface.co/Qwen/Qwen-Image)、Apache-2.0表示 | 初代と派生/後継版の条件は別確認 |
| LTX-Video 2B 0.9.8 distilled / FP8 | [公式model card](https://huggingface.co/Lightricks/LTX-Video)が[Open Weights License 0.X](https://huggingface.co/Lightricks/LTX-Video/blob/main/LTX-Video-Open-Weights-License-0.X.txt)を指定 | Apache/MITと扱わない。用途・商用条件等の全文を利用者が確認 |
| Strata / Qwen派生・Uncensored / Vision / H3 / 音声等 | 実際のモデル名・配布元・派生版・encoder/VAEごとの条件が必要 | API adapterだけを配布。全weightsの権利確認済みとは主張しない |

モデルの名称だけで再配布や商用利用を許可しません。LTXの利用制限・商用条件や、FLUXの版ごとの差を確認してください。
独自に作成したAPI graphを使いますが、第三者からworkflow/nodeコードを取り込む際は由来と条件を別途記録してください。
モデルを自動ダウンロードするinstall処理はありません。
