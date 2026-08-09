"""テストから `code/scripts/` のスクリプトを import できるようにする。

`code/tools` と `code/runtime` はパッケージとして editable install されているが
（`pyproject.toml` の `[tool.hatch.build.targets.wheel]`）、`code/scripts/` は
入口スクリプトの置き場なのでパッケージにしていない。テストで直接呼べるように
ここでパスだけ通す。
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "code" / "scripts"))
# code/slack/ も同じ理由でパッケージにしていない（Lambda の入口の置き場）
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "code" / "slack"))
