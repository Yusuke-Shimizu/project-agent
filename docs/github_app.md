# GitHub App の用意（起案 Lambda 用）

書き戻し（起案）は **GitHub App のインストールトークン**で PR を立てる。
**App の作成はブラウザでしかできない**（作成用の REST API が無い）ので、ここだけ手作業。

設計の背景は `writeback_pr_tool.md` の §6・§7。要点は2つ。

- **Runtime にはこの資格情報を与えない。** 読めるのは起案 Lambda の実行ロールだけで、
  それが「エージェント自身は正本に書けない」の物理的な裏返しになる
- **App に与える権限は最小。** `main` は正本リポジトリ側の ruleset で守る
  （PR 必須・承認1件・必須チェック2つ）。App は bypass 対象ではないので、
  **人の承認なしにマージされる経路が存在しない**

## 1. App を作る

<https://github.com/settings/apps/new>

| 項目 | 値 |
|---|---|
| GitHub App name | `kai-writeback`（**全 GitHub で一意**。取られていたら適当に足す） |
| Homepage URL | `https://github.com/Yusuke-Shimizu/kai-knowledge` |
| Webhook → Active | **チェックを外す**（こちらから叩くだけなので受け口は不要） |
| Repository permissions → **Contents** | **Read and write** |
| Repository permissions → **Pull requests** | **Read and write** |
| Where can this GitHub App be installed? | **Only on this account** |

**これ以上の権限を与えないこと。** とくに Actions / Workflows / Administration は不要。
`Metadata: Read-only` は自動で付く。

> **`Contents: Read and write` があると原理的に `main` へ直接 push できる。**
> GitHub App の権限に「既定ブランチ以外だけ書ける」は無いので、ここは ruleset 側
> （PR 必須）で閉じている。App を bypass actor に入れないこと。

## 2. App ID と秘密鍵を取る

- 作成後の設定画面に出る **App ID** を控える
- 同じ画面の下の **Private keys → Generate a private key** で `.pem` が落ちてくる

**`.pem` はリポジトリに置かない。**`.gitignore` で `*.pem` は除外済みだが、
Secrets Manager に入れたら手元からも消す。

## 3. 正本リポジトリにインストールする

設定画面の **Install App** → 自分のアカウント → **Only select repositories** →
`kai-knowledge` だけを選ぶ。

## 4. 箱を作って値を入れる

スタックは**箱だけ作って値は入れない**（`KaiSlackStack` と同じ扱い。CDK に鍵を通す
経路を作らないこと自体が防御になる）。

```sh
npx -y aws-cdk@2.1134.0 deploy KaiWritebackStack
```

値は**本人が**入れる。改行を含む PEM をそのまま JSON にするので `jq --rawfile` を使う:

```sh
aws secretsmanager put-secret-value --secret-id kai/github-app \
  --secret-string "$(jq -n --arg id '<App ID>' \
    --rawfile key ~/Downloads/<落ちてきた>.pem '{app_id:$id, private_key:$key}')"
```

入れ終わったら `.pem` を消す:

```sh
rm ~/Downloads/<落ちてきた>.pem
```

## 5. 確かめる

**エージェントも Gateway も通さない**（L4a の判定）。

```sh
# 受け付けない側だけ。GitHub にも AWS にも触らない
uv run python code/scripts/check_propose.py --dry-run

# 実際に PR を立てる
uv run python code/scripts/check_propose.py --append KNW-006
```

立った PR で確かめること:

- ラベル `proposed-by-agent` が付いている
- **`Merge` が押せない**（承認1件必須・必須チェック2つ）＝ 設計どおり
- 差分が**末尾への追記だけ**で、front matter が1バイトも変わっていない
- PR 本文に根拠の doc_id と出どころ（Slack の permalink）と `Requested-by` が載っている

確認用の PR は**閉じてよい**（マージすると確認用の文が正本に入る）。

## 6. まだやっていないこと

L4a はここまで。**Gateway には登録していないので、エージェントからは見えない**
（台本 4 問の挙動は変わらない）。次は L4b で `tools/list` に出す。
