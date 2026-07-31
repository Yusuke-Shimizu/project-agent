---
doc_id: DEC-003a
doc_type: decision
title: 非同期処理基盤に EventBridge + Step Functions
date: 2025-07-24
status: superseded
supersedes: null
superseded_by: DEC-003b
decided_by: アーキテクト
owner: SREリード
review_by: 2026-12-31
topic: async
---

# DEC-003a: 非同期処理基盤に EventBridge + Step Functions

**決定日**: 2025-07-24 / **状態**: superseded

## 決定（※後に置換）
非同期処理・バッチワークフローの基盤に Amazon EventBridge + AWS Step Functions を採用する。

## 理由
- ワークフローの状態遷移を可視化でき、複雑な補償処理を組みやすい

## 注記
本決定は **DEC-003b により置換（superseded）** された。現行方針は DEC-003b を参照。
