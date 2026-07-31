---
doc_id: DEC-003b
type: decision
date: 2025-08
status: active
title: 非同期処理は SQS + Lambda に変更
supersedes: DEC-003a
superseded_by: null
---

# DEC-003b: 非同期処理は SQS + Lambda に変更

**決定日**: 2025-08 / **状態**: active

## 決定
非同期処理基盤を Amazon SQS + AWS Lambda に変更する。

## 理由
- 実要件はシンプルなキュー処理で充足でき、Step Functions の状態遷移コスト・運用複雑性が過剰と判明
- 運用チームの学習コストとコストの両面で SQS+Lambda が優位

## 経緯
DEC-003a（EventBridge + Step Functions）を見直し、本決定で置換した。
