#!/usr/bin/env python
"""アプリのタイトルロゴを Nano Banana 2 で作る。

    python make_logo.py [--variants 3] [--out assets/logo.png]
    python make_logo.py --fit-only          # 生成せず、いまの logo.png を整えるだけ

**Nano Banana 2 (`gemini-3.1-flash-image`) を使う。lite は使わない。**
ロゴには日本語を焼き込むので、それがきれいに出るモデルが要る (lite は崩れる)。

API キーは `GEMINI_API_KEY` env から取る。Secret Manager に置いている場合は
`GEMINI_API_KEY_PROJECT` にプロジェクト ID を入れれば gcloud 経由で取りに行く
(シークレット名は `GEMINI_API_KEY_SECRET`、既定 `GEMINI_API_KEY`)。

生成物は**必ず `fit_logo()` を通す**。理由は同関数の docstring を読むこと。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

MODEL_NAME = "gemini-3.1-flash-image"      # Nano Banana 2 (lite ではない)

#: Secret Manager から API キーを取るときの参照先。**リポジトリに自分の
#: プロジェクト ID を焼き込まない** — 公開すると実在の ID が晒されるうえ、
#: 他人のフォークでは意味の無い値になる。使う人が env で渡す。
#: 未設定なら Secret Manager 経路は使わず、`GEMINI_API_KEY` を直接渡してもらう。
GCP_PROJECT_ID = os.environ.get("GEMINI_API_KEY_PROJECT", "").strip()
SECRET_NAME = os.environ.get("GEMINI_API_KEY_SECRET", "GEMINI_API_KEY").strip()

TITLE = "たのしい崩壊メーカー"

PROMPT = """ロゴを作ってください。

名前: たのしい🔞崩壊メーカー

求める水準:
プロのデザイナーが仕上げた商用品質。
置かれる場所は真っ黒に近いダークテーマなので、それに映えること。

**デザインはあなたに任せます。**構図・書体・装飾は自由に決めてください。

守ってほしいのはここだけ:
- 🔞 を中央に置き、名前の文字はその周りに配置する
  (🔞 は Unicode の絵文字そのものの意匠で)
- **グラデーションを使わない。**単色で塗り、使う色数を絞る
- ロゴそのものだけを描くこと。台紙・枠・カード・タイルの中に閉じ込めない
- 日本語の字形が正しいこと。指定した名前以外の文字を足さないこと
"""


#: 中央の 🔞 を描き直させるプロンプト。
#:
#: **フォントの絵文字を画像として渡してはならない。**ブラウザ等で表示した絵文字を
#: スクショして持ち込むと、その絵柄はフォントベンダー (Segoe UI Emoji / Noto など) の
#: 著作物になる。**絵文字は「書体」ではなく「絵」**として保護されうるので、
#: 「作者に権利があります」と宣言する自社ロゴに混ぜてはいけない。
#: モデルに描かせた意匠なら、その問題が起きない。
REDRAW_EMOJI_PROMPT = """1枚目はこのアプリのロゴです。

**中央にある「18禁マーク」だけを描き直してください。**
大きさ・位置・傾きはそのまま、**それ以外の部分 (周囲の文字・配色・背景) は
1枚目から一切変えないでください。**

描き直すマークの意匠:
- 赤い円のリングと、その中を左下から右上へ横切る赤い斜線 (禁止マーク)
- リングの内側は白く、中央に数字の「18」を濃色の太字で置く
- 立体感・光沢の付け方はロゴ全体のタッチに合わせる

**特定の絵文字フォントの意匠を再現しないでください。**上の説明だけに従って、
このロゴに合う意匠として描いてください。
"""


#: ロゴ本体と見なす条件。**背景の明度だけでは切れない** — 生成物の背景には
#: ビネットが乗っていて、隅の明度が 22 から 44 まで動く (実測)。
#: 本体は「明るい」か「彩度が高い」かのどちらかなので、その or で取る。
LOGO_LUM_MIN = 80          # 白い部分 (🔞 の円など) を拾う
LOGO_CHROMA_MIN = 45       # 濃いマゼンタ / シアン / 赤を拾う。背景は無彩色に近い
LOGO_SPECK_MAX = 200       # これ未満の孤立塊はノイズとして捨てる (px)
LOGO_LUM_MAX = 235         # **白背景のとき**に背景と見なす明度の下限


def fit_logo(src: Path, dst: Path | None = None, pad_ratio: float = 0.04) -> Path:
    """ロゴの**中身をキャンバスのど真ん中に置き直す**。

    Nano Banana は中身が偏った絵を返す (実測: 外接矩形で左に 13px、
    重心では 24px ずれていた)。**ヘッダ側で中央寄せしても画像の中身が
    ずれていれば中央には見えない**ので、画像そのものを直す。

    ついでに背景の黒を抜く。アプリの背景はグラデーションなので、
    黒い正方形を貼ると**ロゴではなく箱**に見えてしまう。
    抜きかたは「本体を取って穴を埋める」— 暗部を背景として抜くのではない。
    後者だと (a) 背景のビネットで隅が明るく残り、(b) 文字の内側の暗部
    (🔞 の "18" など) まで透ける。
    """
    from PIL import Image
    from scipy import ndimage

    import numpy as np

    dst = dst or src
    im = Image.open(src).convert("RGBA")
    a = np.asarray(im).astype(np.uint8)
    alpha = a[..., 3].copy()

    if alpha.min() == 255:                      # 不透明 = 生成直後の黒背景
        rgb = a[..., :3].astype(np.int16)
        lum = rgb.max(axis=2)
        chroma = lum - rgb.min(axis=2)
        body = (lum >= LOGO_LUM_MIN) | (chroma >= LOGO_CHROMA_MIN)

        lab, n = ndimage.label(body)            # 孤立した小塊 (JPEG ノイズ等) を捨てる
        if n:
            sizes = np.bincount(lab.ravel())
            body = np.isin(lab, np.nonzero(sizes >= LOGO_SPECK_MAX)[0][1:])
        body = ndimage.binary_fill_holes(body)  # 文字の内側を不透明のまま残す
        body = ndimage.binary_dilation(body, iterations=2)   # AA の縁ぶん広げる

        alpha = (body * 255).astype(np.uint8)
        alpha = ndimage.gaussian_filter(alpha, sigma=0.8)    # 縁のジャギを均す

    ys, xs = np.nonzero(alpha > 8)
    if xs.size == 0:
        raise SystemExit(f"中身が見つからない: {src}")

    rgba = np.dstack([a[..., :3], alpha])
    crop = rgba[ys.min(): ys.max() + 1, xs.min(): xs.max() + 1]
    ch, cw = crop.shape[:2]
    side = int(max(ch, cw) * (1 + pad_ratio * 2))
    out = np.zeros((side, side, 4), dtype=np.uint8)
    y0, x0 = (side - ch) // 2, (side - cw) // 2   # ← 等余白。これが中央寄せの実体
    out[y0:y0 + ch, x0:x0 + cw] = crop

    Image.fromarray(out, "RGBA").save(dst)
    print(f"整形: {src.name} → {dst}  中身 {cw}x{ch} → キャンバス {side}x{side}")
    return dst


def _find_gcloud() -> str:
    cands = ("gcloud.cmd", "gcloud") if os.name == "nt" else ("gcloud", "gcloud.cmd")
    for c in cands:
        if (path := shutil.which(c)):
            return path
    return ""


def fetch_api_key() -> str:
    if (env := os.environ.get("GEMINI_API_KEY", "").strip()):
        return env
    if not GCP_PROJECT_ID:
        raise SystemExit(
            "API キーがありません。GEMINI_API_KEY を env で渡すか、"
            "Secret Manager から取るなら GEMINI_API_KEY_PROJECT に"
            "プロジェクト ID を入れてください "
            f"(シークレット名は GEMINI_API_KEY_SECRET、既定 {SECRET_NAME})。")
    gcloud = _find_gcloud()
    if not gcloud:
        raise SystemExit("gcloud が PATH に無い。GEMINI_API_KEY を env で渡すこと。")
    out = subprocess.run(
        [gcloud, "secrets", "versions", "access", "latest",
         f"--secret={SECRET_NAME}", f"--project={GCP_PROJECT_ID}"],
        check=True, capture_output=True, text=True, encoding="utf-8")
    return out.stdout.strip()


#: README 本文中に貼る縮小版の出力先。
#: **`<img width=...>` だけで縮めない** — 1024px の png を毎回落とさせることになる
#: (このロゴは 300KB 超)。表示サイズぶんの実体を作って貼る。
THUMB_OUT = "assets/logo_small.webp"


def make_thumb(src: Path, dst: Path, px: int) -> str:
    """`src` から幅 `px` の縮小版を作る。**透過は保つ** (README は明暗どちらの
    テーマでも表示されるので、背景を焼き込むと片方で白い箱になる)。"""
    from PIL import Image

    im = Image.open(src).convert("RGBA")
    im = im.resize((px, round(px * im.height / im.width)), Image.LANCZOS)
    dst.parent.mkdir(parents=True, exist_ok=True)
    im.save(dst, "WEBP", quality=92, method=6)
    return f"{dst.name}  {im.size[0]}x{im.size[1]}  {dst.stat().st_size // 1024}KB"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--variants", type=int, default=1, help="作る枚数 (見比べて選ぶ)")
    ap.add_argument("--out", default="assets/logo.png", help="採用する 1 枚の保存先")
    ap.add_argument("--aspect", default="1:1",
                    help="アスペクト比。**参照画像が無いので明示指定が要る**")
    ap.add_argument("--redraw-emoji", action="store_true",
                    help="いまの --out の中央の 18禁マークだけをモデルに描き直させる "
                         "(**絵文字フォントの画像は渡さない**。理由は "
                         "REDRAW_EMOJI_PROMPT の説明を読むこと)")
    ap.add_argument("--fit-only", action="store_true",
                    help="生成せず、いまの --out を中央寄せ + 背景抜きするだけ (API を呼ばない)")
    ap.add_argument("--thumb", type=int, metavar="PX",
                    help="生成せず、いまの --out から縮小版 (assets/logo_small.webp) を作る。"
                         "README に本文中サイズで貼るため (API を呼ばない)")
    args = ap.parse_args()

    here_ = Path(__file__).resolve().parent
    if args.thumb:
        print(make_thumb(here_ / args.out, here_ / THUMB_OUT, args.thumb))
        return 0
    if args.fit_only:
        fit_logo(here_ / args.out)
        return 0

    from google import genai
    from google.genai import types as genai_types

    client = genai.Client(api_key=fetch_api_key())
    here = Path(__file__).resolve().parent
    out_dir = here / "assets" / "logo_candidates"
    out_dir.mkdir(parents=True, exist_ok=True)

    def part(path: Path):
        suffix = path.suffix.lower().lstrip(".")
        mime = "image/jpeg" if suffix in ("jpg", "jpeg") else f"image/{suffix}"
        return genai_types.Part.from_bytes(data=path.read_bytes(), mime_type=mime)

    if args.redraw_emoji:
        logo = here / args.out
        if not logo.is_file():
            raise SystemExit(f"参照画像が無い: {logo}")
        # **渡すのはロゴ 1 枚だけ。**絵文字フォントの画像は渡さない。
        contents = [part(logo), REDRAW_EMOJI_PROMPT]
    else:
        contents = [PROMPT]

    made: list[Path] = []
    for i in range(1, args.variants + 1):
        resp = client.models.generate_content(
            model=MODEL_NAME,
            contents=contents,
            config=genai_types.GenerateContentConfig(
                image_config=genai_types.ImageConfig(aspect_ratio=args.aspect),
                response_modalities=["IMAGE"],
            ),
        )
        data = None
        for cand in resp.candidates or []:
            for part in (cand.content.parts if cand.content else []):
                inline = getattr(part, "inline_data", None)
                if inline and inline.data:
                    data = inline.data
                    break
            if data:
                break
        if not data:
            print(f"  [{i}] 画像が返らなかった (フィルタの可能性)")
            continue
        dst = out_dir / f"logo_{i:02d}.png"
        dst.write_bytes(data)
        made.append(dst)
        print(f"  [{i}] {dst}  ({len(data)} bytes)")

    if not made:
        raise SystemExit("1 枚も生成できなかった。")

    # 既定は 1 枚目を採用。**気に入らなければ候補から差し替える**。
    final = here / args.out
    final.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(made[0], final)
    fit_logo(final)                            # ← 中身がずれたまま採用しない
    print(f"\n採用: {made[0].name} → {final}")
    print(f"候補: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
