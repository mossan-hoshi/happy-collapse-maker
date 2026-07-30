#!/usr/bin/env python
"""キャラタイルの**崩壊版サムネ**を Nano Banana 2 lite で作る。

    python make_chibi_collapse.py                 # 全キャラ 1 枚ずつ
    python make_chibi_collapse.py --chars noa,suzu --variants 3
    python make_chibi_collapse.py --adopt suzu=02  # 候補から採用を差し替える

崩壊度つまみが**崩壊前夜寄り**になったら、タイルの絵をこちらへ差し替える
(判定は `collapse_fx.tile_collapsed`)。素の絵と並べて切り替わるので、
**構図とキャラの同一性を崩さないことが最優先**。そのため
`assets/chibi/<id>.webp` を参照画像として渡し、絵柄を維持したまま描き直させる。

**lite を使う** (`make_logo.py` は日本語を焼くため無印を使うが、こちらは
文字を描かないので lite で足りる)。lite は入力プロンプトもトークン課金される。

API キーは `GEMINI_API_KEY` env、無ければ GCP Secret Manager から取る
(経路は `make_logo.fetch_api_key` を共有)。
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from make_logo import fetch_api_key       # API キーの取得経路は 1 本にする

MODEL_NAME = "gemini-3.1-flash-lite-image"      # Nano Banana 2 lite

SRC_DIR = "assets/chibi"
OUT_DIR = "assets/chibi_collapse"
CAND_DIR = "assets/chibi_collapse_candidates"

#: 完全崩壊で出す「なれの果て」。**モザイクを焼いたものだけをアプリへ置く**
#: (素のままは候補フォルダに残す)。
GORE_OUT_DIR = "assets/chibi_gore"
GORE_CAND_DIR = "assets/chibi_gore_candidates"

#: モザイクの粗さ。1 辺をこの数のブロックに割る。**タイルは 100px 前後で表示される**ので、
#: これ以上細かいと中身が読めてしまい、粗すぎると単なる色面になる。
GORE_MOSAIC_BLOCKS = 14

#: キャラごとの崩壊のさせ方。**全員違う見た目**にする (同じ効果を 8 回見せない)。
#: 薬物を連想させる表現は使わない。流血・裂傷も描かせない (意匠としての崩壊に留める)。
COLLAPSE_STYLES: dict[str, str] = {
    "noa": "ゾンビ風。肌が青灰色に変色し、瞳のハイライトが消えて濁る。"
           "服は土埃で汚れ、裾がほつれて裂けている。",
    "ritsu": "闇落ち風。髪と瞳が黒く染まり、目の下から黒い侵食模様が広がる。"
             "瞳の奥だけが赤く光り、背後に黒い靄が立ちのぼる。",
    "priya": "全身包帯風。顔の半分と手足を古びた包帯で巻き、"
             "ほつれた端が垂れ下がっている。包帯は薄汚れて黄ばんでいる。",
    # **「痩せこけた」と直接書くと画像フィルタで返らない** (摂食障害を連想する語のため)。
    # 狙いは「枯れて骨ばった見た目」なので、乾いて色を失う方向の語に言い換えている。
    "yume": "枯れ果てた風。肌が乾いて色を失い、頬に深い影が落ちる。"
            "手指と鎖骨の線が硬く細く浮き、服が余ってたるんでいる。",
    "reika": "ひび割れた陶器人形風。肌に陶器のような亀裂が走り、"
             "欠けた破片が周囲に浮かぶ。割れ目の奥は暗い空洞。",
    # **この 3 体は 1 回目が弱すぎて素の絵とほぼ見分けが付かなかった。**
    # 「輪郭が散りはじめる」「走査線がずれる」程度だと、サムネ寸では消える。
    # 体の一部が明確に欠ける・大きくずれる、まで踏み込ませること。
    "suzu": "灰化して崩れ落ちる風。**体の下半分がすでに灰になって崩れ落ち**、"
            "床に灰が積もっている。腕の先も粒子になって消え、"
            "顔にも亀裂が走って灰が剥がれ落ちている。",
    "kasumi": "映像が壊れたようなグリッチ風。**体が水平に 4 つの帯へ分断され、"
              "それぞれが大きく左右にずれている**。ずれた隙間は黒く抜け落ち、"
              "RGB が大きく分離して輪郭が三重にぶれる。体の一部が矩形に欠落している。",
    "tsukasa": "影に侵食される風。**体の半分以上が黒い影に飲み込まれ**、顔の下半分も"
               "影に沈んでいる。残った輪郭も黒く溶け出し、"
               "影の中から複数の手が伸びて体を掴んでいる。",
    # せんべいのキャラなので「割れて欠ける」がそのまま崩壊の意匠になる。
    "mossan_hoshi": "せんべいが割れて欠けていく風。**表面に大きなひびが縦横に走り、**"
                    "**縁の一部が欠け落ちて破片になっている。**欠けた断面はざらついた"
                    "断面が見え、崩れた破片が周りに散らばっている。目の表情はそのまま残す。",
}

PROMPT = """1枚目はキャラクターの立ち絵です。
このキャラクターが**崩壊しかけている**姿に描き直してください。

崩壊のさせ方: {style}

**強さ**: 100px 程度のサムネイルで表示するので、**その大きさでも一目で崩壊していると
分かること**。1枚目と見比べないと違いが分からない程度の変化では不十分。

**最優先**: 素の絵と並べて切り替えて見せるので、1枚目と重ねたときにずれないこと。
- 構図・ポーズ・画角・キャラの大きさと位置・背景の扱いを1枚目に揃える
- 絵柄 (線の太さ・塗り・デフォルメの度合い) を1枚目に合わせる
- **同じキャラクターだと分かること** — 髪型・髪色・瞳の色・服の意匠は残す

描かないもの:
- 流血・裂傷・内臓。あくまで意匠としての崩壊に留める
- 薬物を連想させるもの
- 文字・ロゴ・枠・台紙
"""


#: 完全崩壊のなれの果て。**参照画像を渡さない**ので、絵柄は文章で指定する。
#: キャラの同一性は残らない (残骸に髪型も服も無い) ため、キャラ別に作り分けるのは
#: 「絵が 8 枚とも同じでは間が抜ける」という一点の理由。
GORE_VARIANTS: tuple[str, ...] = (
    "白骨。頭蓋骨と肋骨、投げ出された手の骨が床に散らばっている。",
    "崩れた肉片の山。床に不定形の塊がいくつも落ちている。",
    "白骨。うつ伏せに崩れ落ちた全身の骨格。頭蓋骨が横を向いている。",
    "肉片と骨が混じった残骸。床に広がって散らばっている。",
    "白骨。積み上がった骨の山の上に頭蓋骨がひとつ載っている。",
    "崩れた肉片。床に落ちて潰れた塊と、そこから伸びる細い骨。",
    "白骨。膝をついたまま崩れた骨格が前のめりに倒れている。",
    "肉片の残骸。床の上でいくつもの塊に分かれて転がっている。",
)

GORE_PROMPT = """イラストを1枚描いてください。

描くもの: {variant}

絵柄 (**参照画像は無いので、ここの指定に従うこと**):
- 日本のアニメ・ゲームで使われるデフォルメされたキャラクターイラストと同じタッチ
- はっきりした均一な太さの黒い輪郭線
- 影は硬いセルシェーディング。グラデーションはほとんど使わない
- 彩度は中程度で、色数を絞る
- **背景は真っ白**。床の影だけを落とす。枠・台紙・タイルの中に入れない

構図:
- 正方形。対象を中央に置き、周囲に余白を取る
- 真横やや上からの視点で、床に落ちている状態が分かるように

描かないもの:
- 文字・ロゴ
- 人物の顔や生きているキャラクター
- 薬物を連想させるもの
"""


#: **キャラ別のなれの果て。**`GORE_VARIANTS` + `GORE_PROMPT` の使い回しが効かない
#: 相手だけここに書く。`GORE_VARIANTS` は「人が崩れた残骸」を順に配るだけなので、
#: **人でないキャラには当たらない**。mossan_hoshi は煎餅で、素のタイルも
#: イラストではなく写真なので、被写体も絵柄も両方まるごと差し替える。
GORE_PROMPTS: dict[str, str] = {
    "mossan_hoshi": """写真を1枚生成してください。

被写体: 白い煎餅が粉々に砕けた欠片が、山のように積もっている。
大小さまざまな破片と細かい粉が混ざり合って崩れた山になっている。
**原形をとどめている煎餅は1枚も無い。**

見え方 (**参照画像は無いので、ここの指定に従うこと**):
- 実際に撮影したスナップ写真。イラスト・CG にしない
- 煎餅は白〜淡いクリーム色で、表面はざらついて気泡の跡がある
- 木目のテーブルの上に積もっている。やや上から見下ろした構図
- 自然光。強い演出やスタジオ照明にしない

構図:
- 正方形。対象を中央に置き、周囲に余白を取る

描かないもの:
- 文字・ロゴ
- 人物・手・顔
- 丸い形のまま残っている煎餅
""",
}


def mosaic(im, blocks: int = GORE_MOSAIC_BLOCKS):
    """モザイクを**焼き込む**。

    CSS で隠すと DevTools で外せてしまうし、配布物に素の画像が残る。
    サムネとして出すのはこちらだけにして、素のままは候補フォルダに置く。
    """
    from PIL import Image

    w, h = im.size
    small = im.resize((max(blocks, 1), max(round(blocks * h / w), 1)), Image.BOX)
    return small.resize((w, h), Image.NEAREST)


def part(path: Path):
    from google.genai import types as genai_types

    suffix = path.suffix.lower().lstrip(".")
    mime = "image/jpeg" if suffix in ("jpg", "jpeg") else f"image/{suffix}"
    return genai_types.Part.from_bytes(data=path.read_bytes(), mime_type=mime)


def to_webp(src: Path, dst: Path, size: tuple[int, int]) -> None:
    """元サムネと**同じ寸法・同じ形式**へ揃える。

    タイルは CSS で正方形に収めているが、素の絵と寸法が違うと
    切り替えた瞬間に絵の大きさが跳ねる。
    """
    from PIL import Image

    im = Image.open(src).convert("RGB")
    if im.size != size:
        im = im.resize(size, Image.LANCZOS)
    dst.parent.mkdir(parents=True, exist_ok=True)
    im.save(dst, "WEBP", quality=90, method=6)


def make_gore(here: Path, args) -> int:
    """完全崩壊のなれの果てを作る。

    **キャラ 1 体につき 1 枚**割り当てる (`GORE_VARIANTS` を順に配る)。
    参照画像は渡さない — 残骸にキャラの同一性は残らないため。
    アプリへ置くのは**モザイクを焼いたものだけ**。
    """
    from PIL import Image

    out_dir, cand_dir = here / GORE_OUT_DIR, here / GORE_CAND_DIR
    src_dir = here / SRC_DIR
    cand_dir.mkdir(parents=True, exist_ok=True)
    targets = [c.strip() for c in args.chars.split(",") if c.strip()] or list(COLLAPSE_STYLES)

    if args.remosaic:
        n = 0
        for cid in targets:
            cand = cand_dir / f"{cid}.png"
            if not cand.is_file():
                continue
            size = Image.open(src_dir / f"{cid}.webp").size
            out = out_dir / f"{cid}.webp"
            out.parent.mkdir(parents=True, exist_ok=True)
            mosaic(Image.open(cand).convert("RGB").resize(size, Image.LANCZOS)).save(
                out, "WEBP", quality=90, method=6)
            n += 1
        print(f"{n} 枚のモザイクを焼き直した (ブロック数 {GORE_MOSAIC_BLOCKS})")
        return 0 if n else 1

    from google import genai
    from google.genai import types as genai_types

    client = genai.Client(api_key=fetch_api_key())
    made = 0
    for i, cid in enumerate(targets):
        prompt = GORE_PROMPTS.get(cid)
        variant = prompt or GORE_VARIANTS[i % len(GORE_VARIANTS)]
        prompt = prompt or GORE_PROMPT.format(variant=variant)
        print(f"\n{cid}: {variant.strip().splitlines()[0]}")
        try:
            resp = client.models.generate_content(
                model=args.model,
                contents=[prompt],
                config=genai_types.GenerateContentConfig(
                    image_config=genai_types.ImageConfig(aspect_ratio="1:1"),
                    response_modalities=["IMAGE"],
                ),
            )
        except Exception as ex:                # noqa: BLE001
            print(f"  失敗: {ex}")
            continue

        data = None
        for cand in resp.candidates or []:
            for p in ((cand.content.parts if cand.content else None) or []):
                inline = getattr(p, "inline_data", None)
                if inline and inline.data:
                    data = inline.data
                    break
            if data:
                break
        if not data:
            print("  画像が返らなかった (フィルタの可能性)")
            continue

        raw = cand_dir / f"{cid}.png"
        raw.write_bytes(data)
        size = Image.open(src_dir / f"{cid}.webp").size
        out = out_dir / f"{cid}.webp"
        out.parent.mkdir(parents=True, exist_ok=True)
        mosaic(Image.open(raw).convert("RGB").resize(size, Image.LANCZOS)).save(
            out, "WEBP", quality=90, method=6)
        made += 1
        print(f"  {raw.name} ({len(data)//1024}KB) → モザイク焼き込み → {GORE_OUT_DIR}/{cid}.webp")

    print(f"\n{made}/{len(targets)} 枚。素のままは {cand_dir}")
    return 0 if made else 1


def main() -> int:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser()
    ap.add_argument("--chars", default="",
                    help="対象をカンマ区切りで絞る (既定は全員)")
    ap.add_argument("--variants", type=int, default=1, help="1 キャラあたりの生成枚数")
    ap.add_argument("--model", default=MODEL_NAME)
    ap.add_argument("--adopt", default="",
                    help="生成せずに採用だけ差し替える。例 suzu=02,noa=03")
    ap.add_argument("--gore", action="store_true",
                    help="完全崩壊用の「なれの果て」を作る (参照画像なし・モザイク焼き込み)")
    ap.add_argument("--remosaic", action="store_true",
                    help="生成せず、いまの候補からモザイクを焼き直すだけ (API を呼ばない)")
    args = ap.parse_args()

    if args.gore or args.remosaic:
        return make_gore(here, args)

    src_dir, out_dir = here / SRC_DIR, here / OUT_DIR
    cand_dir = here / CAND_DIR

    if args.adopt:
        for item in args.adopt.split(","):
            cid, _, num = item.partition("=")
            cand = cand_dir / f"{cid}_{num}.png"
            if not cand.is_file():
                raise SystemExit(f"候補が無い: {cand}")
            from PIL import Image
            to_webp(cand, out_dir / f"{cid}.webp", Image.open(src_dir / f"{cid}.webp").size)
            print(f"採用: {cand.name} → {cid}.webp")
        return 0

    targets = [c.strip() for c in args.chars.split(",") if c.strip()] or list(COLLAPSE_STYLES)
    unknown = [c for c in targets if c not in COLLAPSE_STYLES]
    if unknown:
        raise SystemExit(f"崩壊のさせ方が未定義: {unknown}")

    from google import genai
    from google.genai import types as genai_types
    from PIL import Image

    client = genai.Client(api_key=fetch_api_key())
    cand_dir.mkdir(parents=True, exist_ok=True)
    made = 0

    for cid in targets:
        src = src_dir / f"{cid}.webp"
        if not src.is_file():
            print(f"[warn] 元サムネが無い: {src}")
            continue
        size = Image.open(src).size
        prompt = PROMPT.format(style=COLLAPSE_STYLES[cid])
        print(f"\n{cid} ({size[0]}x{size[1]})")

        first: Path | None = None
        for i in range(1, args.variants + 1):
            try:
                resp = client.models.generate_content(
                    model=args.model,
                    contents=[part(src), prompt],
                    config=genai_types.GenerateContentConfig(
                        response_modalities=["IMAGE"],
                    ),
                )
            except Exception as ex:            # noqa: BLE001  1 体の失敗で全体を止めない
                print(f"  [{i}] 失敗: {ex}")
                continue

            data = None
            for cand in resp.candidates or []:
                # `content` があっても `parts` が None のことがある (フィルタ時など)。
                # **両方を見ないと TypeError で落ちる。**
                for p in ((cand.content.parts if cand.content else None) or []):
                    inline = getattr(p, "inline_data", None)
                    if inline and inline.data:
                        data = inline.data
                        break
                if data:
                    break
            if not data:
                print(f"  [{i}] 画像が返らなかった (フィルタの可能性)")
                continue

            dst = cand_dir / f"{cid}_{i:02d}.png"
            dst.write_bytes(data)
            print(f"  [{i}] {dst.name}  ({len(data)//1024}KB)")
            first = first or dst

        if first:
            to_webp(first, out_dir / f"{cid}.webp", size)
            made += 1
            print(f"  採用 → {OUT_DIR}/{cid}.webp")

    print(f"\n{made}/{len(targets)} 体ぶん作成。候補: {cand_dir}")
    return 0 if made else 1


if __name__ == "__main__":
    sys.exit(main())
