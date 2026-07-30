#!/usr/bin/env python
"""README にその場で鳴らせるプレイヤーを埋め込むための下ごしらえ。

**GitHub は README の `<audio>` を消し、mp3 ファイル単体の blob ページにも
再生プレイヤーを付けない。**一方、GitHub の Web エディタにドラッグ&ドロップで
アップロードした動画は `https://github.com/user-attachments/assets/<hash>` という
URL が発行され、**その URL を裸のまま markdown に置くだけでページ内蔵のプレイヤーが
自動描画される** (video タグを書く必要すらない)。

そこで音声を「キャラのタイル画像を静止画にした動画」に変換してからアップロードする。
アップロード自体は GitHub の内部 API (ブラウザのセッション認証が要る) でしか行えず、
`gh` の REST API では発行できない。**このスクリプトは動画を作るところまで**を担い、
アップロードはブラウザ自動化 (`docs/embed_upload_runbook` 相当の手順、実体は
Claude Code のセッションで手動 orchestrate) で行う。

    python make_embeds.py                  # SAMPLES 全行ぶん manifest 順に作る
    python make_embeds.py --chars noa      # 一部だけ作り直す

出力は `samples/_embeds/` (**リポジトリには入れない** — 動画はアップロード後の
URL 取得にしか使わず、配布物としては mp3 で足りるため) と
`samples/_embeds/manifest.json` (アップロード順と対応付けるための一覧)。
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import make_samples as ms
import app

OUT_DIR = "samples/_embeds"
#: 正方形のピクセル数。**GitHub は `<video width=...>` を無効化する**ので、
#: 表示サイズは動画そのものの解像度でしか制御できない (実測: width 属性を
#: 付けても無視されてフル幅で出た)。表のセルに収まる大きさとして 120 を採用。
VIDEO_SIZE = 120
AUDIO_BITRATE = "96k"     # 小さい表示サイズに合わせてビットレートも下げる


def repo() -> Path:
    return Path(__file__).resolve().parent


def ref_wav_path(row: dict) -> Path:
    return repo() / "refs" / row["char"] / f"{row['ref']}.wav"


def audio_duration(audio: Path) -> float:
    """音声の長さ (秒) を ffprobe で取る。"""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(audio)],
        check=True, capture_output=True, text=True).stdout.strip()
    return float(out)


def make_mp4(image: Path, audio: Path, out: Path) -> None:
    """静止画 + 音声の mp4 を作る。

    **映像の長さを `-t` で音声に揃える。**`-loop 1` の静止画は無限に伸びるので
    `-shortest` で止めたつもりだったが**効かず、映像が音声より 14 秒長い mp4**が
    出来ていた (実測: 映像 33.5s / 音声 19.4s)。長さの食い違う mp4 は再生側が
    音声トラックを正しく扱えないことがある。**`-t` で明示するのが確実。**
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "ffmpeg", "-y", "-loop", "1", "-i", str(image), "-i", str(audio),
        "-c:v", "libx264", "-tune", "stillimage", "-pix_fmt", "yuv420p",
        "-vf", f"scale={VIDEO_SIZE}:{VIDEO_SIZE}",
        "-c:a", "aac", "-b:a", AUDIO_BITRATE,
        "-t", f"{audio_duration(audio):.3f}", str(out),
    ], check=True, capture_output=True)


def build(rows: tuple[dict, ...]) -> list[dict]:
    """**manifest はアップロード順そのもの。**この順序を崩さないこと —
    ブラウザ自動化側はファイルを順に投げて URL を回収するだけで、
    どの URL がどのファイルかはこの並びでしか分からない。
    """
    out = repo() / OUT_DIR
    manifest: list[dict] = []
    for row in rows:
        char = row["char"]
        # 参照音声: タイルは「通常」の絵で固定 (崩壊させた声を壊れた絵と一緒に
        # 聴かせると、素の声だと誤解されるため)
        ref_img = repo() / ms.tile_for(char, ms.fx.EASY_MIN)
        ref_mp4 = out / f"{char}_ref.mp4"
        make_mp4(ref_img, ref_wav_path(row), ref_mp4)
        manifest.append({"char": char, "kind": "ref", "mp4": str(ref_mp4)})

        for idx in sorted(row["seeds"]):
            src = ms.sample_path(char, idx)
            if not src.is_file():
                continue
            pos = ms.pos_of(row, idx)
            img = repo() / ms.tile_for(char, pos)
            slug = ms.TIER_SLUGS[idx]
            mp4 = out / f"{char}_{slug}.mp4"
            make_mp4(img, src, mp4)
            manifest.append({"char": char, "kind": slug, "mp4": str(mp4)})

    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--chars", help="対象キャラをカンマ区切りで絞る")
    args = ap.parse_args()

    rows = ms.SAMPLES
    if args.chars:
        want = {s.strip() for s in args.chars.split(",") if s.strip()}
        rows = tuple(r for r in ms.SAMPLES if r["char"] in want)

    manifest = build(rows)
    for m in manifest:
        print(f"{m['char']:14} {m['kind']:6} {m['mp4']}")
    print(f"\n{len(manifest)} 本を samples/_embeds/ に作成。"
          f"manifest: samples/_embeds/manifest.json")


if __name__ == "__main__":
    main()
