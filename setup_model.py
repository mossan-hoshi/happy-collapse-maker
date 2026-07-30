#!/usr/bin/env python
"""モデルを取ってくる。**初回セットアップはこれ 1 本で済ませる。**

    python setup_model.py                 # ./model へ落とす
    python setup_model.py --dest D:/foo   # 置き場所を変える
    python setup_model.py --check         # 落とさず、揃っているかだけ見る

必要なもの:

- **Hugging Face のアカウントとトークン**。ここだけは手動 (ブラウザでの発行が要る)。
  https://huggingface.co/settings/tokens で read 権限のトークンを作り、
  `HF_TOKEN` 環境変数に入れるか、`huggingface-cli login` を一度実行しておく。
  **公開モデルなのでトークン無しでも落ちることが多い**が、匿名だと
  レート制限に当たりやすい。

`huggingface_hub` が入っていなければ自動で入れる。
再実行は安全 (既にあるファイルは再ダウンロードしない)。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO_ID = "Qwen/Qwen3-TTS-12Hz-1.7B-Base"
DEFAULT_DEST = "model"

#: これが全部あればモデルとして使える。**`--check` の判定に使う。**
#: 落とし損ねに気付かず起動して、意味の分からない例外で落ちるのを防ぐ。
REQUIRED = ("config.json",)
MIN_TOTAL_MB = 3000        # 実測 3.4GB。半端に落ちたのを「ある」と判定しない


def ensure_hub() -> None:
    try:
        import huggingface_hub  # noqa: F401
        return
    except ImportError:
        pass
    print("huggingface_hub が無いので入れます...")
    # uv 製の venv には pip が無いことがあるので、両方を試す。
    for cmd in ([sys.executable, "-m", "pip", "install", "-U", "huggingface_hub"],
                ["uv", "pip", "install", "--python", sys.executable, "huggingface_hub"]):
        try:
            subprocess.run(cmd, check=True)
            return
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
    raise SystemExit("huggingface_hub を入れられませんでした。手動で入れてください。")


def total_mb(d: Path) -> int:
    return int(sum(p.stat().st_size for p in d.rglob("*") if p.is_file()) / 1024**2)


def looks_complete(d: Path) -> tuple[bool, str]:
    if not d.is_dir():
        return False, "フォルダが無い"
    missing = [f for f in REQUIRED if not (d / f).is_file()]
    if missing:
        return False, f"足りない: {', '.join(missing)}"
    mb = total_mb(d)
    if mb < MIN_TOTAL_MB:
        return False, f"容量が足りない ({mb}MB < {MIN_TOTAL_MB}MB)。途中で切れている"
    return True, f"{mb}MB"


def main() -> int:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", default=str(here / DEFAULT_DEST), help="置き場所")
    ap.add_argument("--repo", default=REPO_ID)
    ap.add_argument("--check", action="store_true", help="落とさず確認だけする")
    args = ap.parse_args()

    dest = Path(args.dest).resolve()
    ok, why = looks_complete(dest)
    if ok:
        print(f"モデルは揃っています: {dest} ({why})")
        return 0
    if args.check:
        print(f"モデルが不完全です: {dest} — {why}")
        return 1

    print(f"{args.repo} を {dest} へ落とします (約 3.4GB)。")
    if not os.environ.get("HF_TOKEN"):
        print("  ※ HF_TOKEN が未設定です。公開モデルなので通常は落ちますが、"
              "レート制限に当たったらトークンを設定してください。")

    ensure_hub()
    from huggingface_hub import snapshot_download

    snapshot_download(
        repo_id=args.repo,
        local_dir=str(dest),
        # **シンボリックリンクを作らせない。**Windows では管理者権限が要り、
        # PyInstaller で固めた配布物へ持っていくときにも壊れる。
        local_dir_use_symlinks=False,
        max_workers=4,
    )

    ok, why = looks_complete(dest)
    print(("完了: " if ok else "不完全: ") + f"{dest} ({why})")
    if ok:
        print("\n起動:  python app.py")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
