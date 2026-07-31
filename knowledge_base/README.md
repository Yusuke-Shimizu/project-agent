# knowledge_base — デモ用ダミー案件データ（完全架空）

Bedrock Knowledge Base のデータソースになる正本。実在の顧客名・案件名・個人名は
一切含まない、登壇デモ専用の架空データ。

## 架空案件の設定

- コードネーム: **KAI**
- 内容: BtoB 向け在庫管理 SaaS の刷新（レガシー基盤 → AWS モダナイズ）
- 想定チーム: PM 1・設計/開発 4・SRE 1
- 期間: 2025-06 キックオフ 〜 2026 リリース想定

## ディレクトリ

| パス | 役割 |
| --- | --- |
| `decisions/` | 正式な意思決定。1 ファイル = 1 決定 |
| `knowledge/` | 案件固有の制約・暗黙知 |
| `meetings/` | 議事録。**正式 decision に昇格していない検討メモ**を含む |

## front matter

全ファイルが YAML front matter を持つ。エージェントは本文だけでなくこのメタデータを
根拠の提示（doc_id・日付・状態）に使う。

```yaml
doc_id: DEC-003a          # ファイル名と一致させる
type: decision            # decision | knowledge | meeting
date: 2025-07             # decision のみ。決定日
status: superseded        # active | superseded | draft
title: 非同期処理基盤に EventBridge + Step Functions
supersedes: null          # decision のみ。置換した側の doc_id
superseded_by: DEC-003b   # decision のみ。置換された側から見た後継
```

`status` の意味:

- `active` — 現行の正本
- `superseded` — 後続の decision に置換済み。`superseded_by` を辿る
- `draft` — 検討段階。**正本として扱ってはいけない**（meetings が主にこれ）

## データに仕込んだ仕掛け

デモで見せたい挙動を引き出すため、意図的に以下を埋め込んである。データを追加・修正
するときはこの構造を壊さないこと。

1. **矛盾（未昇格）** — DEC-004（共有スキーマ + tenant_id, active）と
   MTG-2026-03-15（物理分離の検討, draft）。検討メモを決定と取り違えないか
2. **superseded** — DEC-003a（Step Functions）→ DEC-003b（SQS + Lambda）。
   古い決定を現行方針として返さないか
3. **暗黙知の想起** — KNW-002（PartnerSync API のレート制限・過去事故）。
   明示的に問われなくても関連する制約を引けるか
4. **根拠なしの明言** — 監視ツール（Datadog 等）に関する decision は存在しない。
   「根拠がない」と言えるか、それとも埋めてしまうか

当日の台本と、各問がどのスライドに対応するかは登壇資料側の `demo/demo_script.md` にある。
