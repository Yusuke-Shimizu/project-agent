# project-agent

## セットアップ

clone 後に一度だけ実行する。どちらも git config / direnv のローカル設定なので、
リポジトリに設定ファイルが入っていても自動では有効にならない。

```sh
direnv allow                          # .envrc を許可する
git config core.hooksPath .githooks   # pre-commit hook を有効にする
```

依存ツール:

```sh
brew install direnv gitleaks
```

`direnv` は shell hook の設定も必要（`eval "$(direnv hook zsh)"` を `.zshrc` に）。

## AWS

このリポジトリでの作業は AWS の `personal` プロファイルを既定で使う。
`--profile` を毎回指定する必要はない。

| ファイル | 効く範囲 |
| --- | --- |
| [.envrc](.envrc) | ターミナル。direnv がこのディレクトリで `AWS_PROFILE` を設定する |
| [.claude/settings.json](.claude/settings.json) | Claude Code が実行するコマンド |

`.claude/settings.json` の変更はセッションを開始し直すまで反映されない。

## 秘匿情報の扱い

**public リポジトリなので、認証情報をコミットしないこと。**

環境変数として秘匿値が必要になったら `.envrc.local` に書く。gitignore 済みで、
direnv が `.envrc` から自動で読み込む。

多層で防いでいる:

1. [.gitignore](.gitignore) — `.env*`、鍵ファイル、`.envrc.local` を追跡対象から外す
2. [.githooks/pre-commit](.githooks/pre-commit) — コミット前に gitleaks でステージ済み差分を検査する
3. [.gitleaks.toml](.gitleaks.toml) — 検出ルール。gitleaks の既定ルールは AWS
   アクセスキーと秘密鍵ブロックを検出しないため、ここで補っている
4. GitHub の secret scanning / push protection — push 時のサーバー側チェック

pre-commit hook は `--no-verify` で回避でき、clone 先では設定するまで動かない。
事故防止であって保証ではないので、最後の砦は GitHub 側という前提で扱う。

gitleaks が誤検知したときは、該当行末に `gitleaks:allow` コメントを付けるか、
出力された Fingerprint を `.gitleaksignore` に追記する。
