#!/usr/bin/env python
"""README に貼るサンプル音声と、その表の markdown を作る。

**アプリ (app.py) は立ち上げないが、生成経路はアプリと同じ** (`app.synthesize`)。
表に出す音は「素の既定値で出る音」でなければ意味がないので、UI の既定値を
下の定数に写してある。**ここを勝手に触らないこと** (表と実物がずれる)。

    python make_samples.py                 # 全キャラぶん作って samples/table.md を書く
    python make_samples.py --chars noa     # 一部だけ作り直す
    python make_samples.py --table-only    # 音は作らず表だけ組み直す

崩壊は確率的なので、**崩れなかった行はシードを変えて引き直す**。当たったシードは
`SAMPLES` に書き戻して固定する (シードを固定すればビット単位で再現される)。
崩れたかどうかは耳で聴くのが本筋だが、目安は出力の指標 (`measure`) で判る:
生成尺の上限に張り付いていて、かつ高域比・スペクトル平坦度が正常部より大きく上がる。
"""

from __future__ import annotations

import argparse
import json
import random
import time
from pathlib import Path

import numpy as np
import soundfile as sf

import app
import collapse_fx as fx

# ---- UI の既定値 (app.py のスライダ初期値と同じ。**表は素の既定で出す**) ----
DUR_SEC = 20.0
HEAD_LO, HEAD_HI = app.HEAD_DEFAULT_LO, app.HEAD_DEFAULT_HI   # 冒頭の正常区間 (秒)
TEMPERATURE = SUB_TEMPERATURE = 0.9
NORMAL_HEAD = True                 # 冒頭は正常な推論を使う

#: 音声の置き場所 (リポジトリ相対)。README からはここへリンクする。
SAMPLE_DIR = "samples"

#: **mp3 で置く。**24kHz mono の wav は 1 秒 48KB で、3 段 × 8 キャラだと
#: リポジトリが数十 MB 太る。表から聴ければ十分なので圧縮側に振る。
SAMPLE_EXT = "mp3"

#: 段の並びは `fx.EASY_STOPS` (つまみのアンカー) が正。ファイル名用の綴りだけここで持つ。
TIER_SLUGS = ("normal", "eve", "full")
assert len(TIER_SLUGS) == len(fx.EASY_STOPS)

#: 崩壊度の目印。**アプリのバーの色 (`app.EASY_COLORS`) に合わせる** —
#: 緑 `#3ddc97` → 黄 `#ffc400` → 赤 `#ff1717`。
#: **GitHub は README の `style` 属性を落とす**ので文字色は付けられない。emoji で代える。
TIER_EMOJI = ("🟢", "🟡", "🔴")
assert len(TIER_EMOJI) == len(app.EASY_COLORS)

#: 読ませる文。**全行で同じ文を使う。**
#:
#: 理由が 2 つある。(1) 崩壊が当たるかどうかは**文にも強く依存する**
#: (実測: 台詞系の短めの文では 6 通り試して全部不発、地の文では 1 発で崩壊)。
#: (2) 全員が同じ文を読むので**声そのものを比べられる**。
#: **短い文は選ばない** — 崩壊は生成が進むほど深くなるので、短文だと崩れきる前に
#: 文が終わる。
SAMPLE_PRESET = "地の文 (t2・検証で最良)"

#: 段の別名 (`tiers` に書くときの綴り)。並びは `TIER_SLUGS` と同じ。
NORMAL, EVE, FULL = range(len(TIER_SLUGS))

#: キャラ 1 人ぶんの設定。`preset` は `app` のテキストプリセットの見出し
#: (本文をここに書き写すと app 側と二重管理になる)。
#:
#: **`ref` と `seed` は「長いほう」ではなく「崩壊が当たった組」。**参照の長さは効きに
#: 関係するが決定的ではない (noa は 15.9 秒の set6 で不発、11.5 秒の set1 で崩壊)。
#: `--probe` で当たりを探して、その組をここに書き戻す。
#:
#: **`tiers` には「崩壊が当たった段」だけを書く。**崩壊は確率的で、しかもキャラによって
#: 当たる段が違う (下表)。表は**段を 1 つの列**にしてあるので、当たらなかった段は
#: 行が無いだけで済む — 段ごとに列を割ると、そこに「壊れていない音」が並んでしまう。
#:
#: 実測 (地の文・シード 1 と 7、`--probe`):
#:
#: | キャラ | 崩壊前夜 | 完全崩壊 |
#: |---|---|---|
#: | 乃亜 (set1) / 怜香 / すず / ゆめ | ○ | ○ |
#: | 司 / 霞 | ○ | × |
#: | 律 | × | ○ |
#: | プリヤ | × | × |
#: **シードは生成のたびにランダムに振る** (`seeds` の値が `None` の段)。
#: 固定値を使い回すと、文もレシピも同じなので**違うのは参照音声だけ**になり、
#: 崩れ方が横並びで似る (実測: 韓国語風の音韻に落ちるモードが何本も続けて出た。
#: Qwen3-TTS は多言語モデルなので、参照を壊すと `language="japanese"` の指定を
#: 振り切って他言語の音韻へ流れる)。
#: **値を書くのは「採用したテイクを再現する」ときだけ。**振った値は
#: `samples/seeds.json` に残るので、採用が決まったらそこから写す。
#:
#: `keep` は**作り直さない**段 = **耳で採用が決まったもの**。ここに入れ忘れると
#: 一括実行で上書きされ、選んだテイクを失う (実際に noa の中段を潰した)。
#: 0% は接合が無いので作り直しても同じものが出るが、崩壊段は設定を変えれば別物になる。
#:
#: **並び順はそのまま表の行順になる** (`table()` は受け取った順に並べるだけ)。
#: 順序を変えても生成物には影響しないが、`make_embeds.py` の manifest 順も動く。
SAMPLES: tuple[dict, ...] = (
    # noa の中段は**弱めた値で描いたテイクを採用した** (`start=0.65` / `hard=0.90`)。
    # つまみ位置に換算すると 0.70 = 35% なので表記はその % だが、補間値
    # (`hard=0.93`) では**崩壊が不発になった**ので、実値を `fx` に持って再現する。
    dict(char="noa",     ref="set1",      keep=(NORMAL, EVE, FULL),
         seeds={NORMAL: 1, EVE: 1, FULL: 842759}, pos={EVE: 0.70},
         fx={EVE: dict(chunk_sec=0.50, start=0.65, hard=0.90, warp=0.0)}),
    dict(char="yume",    ref="normal_01", keep=(NORMAL, EVE, FULL),
         seeds={NORMAL: 1, EVE: 111, FULL: 400400}),
    dict(char="tsukasa", ref="normal_01", keep=(NORMAL, EVE, FULL),
         seeds={NORMAL: 7, EVE: 7, FULL: 752224}),
    dict(char="reika",   ref="normal_02", keep=(NORMAL, EVE, FULL),
         seeds={NORMAL: 1, EVE: 105, FULL: 302}),
    dict(char="suzu",    ref="normal_01", keep=(NORMAL, EVE, FULL),
         seeds={NORMAL: 1, EVE: 1, FULL: 983820}),
    dict(char="kasumi",  ref="normal_01", keep=(NORMAL, EVE, FULL),
         seeds={NORMAL: 1, EVE: 1, FULL: 251662}),
    dict(char="ritsu",   ref="normal_01", keep=(NORMAL, EVE, FULL),
         seeds={NORMAL: 7, EVE: 145139, FULL: 461936}),
    # プリヤは 2 シード × 2 段すべて不発だったので 0% だけ。
    dict(char="priya",   ref="normal_02", keep=(NORMAL,),
         seeds={NORMAL: 1}),
    # **通常段は `keep` に入れていたのに一度も生成していなかった** (バグ。
    # 生成前から keep していたので `generate()` がずっとスキップしていた)。
    dict(char="mossan_hoshi", ref="normal_01", keep=(EVE, FULL),
         seeds={NORMAL: 1, EVE: 280860, FULL: 903948}),
)


def preset_of(row: dict) -> str:
    """読ませる文の見出し。**全行同じ** (`SAMPLE_PRESET`) だが上書きできる。"""
    return row.get("preset") or SAMPLE_PRESET


def repo() -> Path:
    return Path(__file__).resolve().parent


def measure(y: np.ndarray) -> dict[str, float]:
    """崩壊したかどうかの目安。**耳の代わりではない**が、引き直しの判断には使える。

    崩壊部は正常部より高域 (6kHz 以上) のエネルギー比とスペクトル平坦度が上がる
    (実測で HF% が 40 倍・平坦度が 19 倍)。`clip%` はモデル内部のクランプに
    張り付いた割合 = 音割れの量。
    """
    n = 1024
    frames = [y[i:i + n] for i in range(0, max(len(y) - n, 1), n)]
    sfm, hf = [], []
    for f in frames:
        if len(f) < n:
            continue
        spec = np.abs(np.fft.rfft(f * np.hanning(n))) ** 2 + 1e-12
        sfm.append(float(np.exp(np.mean(np.log(spec))) / np.mean(spec)))
        freqs = np.fft.rfftfreq(n, 1 / fx.SR)
        hf.append(float(spec[freqs >= 6000].sum() / spec.sum()))
    return {
        "peak": float(np.max(np.abs(y))) if y.size else 0.0,
        "clip%": float(np.mean(np.abs(y) > 0.99) * 100) if y.size else 0.0,
        "sfm": float(np.mean(sfm)) if sfm else 0.0,
        "hf%": float(np.mean(hf) * 100) if hf else 0.0,
        "sec": len(y) / fx.SR,
    }


def pos_of(row: dict, idx: int) -> float:
    """その段を描いたつまみ位置。既定は段のアンカー (`fx.EASY_STOPS`)。

    **採用済みのテイクをアンカー以外の値で描いた場合は `pos` に実測値を書く** —
    表の % 表記が実物と食い違わないようにするため。
    """
    return float(row.get("pos", {}).get(idx, fx.EASY_STOPS[idx][0]))


def tier_params(pos: float) -> tuple[str, float, float, float, float]:
    """つまみ位置から `(効果, chunk_sec, start, hard, warp)` を引く。"""
    return fx.easy_params(pos)


def tile_for(char: str, pos: float) -> str:
    """その崩壊度でアプリが出すタイル画像のパス。**判定は app と同じ経路を通す。**

    その段の絵を持たないキャラは 1 段手前へ落とす (アプリと同じ振る舞い)。
    """
    effect, _c, start, _h, _w = tier_params(pos)
    want = fx.tile_state(effect, start)
    for cand in (want, fx.TILE_COLLAPSE, fx.TILE_NORMAL):
        p = repo() / "assets" / cand / f"{char}.webp"
        if p.is_file():
            return f"assets/{cand}/{char}.webp"
    return ""


def sample_path(char: str, idx: int) -> Path:
    return repo() / SAMPLE_DIR / f"{char}_{TIER_SLUGS[idx]}.{SAMPLE_EXT}"


def write_sample(path: Path, wav: np.ndarray) -> Path:
    """サンプルを書き出す。**書けなくても走り続ける。**

    Windows は**プレイヤーで再生中のファイルをロックする**ので、聴きながら作り直すと
    ここで `LibsndfileError` になる。1 本のロックで数十分の生成を全部捨てるのは論外なので、
    隣に `<名前>.new.<拡張子>` を書いて先へ進む (閉じてから差し替えればよい)。
    """
    try:
        sf.write(str(path), wav, fx.SR, format=SAMPLE_EXT.upper())
        return path
    except Exception as e:                      # ロック / 権限 / ディスク
        alt = path.with_suffix(f".new.{SAMPLE_EXT}")
        sf.write(str(alt), wav, fx.SR, format=SAMPLE_EXT.upper())
        print(f"      [警告] {path.name} に書けなかったので {alt.name} に置いた"
              f" (再生中のプレイヤーを閉じて差し替えること): {e}")
        return alt


#: 実際に振ったシードの記録。**生成のたびにランダムに振る**ので、これが無いと
#: 採用したテイクを二度と描けない (崩壊は確率的で、シードが変われば別物になる)。
#: `--table-only` でも正しいシードを表に出せるようにファイルへ残す。
SEED_LOG = "samples/seeds.json"

#: シードの範囲。**アプリが起動時に振る範囲と同じ** (`app.py` の `random.randint`)。
SEED_MAX = 999_999


def load_seed_log() -> dict[str, int]:
    p = repo() / SEED_LOG
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}


def save_seed_log(log: dict[str, int]) -> None:
    (repo() / SEED_LOG).write_text(
        json.dumps(log, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")


def seed_key(char: str, idx: int) -> str:
    return f"{char}_{TIER_SLUGS[idx]}"


def seed_of(row: dict, idx: int) -> int | None:
    """そのテイクのシード。**None なら生成時にランダムに振る。**

    優先順は `--seed` (全段上書き) → `SAMPLES` の固定値 → 記録 (`seeds.json`)。
    `SAMPLES` に値を書くのは**採用済みのテイクを再現したいときだけ**にする。
    毎回同じ値を使い回すと、文もレシピも同じなので崩れ方が横並びで似てしまう。
    """
    if row.get("seed") is not None:
        return int(row["seed"])
    pinned = row["seeds"].get(idx)
    if pinned is not None:
        return int(pinned)
    return load_seed_log().get(seed_key(row["char"], idx))


def params_for(row: dict, idx: int, text: str, seed: int) -> app.Params:
    effect, chunk, start, hard, warp = tier_params(pos_of(row, idx))
    # **採用したテイクを実値で再現できるようにする。**つまみ位置からの補間だと
    # アンカー以外の値で描いたテイクを取り戻せない (`pos=0.70` の補間は
    # `hard=0.93` になり、実際に描いた `0.90` と一致しない)。耳で選んだテイクを
    # 作り直しで失わないため、`fx` に実値があればそれを使う。
    over = row.get("fx", {}).get(idx) or {}
    chunk = float(over.get("chunk_sec", chunk))
    start = float(over.get("start", start))
    hard = float(over.get("hard", hard))
    warp = float(over.get("warp", warp))
    # **`warp` だけは行から上書きできる。**質感を壊す軸は「どれだけ低音になるか」に
    # 直結するので (終端の再生速度が下がる = ピッチとフォルマントが下がる)、
    # 崩壊の当たり外れとは別に量を選びたい。
    if row.get("warp") is not None:
        warp = float(row["warp"])
    return app.Params(
        mode="サンプル", text=text, ref_choice=f"{row['char']}/{row['ref']}",
        ref_file=None, ref_text="", auto_text="", do_trim=True,
        effect=effect, chunk_sec=chunk, ramp_start=start, ramp_hard=hard, warp=warp,
        normal_head=NORMAL_HEAD, head_lo=HEAD_LO, head_hi=HEAD_HI,
        dur_sec=DUR_SEC, seed=int(seed),
        temperature=TEMPERATURE, sub_temperature=SUB_TEMPERATURE,
    )


def detect_lang(y: np.ndarray) -> tuple[str, float]:
    """崩壊部が**どの言語に聞こえるか**を返す `(言語, 確度)`。

    参照を壊すとモデルが `language="japanese"` の指定を振り切って他言語の音韻へ
    流れる (実測: 韓国語風になる組が続けて出た)。`app.transcribe` は
    `language="ja"` 固定なので、韓国語風でも**無理に日本語として書き起こされて**
    しまい気付けない。ここは言語自動判定で見る。
    """
    import librosa

    y16 = librosa.resample(y.astype(np.float32), orig_sr=fx.SR,
                           target_sr=app.WHISPER_SR)
    _segs, info = app._asr().transcribe(np.ascontiguousarray(y16, dtype=np.float32),
                                        language=None, beam_size=1, vad_filter=False)
    return info.language, float(info.language_probability)


def kept_ratio(text: str, got: str) -> float:
    """読ませた文の文字が書き起こしにどれだけ残っているか (0〜1)。

    **崩壊したかどうかの機械判定はこれで行う。**指標 (`measure`) では判定できない —
    完全崩壊は `warp` で速度が落ちるので高域比と平坦度が**下がる**方向に動き、
    「壊れていない」と見分けが付かない。
    """
    return len(set(got) & set(text)) / max(len(set(text)), 1)


def generate(rows: tuple[dict, ...], verbose: bool = False, retry: int = 0) -> None:
    out_dir = repo() / SAMPLE_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    seed_log = load_seed_log()
    for row in rows:
        char, name = row["char"], app.CHARACTER_NAMES[row["char"]]
        text = app.presets_for(char)[preset_of(row)]
        print(f"\n=== {name} ({char}) / 参照 {row['ref']}")
        for idx in sorted(row["seeds"]):
            label = fx.EASY_STOPS[idx][1]
            if idx in row.get("keep", ()):
                print(f"  {label}: 採用済みなので作り直さない ({sample_path(char, idx).name})")
                continue
            # **生成ごとにランダムに振る。**固定値を使い回すと文もレシピも同じなので
            # 崩れ方が横並びで似る。`SAMPLES` に値がある = 採用済みの再現のときだけ従う。
            pinned = row["seeds"].get(idx) if row.get("seed") is None else row["seed"]
            # **不発なら引き直す。**崩壊は確率的で、当たるまでシードを振るのが
            # アプリでの使い方でもある。固定シードのときは引き直さない (再現が目的)。
            for attempt in range(retry + 1 if pinned is None and idx != NORMAL else 1):
                seed = int(pinned) if pinned is not None else random.randint(1, SEED_MAX)
                seed_log[seed_key(char, idx)] = seed
                save_seed_log(seed_log)
                t = time.perf_counter()
                wav, log, _refs = app.synthesize(params_for(row, idx, text, seed))
                write_sample(sample_path(char, idx), wav)
                m = measure(wav)
                capped = m["sec"] >= DUR_SEC * 0.95
                print(f"  {label} seed {seed}: {m['sec']:.1f}s  "
                      f"peak {m['peak']:.2f}  clip {m['clip%']:.2f}%  sfm {m['sfm']:.4f}"
                      f"{'  ← 上限張り付き (崩壊)' if capped else '  ← 不発'}"
                      f"  [{time.perf_counter()-t:.0f}s]")
                if idx != NORMAL:
                    # **崩壊部だけを見る** (頭は正常な推論なので必ず日本語と判定される)
                    lang, prob = detect_lang(wav[int(HEAD_HI * fx.SR):])
                    print(f"      言語判定 {lang} ({prob*100:.0f}%)"
                          f"{'' if lang == 'ja' else '  ← 日本語から外れた'}")
                if capped:
                    break
            for line in log:
                if verbose or "上限張り付き" in line:
                    print(f"    {line.strip()}")


#: シード探索の置き場所。**配布物に入れない** (`.gitignore`)。
PROBE_DIR = "samples/_probe"


def probe(rows: tuple[dict, ...], seeds: tuple[int, ...], tiers: tuple[int, ...]) -> None:
    """**当たりシードを探す。**崩壊は確率的なので、素のまま通ってしまう組が出る。

    **候補も本番と同じ「冒頭は正常」付きで作る。**崩壊するかどうかは頭の有無で
    変わらないので、切れば生成が 1 回で済んで探索は倍速になる — が、**耳で選ぶ相手が
    本番と違う形をしているのは駄目**で、実際に「冒頭から意味不明」と混乱を招いた。
    聴いたものがそのまま最終形になるようにする。
    """
    out = repo() / PROBE_DIR
    out.mkdir(parents=True, exist_ok=True)
    for row in rows:
        char = row["char"]
        text = app.presets_for(char)[preset_of(row)]
        print(f"\n=== {app.CHARACTER_NAMES[char]} ({char}) / 参照 {row['ref']}")
        for idx in tiers:
            label = fx.EASY_STOPS[idx][1]
            for seed in seeds:
                t = time.perf_counter()
                wav, _log, _refs = app.synthesize(params_for(row, idx, text, seed))
                tag = "" if row.get("warp") is None else f"_w{float(row['warp']):.2f}"
                write_sample(
                    out / f"{char}_{row['ref']}_{TIER_SLUGS[idx]}_{seed}{tag}"
                    f".{SAMPLE_EXT}", wav)
                sec = len(wav) / fx.SR
                # 頭は正常な推論なので、**崩壊部だけ**を書き起こして判定する
                got = app.transcribe(wav[int(HEAD_HI * fx.SR):])
                print(f"  {label} seed {seed}: {sec:4.1f}s  "
                      f"崩壊部の残存 {kept_ratio(text, got)*100:3.0f}%"
                      f"{'  ← 上限張り付き' if sec >= DUR_SEC * 0.95 else ''}"
                      f"  [{time.perf_counter()-t:.0f}s]\n      {got}")


def verify(rows: tuple[dict, ...]) -> None:
    """出来上がったサンプルを**書き起こして崩壊したかどうかを見る。**

    指標 (`measure`) だけでは判定できない。完全崩壊は `warp` で速度が落ちるので
    高域比と平坦度が**下がる**方向に動き、「壊れていない」と見分けが付かない。

    **音声全体に一度掛けても判定にならない。**崩壊段のサンプルは
    「頭は正常な推論 + その後ろが崩壊」なので、全体を書き起こすと頭の正しい文が
    そのまま出てきて高い一致率になり、後ろが壊れていても見えない。
    **頭と後ろを分けて書き起こし、頭は文と一致・後ろは意味不明**であることを見る。

    耳の代わりにはならないが、**引き直すべき行を機械的に見つけられる。**
    """
    for row in rows:
        char = row["char"]
        text = app.presets_for(char)[preset_of(row)]
        print(f"\n=== {app.CHARACTER_NAMES[char]} ({char})\n  読ませた文: {text}")
        for idx in sorted(row["seeds"]):
            label = fx.EASY_STOPS[idx][1]
            path = sample_path(char, idx)
            if not path.is_file():
                print(f"  {label}: (未生成)")
                continue
            y = app.read_mono24k(str(path))
            effect, _c, start, _h, _w = tier_params(pos_of(row, idx))
            if effect == "none":
                got = app.transcribe(y)
                print(f"  {label}: 一致 {kept_ratio(text, got)*100:3.0f}%  / {got}")
                continue
            # 接合位置は `HEAD_LO`〜`HEAD_HI` の中から自動で選ばれる。
            # **後ろ側だけを見たいので上限で切る** (頭を混ぜると一致率が上がってしまう)。
            cut = int(HEAD_HI * fx.SR)
            head, tail = app.transcribe(y[:cut]), app.transcribe(y[cut:])
            print(f"  {label}: 頭 {HEAD_HI:.0f}s まで / {head}")
            print(f"          崩壊部 一致 {kept_ratio(text, tail)*100:3.0f}% / {tail}")


#: GitHub user-attachments の埋め込み URL 一覧。**キーは `<char>_<ref|normal|eve|full>`。**
#: 発行は Web エディタへの手動アップロードでしかできない (API では発行不可) ので、
#: ここでは既に発行済みの値を読むだけ。無いキャラ/段は表からプレイヤーが抜ける
#: (壊れたリンクを出すよりまし)。
EMBED_URLS = "samples/embed_urls.json"


def load_embed_urls() -> dict[str, str]:
    p = repo() / EMBED_URLS
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}


def player_cell(url: str | None) -> str:
    """表のセルに置く**その場再生プレイヤー**。`<video>` タグを直接書く。

    GitHub で音声を「ページ遷移せずその場で鳴らす」方法は user-attachments だけだが、
    **書き方を 3 通り試して 1 つしか使えなかった**ので、経緯を残す:

    1. **bare URL** (`https://github.com/user-attachments/assets/<hash>` を裸で置く)
       → プレイヤーが自動描画されるが、**前後を空行で挟んだブロックレベルの段落**
       でしか発火しない。表のセルに直接書くとただのリンクになる
    2. **`<details>` + 空行 + bare URL** → セル内でも発火するが、
       **セルの中の空行が markdown の表を分断する。**表全体が崩壊し、
       動画は表の外へはみ出す (実際にこれで README を壊した)
    3. **`<video src="...">` タグを直接書く** ← **これが正解。**GitHub は
       `<video>` をサニタイズしない。空行が要らないので 1 セル = 1 行に収まり、
       表を壊さない。折りたたみも不要で最初からプレイヤーが見えている

    **`width` 属性は GitHub 側で無効化される**ので、表示サイズは動画そのものの
    解像度で決める (`make_embeds.py` の `VIDEO_SIZE`)。
    """
    return f'<video src="{url}"></video>' if url else "—"


#: 漢字名の読み。**出典はキャラ設定資料の「本名」行に併記されたローマ字表記**
#: (Fumizuki Noa / Mikage Tsukasa / Hiiragi Ritsu / Sawatari Yume / Mikage Reika /
#: Mikage Suzu)。推測で振らないこと — **文月は「ふづき」ではなく「ふみづき」**で、
#: 月名の読みに引きずられると間違える。
#: 久遠だけ mascot.md に載っていないが、姓としては「くおん」で確定できる
#: (名の「かすみ」はキャラ id が固定している)。
#: **漢字を含まないキャラには振らない** (プリヤ・シャルマ / mossan_hoshi)。
NAME_KANA: dict[str, str] = {
    "noa": "ふみづき のあ",
    "tsukasa": "みかげ つかさ",
    "ritsu": "ひいらぎ りつ",
    "yume": "さわたり ゆめ",
    "reika": "みかげ れいか",
    "suzu": "みかげ すず",
    "kasumi": "くおん かすみ",
}

#: 表の前に置く注意書き。**GitHub は埋め込み動画に `muted` 属性を強制付与する**ので
#: (bare URL / `<video>` タグ / 表のセル、どの書き方でも同じ)、視聴者は再生してから
#: 🔊 を押さないと音が出ない。「動画は動くのに音が出ない」と誤解される実害が出たため
#: 明示する。**プレイヤー側では解除できない** — GitHub の描画側の仕様。
MUTE_NOTE = "\n".join([
    "> [!NOTE]",
    "> GitHub のプレイヤーは**既定でミュート**です。"
    "▶ を押したあと **🔊 を押すと音が出ます**。",
    ">",
    "> プレイヤーごとに押すのが面倒なら、**ブラウザの開発者ツール (F12) の"
    "コンソール**に下を貼って実行すると、このページの全プレイヤーが一度に"
    "ミュート解除されます (**音量を下げてから**どうぞ)。",
    ">",
    "> ```js",
    "> document.querySelectorAll('video').forEach(v => { v.muted = false; v.volume = 1 });",
    "> ```",
])

#: 音量の警告。**アプリ側 (`app.py` の結果再生欄) と同じ文言**にする。
#: 崩壊段はノイズ化して割れるので、音量を上げたまま鳴らすと痛い。**表の直前に出す**。
VOLUME_WARNING = "> [!WARNING]\n> ⚠️ 音量注意 (音割れ発生することがあります)"


def table(rows: tuple[dict, ...]) -> str:
    """README に貼る markdown。**1 行 = 1 キャラ、結果は 3 列 (通常/崩壊前夜/完全崩壊)。**

    - 崩壊が当たらなかった段は該当セルが `—` になる (キャラによって当たる段が違うため。
      §DEVNOTES 4 参照)。段ごとに列を割ってもここは変わらない — 1 列にまとめて
      「行を間引く」設計は「表の形が崩壊度によって変わる」という副作用があったので、
      3 列に固定して**抜けはセルの中で表現する**ほうに倒した
    - **読み上げテキストが全行同じなら列ごと落として表の外に 1 回だけ書く。**
      同じ文を 9 回並べても情報が無く、横幅を食ってプレイヤーを圧迫するだけ
      (プレイヤーの表示幅はセル幅で決まる)。行ごとに違う文を混ぜたときだけ列が復活する
    - シード値は**小さい文字** (`<sub>`) にする — 主役は音声で、これは補足でしかない
    - 参照音声・各結果セルはその場再生プレイヤー (`player_cell`)。
      **セルの中に空行を作らないこと** — 空行は markdown の表を分断する
    """
    urls = load_embed_urls()
    tier_heads = [f"{TIER_EMOJI[i]} {fx.EASY_STOPS[i][1]}" for i in range(len(TIER_SLUGS))]
    texts = [app.presets_for(r["char"])[preset_of(r)] for r in rows]
    shared = texts[0] if len(set(texts)) == 1 else None

    # **見出しの列数と実際に作るセル数を必ず一致させること。**食い違うと markdown の
    # 表が丸ごと壊れる (実際に "シード値" という見出しだけあってセルが無いバグで崩れた)。
    # シードは列を割らず各結果セルの中に添える (下の cells 参照)。
    columns = ([] if shared else ["読み上げテキスト"]) + ["参照音声", *tier_heads]
    # 音量の警告を先に出す (聴く前に知る必要があるのはこちら)。
    head = [VOLUME_WARNING, "", MUTE_NOTE, ""]
    if shared:
        head += [f"読み上げテキスト: <sub>{shared}</sub>", ""]
    out = head + ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    prev_text: str | None = None
    for row, text in zip(rows, texts):
        char = row["char"]
        text_cell = "〃" if text == prev_text else f"<sub>{text}</sub>"
        prev_text = text

        ref = app._REFS.get(f"{char}/{row['ref']}", {})
        dur = ref.get("duration_sec")
        kana = NAME_KANA.get(char)
        ref_cell = (
            f"<b>{app.CHARACTER_NAMES[char]}</b>"
            + (f" <sub>({kana})</sub>" if kana else "")
            + "<br>"
            + player_cell(urls.get(f"{char}_ref"))
            + (f"<br><sub>{dur:.1f}s</sub>" if dur else "")
        )

        cells = ([] if shared else [text_cell]) + [ref_cell]
        for idx in range(len(TIER_SLUGS)):
            if idx not in row["seeds"]:
                cells.append("—")
                continue
            seed = seed_of(row, idx)
            player = player_cell(urls.get(f"{char}_{TIER_SLUGS[idx]}"))
            cells.append(f"{player}<br><sub>seed {seed}</sub>" if seed else player)

        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out) + "\n"


#: README の差し込み口。**手でコピペしないこと** — 列数のずれや貼り損ねで
#: 表が丸ごと壊れる事故を実際に 2 回起こしている。
README_BEGIN = "<!-- SAMPLES:BEGIN -->"
README_END = "<!-- SAMPLES:END -->"


def patch_readme(body: str) -> Path:
    """README のマーカー間を差し替える。マーカーが無ければ落とす (黙って何もしない、を避ける)。"""
    path = repo() / "README.md"
    src = path.read_text(encoding="utf-8")
    head, sep, rest = src.partition(README_BEGIN)
    _, sep2, tail = rest.partition(README_END)
    if not (sep and sep2):
        raise SystemExit(f"README に {README_BEGIN} / {README_END} がありません")
    path.write_text(f"{head}{README_BEGIN}\n\n{body}\n{README_END}{tail}",
                    encoding="utf-8")
    return path


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", help="ローカル snapshot のパス (repo id は渡さない)")
    ap.add_argument("--chars", help="対象キャラをカンマ区切りで絞る")
    ap.add_argument("--seed", type=int, help="全行のシードを上書きして引き直す")
    ap.add_argument("--table-only", action="store_true", help="音は作らず表だけ組む")
    ap.add_argument("--verify", action="store_true",
                    help="既にあるサンプルを書き起こして崩壊したかを見る (生成しない)")
    ap.add_argument("--probe", help="当たりシードを探す。カンマ区切りのシード列")
    ap.add_argument("--ref", help="参照セットを上書き (探索用)")
    ap.add_argument("--preset", help="読ませる文のプリセット見出しを上書き (探索用)")
    ap.add_argument("--warp", type=float,
                    help="質感を壊す量を上書き (0〜1)。上げるほど低音になる")
    ap.add_argument("--tiers", default="eve,full",
                    help=f"対象の段を絞る ({','.join(TIER_SLUGS)})。"
                         "生成・探索の両方に効く")
    ap.add_argument("--only-tiers", action="store_true",
                    help="生成でも --tiers で絞る (1 段だけ引き直したいとき)")
    ap.add_argument("--verbose", action="store_true", help="生成のログを全部出す")
    ap.add_argument("--retry", type=int, default=0,
                    help="崩壊が不発だったらシードを振り直す回数 (固定シードには効かない)")
    ap.add_argument("--device", default="auto")
    args = ap.parse_args()

    rows = SAMPLES
    if args.chars:
        want = {s.strip() for s in args.chars.split(",") if s.strip()}
        rows = tuple(r for r in SAMPLES if r["char"] in want)
        if not rows:
            raise SystemExit(f"該当するキャラがありません: {sorted(want)}")
    if args.seed is not None:
        rows = tuple({**r, "seed": args.seed} for r in rows)
    for key, val in (("ref", args.ref), ("preset", args.preset), ("warp", args.warp)):
        if val is not None:
            rows = tuple({**r, key: val} for r in rows)
    if args.only_tiers:
        # **`keep` ではなく `seeds` を絞る。**残す段を keep に足す形だと、
        # 段ごとに keep を書き換えることになって「採用済みかどうか」の意味が濁る。
        want = {TIER_SLUGS.index(t.strip()) for t in args.tiers.split(",")}
        rows = tuple({**r, "seeds": {i: s for i, s in r["seeds"].items() if i in want},
                      "keep": ()} for r in rows)
        rows = tuple(r for r in rows if r["seeds"])

    app._REFS = app.load_refs(
        [str(p) for p in sorted((repo() / "refs").glob("*/refs.json"))])

    if args.verify:
        verify(rows)
        return

    if not args.table_only:
        device, dtype = app.pick_device(args.device)
        print(f"デバイス {device} / {str(dtype).replace('torch.', '')}", flush=True)
        from qwen_tts import Qwen3TTSModel

        print("モデルを読み込み中...", flush=True)
        app._MODEL = Qwen3TTSModel.from_pretrained(
            app.resolve_model(args.model), device_map=device, dtype=dtype,
            attn_implementation="sdpa")
        if args.probe:
            seeds = tuple(int(s) for s in args.probe.split(",") if s.strip())
            tiers = tuple(TIER_SLUGS.index(t.strip()) for t in args.tiers.split(","))
            probe(rows, seeds, tiers)
            return
        generate(rows, verbose=args.verbose, retry=args.retry)

    # 表は**常に全行**で書き出す (一部だけ作り直したときに表が縮まないように)
    md = repo() / SAMPLE_DIR / "table.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    body = table(SAMPLES)
    md.write_text(body, encoding="utf-8")
    print(f"\n表を書き出した: {md}")
    print(f"README を更新した: {patch_readme(body)}")


if __name__ == "__main__":
    main()
