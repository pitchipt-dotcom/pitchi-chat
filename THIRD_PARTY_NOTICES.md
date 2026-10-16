# 第三者依存と由来

自作コードはLICENSING.mdの範囲でMITです。以下は依存のmetadata表示です。ライブラリ本体やモデルweightsを同梱しません。

| Package | Version | Upstream license metadata |
|---|---|---|
| [annotated-doc](https://pypi.org/project/annotated-doc/0.0.5/) | 0.0.5 | MIT |
| [annotated-types](https://pypi.org/project/annotated-types/0.8.0/) | 0.8.0 | MIT |
| [anyio](https://pypi.org/project/anyio/4.15.1/) | 4.15.1 | MIT |
| [attrs](https://pypi.org/project/attrs/26.1.0/) | 26.1.0 | MIT |
| [certifi](https://pypi.org/project/certifi/2026.7.22/) | 2026.7.22 | MPL-2.0 |
| [click](https://pypi.org/project/click/8.5.0/) | 8.5.0 | BSD-3-Clause |
| [colorama](https://pypi.org/project/colorama/0.4.6/) | 0.4.6 | BSD-3-Clause (upstream LICENSE.txt) |
| [fastapi](https://pypi.org/project/fastapi/0.142.2/) | 0.142.2 | MIT |
| [h11](https://pypi.org/project/h11/0.16.0/) | 0.16.0 | MIT |
| [httpcore](https://pypi.org/project/httpcore/1.0.9/) | 1.0.9 | BSD-3-Clause |
| [httpx](https://pypi.org/project/httpx/0.28.1/) | 0.28.1 | BSD-3-Clause |
| [idna](https://pypi.org/project/idna/3.20/) | 3.20 | BSD-3-Clause |
| [iniconfig](https://pypi.org/project/iniconfig/2.3.0/) | 2.3.0 | MIT |
| [jsonschema](https://pypi.org/project/jsonschema/4.26.0/) | 4.26.0 | MIT |
| [jsonschema-specifications](https://pypi.org/project/jsonschema-specifications/2025.9.1/) | 2025.9.1 | MIT |
| [opentelemetry-api](https://pypi.org/project/opentelemetry-api/1.45.0/) | 1.45.0 | Apache-2.0 |
| [packaging](https://pypi.org/project/packaging/26.3/) | 26.3 | Apache-2.0 OR BSD-2-Clause |
| [pillow](https://pypi.org/project/pillow/12.3.0/) | 12.3.0 | MIT-CMU |
| [pluggy](https://pypi.org/project/pluggy/1.6.0/) | 1.6.0 | MIT |
| [pydantic](https://pypi.org/project/pydantic/2.13.5/) | 2.13.5 | MIT |
| [pydantic-core](https://pypi.org/project/pydantic-core/2.46.5/) | 2.46.5 | MIT |
| [pygments](https://pypi.org/project/pygments/2.21.0/) | 2.21.0 | BSD-2-Clause |
| [pytest](https://pypi.org/project/pytest/9.1.1/) | 9.1.1 | MIT |
| [referencing](https://pypi.org/project/referencing/0.37.0/) | 0.37.0 | MIT |
| [rpds-py](https://pypi.org/project/rpds-py/2026.9.1/) | 2026.9.1 | MIT |
| [starlette](https://pypi.org/project/starlette/1.7.0/) | 1.7.0 | BSD-3-Clause |
| [typing-extensions](https://pypi.org/project/typing-extensions/4.16.0/) | 4.16.0 | PSF-2.0 |
| [typing-inspection](https://pypi.org/project/typing-inspection/0.4.4/) | 0.4.4 | MIT |
| [uvicorn](https://pypi.org/project/uvicorn/0.54.0/) | 0.54.0 | BSD-3-Clause |

依存を再配布する場合は各配布物に付属するライセンス全文とNOTICEの条件を確認してください。
自作Host/adapterと、接続先サービス・モデル・付属workflowの条件は別です。
前身のローカルプロジェクトから抽出した自作部分を含みます。権利者の承認に基づくMITの対象は自作部分に限り、第三者の権利・表示は変更しません。
テストの書誌fixtureは公開タイトル/公式リンク等のメタデータだけです。資料本文・個人蔵書・運用DBではありません。
[モデル・外部ソフトの条件](docs/MODEL_LICENSES.md)も参照してください。

ColoramaのPyPI metadataは未指定だったため、[upstream LICENSE.txt](https://github.com/tartley/colorama/blob/master/LICENSE.txt)でBSD-3-Clauseの条件を確認しました。
