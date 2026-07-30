#!/usr/bin/env python
"""参照音声の加工と、崩壊音声の合成に使う信号処理をまとめる。

`17_probe_collapse_ref.py` (探査) と `gui_qwen_tts.py` (GUI) の両方から使う。
数字始まりのモジュールは import できないため、共有物はここに置く。

**採用した崩壊レシピ**は `ramp_reverse`。0.25 秒ごとに区切り、後半へ行くほど
逆再生する参照を作る。静的に壊した参照 (全体を逆再生など) はモデルが「壊れた状態」で
定常化してしまい途中から進行が止まるが、参照自体に傾きがあると崩れ方も進行し続ける。

崩壊は確率的で、**参照が長いほど確実**に効く (実測: 11.5 秒の参照で 3/3、
3.8 秒で 2/3、5.0 秒で 1/3)。短い参照では引き直しが要る。
"""

from __future__ import annotations

import numpy as np

SR = 24000
TRIM_THRESH = 0.01     # ピーク比。これ未満を端の無音とみなす
XFADE_SEC = 0.02


# --------------------------------------------------------------------------
# 参照音声の下ごしらえ
# --------------------------------------------------------------------------

def trim_edges(y: np.ndarray, thresh: float = TRIM_THRESH) -> np.ndarray:
    """クリップ前後の無音を落とす。

    ICL は参照の音響特性をそのまま写すので、**端の無音も「この話者の喋り方」として
    学習される**。落とさないと出力の読点が 0.4〜0.9 秒に伸びる (実測)。
    """
    if y.size == 0:
        return y
    loud = np.abs(y) > np.abs(y).max() * thresh
    idx = np.flatnonzero(loud)
    return y[idx[0]: idx[-1] + 1] if idx.size else y


def crossfade(a: np.ndarray, b: np.ndarray, sr: int = SR,
              sec: float = XFADE_SEC) -> np.ndarray:
    """境界のクリックを消して繋ぐ。無音を挟まないこと (挟むと ICL が写す)。"""
    n = min(int(sec * sr), len(a), len(b))
    if n <= 0:
        return np.concatenate([a, b])
    ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
    mid = a[-n:] * (1.0 - ramp) + b[:n] * ramp
    return np.concatenate([a[:-n], mid, b[n:]]).astype(np.float32)


def normalize_peak(y: np.ndarray, dbfs: float = -1.0) -> np.ndarray:
    peak = float(np.max(np.abs(y))) if y.size else 0.0
    return (y * (10 ** (dbfs / 20.0) / peak)).astype(np.float32) if peak > 0 else y


# --------------------------------------------------------------------------
# 崩壊エフェクト (参照音声を壊す)
# --------------------------------------------------------------------------

def ramp_reverse(y: np.ndarray, sr: int = SR, chunk_sec: float = 0.50,
                 start: float = 0.50, hard: float = 0.9) -> np.ndarray:
    """後ろへ行くほど長い区間を逆再生する。**採用レシピ。**

    start … この割合を過ぎた区間から壊し始める
    hard  … この割合を過ぎたら全区間を逆再生する (それ以前は 1 つ飛ばし)

    **既定値は「崩壊するが音が汚れない」点で選んである。**強く壊すほど出力が
    ノイズ化し、音割れとして聞こえる。7 条件 × 2 シードの実測 (`sweep_collapse`、
    HF% = 6kHz 以上のエネルギー比 / SFM = スペクトル平坦度):

    | chunk / start / hard | 崩壊 | HF% | SFM | 張り付き |
    |---|---|---|---|---|
    | 偶発版 (目標) | ○ | 0.53 | 0.0214 | 1 |
    | **0.50 / 0.50 / 0.9 (既定)** | **2/2** | **1.56〜1.59** | **0.0212〜0.0217** | 0〜1 |
    | 0.25 / 0.25 / 0.6 (旧既定) | 2/2 | 3.93〜5.57 | 0.0884〜0.1022 | 3〜7 |
    | 0.10 / 0.40 / 1.0 | **0/2** | — | — | — |

    細かく刻むと崩壊しなくなり、粗く強く壊すとノイズ化する。**間を取った値。**
    """
    c = max(1, int(chunk_sec * sr))
    total = max(len(y) / c, 1)
    out, i, k = [], 0, 0
    while i < len(y):
        seg = y[i: i + c]
        pos = k / total
        if pos > start and (k % 2 == 0 or pos > hard):
            seg = seg[::-1]
        out.append(seg)
        i += c
        k += 1
    return np.concatenate(out).astype(np.float32) if out else y


#: `warp=1.0` のときの終端速度。`ramp_speed` の既定と同じ。
WARP_END_FACTOR = 0.45


def warp_texture(y: np.ndarray, amount: float = 0.0) -> np.ndarray:
    """後半へ向けて再生速度を落とす。**既定では使わない** (`EASY_STOPS` は全段 0)。

    **速度が変わると同一人物に聞こえなくなるので、演出として却下した** (2026-07-30)。
    崩壊音は「そのキャラが壊れていく」ものであって、別人になっては意味がない。
    詳細設定のつまみとしては残してあるが、上げるほど低音になる (終端 `WARP_END_FACTOR`
    倍速 = 実測でスペクトル重心 1635 → 1109) ことを承知の上で使うもの。

    以下は導入時の理屈 (記録として残す):

    `ramp_reverse` は**時間の順番を入れ替えるだけ**なので、スペクトルは元のまま
    = **声色 (誰の声か) は完全に残る**。「完全崩壊なのに元の声の質感が残る」のは
    レシピの強さ不足ではなく**原理的な限界**で、`start` をいくら下げても消えない。

    質感まで動かすにはスペクトルを触る必要がある。ここで速度を落とすと
    ピッチとフォルマントが一緒に下がり、**参照の声そのものが別物になる**。
    ノイズや量子化で壊す手もあるが、**あれは高域にエネルギーを足すので
    音割れの原因になる**。速度変化は再標本化なのでエネルギーを足さない。

    amount … 0 で無加工、1 で終端 `WARP_END_FACTOR` 倍速まで落ちる。
    **音は長くなる** (遅くなるので)。長さの上限は呼び出し側で切ること。
    """
    if amount <= 0.0 or len(y) < 2:
        return y
    end = 1.0 + (WARP_END_FACTOR - 1.0) * min(float(amount), 1.0)
    return ramp_speed(y, end)


def ramp_noise(y: np.ndarray, rng: np.random.Generator,
               end_snr_db: float = -6.0) -> np.ndarray:
    """ノイズが時間とともに増える。頭はきれい、尻は埋もれる。"""
    sig = float(np.sqrt(np.mean(y**2) + 1e-12))
    n = rng.standard_normal(len(y)).astype(np.float32)
    n /= float(np.sqrt(np.mean(n**2) + 1e-12))
    amp = sig / (10 ** (end_snr_db / 20.0))
    return (y + n * amp * np.linspace(0.0, 1.0, len(y), dtype=np.float32)).astype(np.float32)


def ramp_speed(y: np.ndarray, end_factor: float = 0.45) -> np.ndarray:
    """再生速度が連続的に落ちる (テープが止まっていく音)。ピッチも一緒に下がる。"""
    n = len(y)
    steps, pos, guard = [], 0.0, 0
    while pos < n - 1:
        steps.append(pos)
        f = 1.0 + (end_factor - 1.0) * (pos / max(n - 1, 1))
        pos += max(f, 0.05)
        guard += 1
        if guard > n * 8:
            break
    return np.interp(np.asarray(steps, dtype=np.float32),
                     np.arange(n, dtype=np.float32), y).astype(np.float32)


def ramp_bitcrush(y: np.ndarray, end_bits: float = 3.0) -> np.ndarray:
    """量子化が時間とともに粗くなる。デジタルが壊れていく質感。"""
    bits = np.linspace(16.0, end_bits, len(y), dtype=np.float32)
    step = 2.0 / np.power(2.0, bits)
    return (np.round(y / step) * step).astype(np.float32)


def ramp_dropout(y: np.ndarray, rng: np.random.Generator, sr: int = SR,
                 chunk_sec: float = 0.06) -> np.ndarray:
    """後ろへ行くほど頻繁に音が欠落する。回線が切れていく質感。"""
    c = max(1, int(chunk_sec * sr))
    out, i, total = [], 0, max(len(y), 1)
    while i < len(y):
        seg = y[i: i + c].copy()
        if rng.random() < (i / total) * 0.8:
            seg[:] = 0.0
        out.append(seg)
        i += c
    return np.concatenate(out).astype(np.float32) if out else y


def full_reverse(y: np.ndarray) -> np.ndarray:
    return y[::-1].copy()


# --------------------------------------------------------------------------
# かんたん設定 (崩壊の強さを 1 本のつまみに畳む)
# --------------------------------------------------------------------------

# つまみ位置と `ramp_reverse` のパラメータの対応。**間は線形補間する**ので、
# 3 段階ではなく連続的に強さを選べる。`start` を下げるほど壊れる方向で単調なので、
# これがそのまま強度軸になる (`start=1.0` はどの区間も逆再生されない = 素の生成)。
#
# **`warp` は全段 0。**声色を壊す軸だが、速度が変わると**同一人物に聞こえなくなる**ので
# 演出として成立しない (2026-07-30 に耳で却下)。「完全崩壊なのに元の声の質感が残る」は
# 残るが、**誰の声か分からなくなるくらい壊すのは目的ではない** (`warp_texture` 参照)。
# 強さは `start` だけで付ける。
#
# **`start=0.65` (中段を弱める) は試して却下した** (2026-07-30)。「完全崩壊との差が
# 耳で付かない」を弱める方向で解こうとしたが、弱めても差が付かず崩れ方が薄くなるだけ。
# 中段は 0.50 のまま。**触るなら 0.75 を超えないこと** —
# `TILE_COLLAPSE_START_MAX` を超えるとタイルの絵が素のままになり、音は壊れているのに
# 絵が通常という食い違いが起きる。
EASY_STOPS: tuple[tuple[float, str, dict[str, float]], ...] = (
    (0.0, "通常",     dict(chunk_sec=0.50, start=1.00, hard=1.00, warp=0.0)),
    (1.0, "崩壊前夜", dict(chunk_sec=0.50, start=0.50, hard=0.90, warp=0.0)),
    # **`chunk_sec` は 0.50 で通す** (旧 0.25)。細かく刻むと崩壊しなくなる
    # (実測 0.10/0.40/1.0 で 0/2、0.25 では「50% では崩れるのに 100% で崩れない」
    # 逆転が実際に起きた)。強さは `start` だけで付ける。
    (2.0, "完全崩壊", dict(chunk_sec=0.50, start=0.25, hard=0.60, warp=0.0)),
)
EASY_MIN, EASY_MAX = EASY_STOPS[0][0], EASY_STOPS[-1][0]

# これ未満は素の生成として扱う。`start≈1.0` でも実質は無加工だが、
# エフェクト有りの経路に入ると「冒頭は正常」で**生成が 2 回走ってしまう**ため明示的に外す。
EASY_NONE_BELOW = 0.05

#: キャラタイルを崩壊版の絵に差し替える境目 (`start` がこれ以下なら崩壊版)。
#: 通常 `start=1.00` と崩壊前夜 `start=0.50` の中点。**`start` は下げるほど壊れる**
#: 向きに単調なので、かんたん設定と詳細設定のどちらから来ても同じ軸で判定できる。
TILE_COLLAPSE_START_MAX = 0.75

#: なれの果て (骸骨 / 肉片) に差し替える境目。崩壊前夜 `start=0.50` と
#: 完全崩壊 `start=0.25` の中点。
TILE_GORE_START_MAX = 0.375

#: タイルの 3 状態。値はそのまま画像を置くフォルダ名になる。
TILE_NORMAL, TILE_COLLAPSE, TILE_GORE = "chibi", "chibi_collapse", "chibi_gore"


def tile_state(effect: str, start: float) -> str:
    """つまみの位置からタイルに出す絵の種別を返す。

    素の生成 (`effect="none"`) では絶対に差し替えない — 音が普通なのに絵だけ
    崩れていると、どちらが今の設定なのか読めなくなる。
    """
    if effect == "none":
        return TILE_NORMAL
    s = float(start)
    if s <= TILE_GORE_START_MAX:
        return TILE_GORE
    return TILE_COLLAPSE if s <= TILE_COLLAPSE_START_MAX else TILE_NORMAL


def easy_params(t: float) -> tuple[str, float, float, float, float]:
    """つまみ位置 (0〜2) を (効果, chunk_sec, start, hard, warp) に写す。"""
    t = min(max(float(t), EASY_MIN), EASY_MAX)
    lo, hi = EASY_STOPS[0], EASY_STOPS[-1]
    for a, b in zip(EASY_STOPS, EASY_STOPS[1:]):
        if t <= b[0]:
            lo, hi = a, b
            break
    span = hi[0] - lo[0]
    r = (t - lo[0]) / span if span > 0 else 0.0
    p = {k: lo[2][k] + (hi[2][k] - lo[2][k]) * r for k in lo[2]}
    effect = "none" if t < EASY_NONE_BELOW else "ramp_reverse"
    return effect, p["chunk_sec"], p["start"], p["hard"], p["warp"]


def easy_label(t: float) -> str:
    """つまみ位置の表示名。アンカー上なら名前、間なら両隣を併記する。"""
    t = min(max(float(t), EASY_MIN), EASY_MAX)
    if t < EASY_NONE_BELOW:
        # `easy_params` がここを素の生成に倒すので、表示も「通常」で揃える
        return EASY_STOPS[0][1]
    for pos, name, _ in EASY_STOPS:
        if abs(t - pos) < 0.02:
            return name
    lo, hi = EASY_STOPS[0], EASY_STOPS[-1]
    for a, b in zip(EASY_STOPS, EASY_STOPS[1:]):
        if t <= b[0]:
            lo, hi = a, b
            break
    span = hi[0] - lo[0]
    r = (t - lo[0]) / span if span > 0 else 0.0
    return f"{lo[1]} → {hi[1]}  {r*100:.0f}%"


# 参照の壊し方。GUI のプルダウンはここを見る。
REF_EFFECTS: dict[str, str] = {
    "none": "壊さない (通常の生成)",
    "ramp_reverse": "後半ほど逆再生 (段階的に崩れる。採用レシピ)",
    "ramp_noise": "ノイズが増えていく",
    "ramp_speed": "速度が落ちていく (テープが止まる)",
    "ramp_bitcrush": "量子化が粗くなっていく",
    "ramp_dropout": "音が欠落していく",
    "full_reverse": "全体を逆再生 (途中で定常化する)",
}


def apply_ref_effect(y: np.ndarray, effect: str, sr: int = SR,
                     seed: int = 0, chunk_sec: float = 0.50,
                     start: float = 0.50, hard: float = 0.9,
                     warp: float = 0.0) -> np.ndarray:
    """壊し方を 1 つ選んで適用し、**最後に質感を歪める** (`warp`)。

    `warp` は効果とは独立の軸。**時間を入れ替える系の効果では声色が残る**ため、
    それを崩したいときだけ足す (詳細は `warp_texture`)。
    """
    rng = np.random.default_rng(seed)
    if effect in ("none", "", None):
        return y
    if effect == "ramp_reverse":
        out = ramp_reverse(y, sr, chunk_sec, start, hard)
    elif effect == "ramp_noise":
        out = ramp_noise(y, rng)
    elif effect == "ramp_speed":
        out = ramp_speed(y)
    elif effect == "ramp_bitcrush":
        out = ramp_bitcrush(y)
    elif effect == "ramp_dropout":
        out = ramp_dropout(y, rng, sr)
    elif effect == "full_reverse":
        out = full_reverse(y)
    else:
        raise ValueError(f"未知の効果: {effect}")
    return warp_texture(out, warp)


#: 崩壊させた参照をここまで伸ばす (秒)。**崩壊の効きは参照の長さに強く依存する**
#: (実測: 発話 11.5 秒で 3/3、5.0 秒で 1/3、3.8 秒で 2/3)。短い参照でも同じ土俵に
#: 乗せるため、崩壊部分を継ぎ足してこの長さに揃える。
TARGET_BROKEN_SEC = 11.0

#: 継ぎ足しブロックを切り出す位置のずらし幅 (元音声長に対する比)。
#: **黄金比の小数部**。整数比だと数ブロックで元の位置に戻り、実質リピートになる。
_OFFSET_STRIDE = 0.6180339887


#: 崩壊させた参照の長さの上限 (秒)。`warp` を掛けると音が伸びるので、
#: prefill と KV キャッシュが膨らんで VRAM 8GB の生成側が足りなくなるのを防ぐ。
MAX_BROKEN_SEC = 20.0


def build_broken_ref(y: np.ndarray, effect: str, sr: int = SR, seed: int = 0,
                     chunk_sec: float = 0.50, start: float = 0.50,
                     hard: float = 0.9, warp: float = 0.0,
                     target_sec: float = TARGET_BROKEN_SEC,
                     max_sec: float = MAX_BROKEN_SEC) -> tuple[np.ndarray, int]:
    """崩壊させた参照を作り、短ければ**崩壊部分を継ぎ足して** `target_sec` へ伸ばす。

    戻り値は `(波形, 継ぎ足したブロック数)`。

    **単純なリピートにはしない。**同じ波形を並べると ICL がその繰り返し自体を
    写してしまう。継ぎ足すブロックは元音声を**毎回違うオフセットで回して**から
    同じレシピで壊すので、壊れ方は同種のまま音としては別物になる。

    継ぎ足す側は `start=0.0` で作る。**すでに崩壊しきった後ろに繋ぐ区間**なので、
    そこだけ健全な頭を持たせると崩壊が一度戻ってしまう。`hard` はそのまま渡し、
    元の終端より強くは壊さない (強く壊すほどノイズ化して音割れになる)。
    """
    broken = apply_ref_effect(y, effect, sr, seed, chunk_sec, start, hard, warp)
    need, cap = int(target_sec * sr), int(max_sec * sr)
    if effect in ("none", "", None) or len(y) < sr // 2:
        return broken, 0
    if len(broken) >= need:
        return broken[:cap].astype(np.float32), 0

    out, blocks = broken, 0
    while len(out) < need:
        blocks += 1
        off = int((_OFFSET_STRIDE * blocks) % 1.0 * len(y))
        blk = apply_ref_effect(np.roll(y, -off), effect, sr, seed + blocks,
                               chunk_sec, 0.0, hard, warp)
        out = crossfade(out, blk, sr)
    return out[:need].astype(np.float32), blocks


# --------------------------------------------------------------------------
# 正常音声と崩壊音声の接合
# --------------------------------------------------------------------------

def _rms_envelope(y: np.ndarray, sr: int, win_sec: float = 0.01) -> np.ndarray:
    w = max(1, int(win_sec * sr))
    n = len(y) // w
    if n == 0:
        return np.zeros(1, dtype=np.float32)
    return np.array(
        [np.sqrt(np.mean(y[i * w:(i + 1) * w] ** 2) + 1e-12) for i in range(n)],
        dtype=np.float32,
    )


def pick_loud_window(y: np.ndarray, sr: int = SR, max_sec: float = 20.0,
                     win_sec: float = 0.01) -> np.ndarray:
    """長い音声から**しっかり喋っている区間**を `max_sec` ぶん切り出す。

    **先頭から機械的に切ってはいけない。**前置きの無音・空調・息だけを掴んで、
    「参照音声なのにほとんど喋っていない」ものが出来上がる。

    10ms 単位の RMS を積分して**平均エネルギーが最大になる窓**を選び、そのうえで
    端を整える: 頭は最初に声が立ち上がる位置まで進め、尻は直近の無音まで戻す
    (語の途中で切ると不自然な参照になる)。
    """
    n_max = int(max_sec * sr)
    if len(y) <= n_max:
        return y

    env = _rms_envelope(y, sr, win_sec)
    w = min(int(max_sec / win_sec), env.size)
    if w <= 0 or env.size <= w:
        return y[:n_max]

    # 窓ごとの平均エネルギー。累積和で一度に出す。
    csum = np.concatenate([[0.0], np.cumsum(env.astype(np.float64))])
    means = (csum[w:] - csum[:-w]) / w
    s = int(np.argmax(means))
    e = s + w

    thresh = float(env.max()) * 0.08
    # 頭: 声が立ち上がるまで進める (最大 2 秒ぶん)
    limit = s + int(2.0 / win_sec)
    while s < min(e - 1, limit) and env[s] < thresh:
        s += 1
    # 尻: 直近の無音まで戻す (最大 1.5 秒ぶん)。無音が無ければそのまま。
    back = max(s + 1, e - int(1.5 / win_sec))
    for i in range(e - 1, back - 1, -1):
        if env[i] < thresh:
            e = i + 1
            break

    return y[int(s * win_sec * sr): int(e * win_sec * sr)]


#: 無音とみなす閾値 (区間内の最大 RMS に対する比)。
BREAK_QUIET_RATIO = 0.08


def find_break_point(y: np.ndarray, sr: int, lo_sec: float, hi_sec: float) -> int:
    """[lo_sec, hi_sec] の中から**いちばん区切りのいい位置**を選んで返す。

    息継ぎ・読点の無音を狙う。**候補をランダムに選ばない** — 範囲内には浅い谷も
    深い息継ぎも混在しており、ランダムだと語の途中の小さな谷を掴んで
    「ぶつっと切り替わった」接合になる。**いちばん長く静かな区間**を採り、
    同じ長さなら**より静かなほう**を採る (長い無音 = 本物の息継ぎ・句点)。

    候補が無ければ**範囲内でいちばん静かなフレーム**を採る (無音が無い = 切れ目が
    無い音なので、せめて谷で切る)。ここもランダムにしない。
    """
    env = _rms_envelope(y, sr)
    if env.size == 0:
        return min(len(y), int(hi_sec * sr))
    win = 0.01
    lo, hi = int(lo_sec / win), min(int(hi_sec / win), env.size - 1)
    if hi <= lo:
        return min(len(y), int(hi_sec * sr))

    seg = env[lo:hi + 1]
    quiet = np.flatnonzero(seg < float(env.max()) * BREAK_QUIET_RATIO)
    if quiet.size:
        # 静かなフレームが連続する塊に分ける
        groups, cur = [], [int(quiet[0])]
        for a, b in zip(quiet, quiet[1:]):
            if b == a + 1:
                cur.append(int(b))
            else:
                groups.append(cur)
                cur = [int(b)]
        groups.append(cur)
        # 長さ優先・同じ長さなら静かなほう。塊の中央で切る (端で切ると尻切れになる)
        best = max(groups, key=lambda g: (len(g), -float(np.mean(seg[g]))))
        idx = lo + best[len(best) // 2]
    else:
        idx = lo + int(np.argmin(seg))
    return int(min(len(y), idx * win * sr))


#: 崩壊側を探す幅 (秒)。正常側の接合時刻からこの範囲で区切りを探す。
#: **正常側より広く取る** — 崩壊側は息継ぎが崩れていて、狭いと谷が 1 つも無い。
COLLAPSE_BREAK_SPAN = 0.6


def splice_normal_then_collapse(
    normal: np.ndarray, collapsed: np.ndarray, sr: int,
    head_lo_sec: float = 2.0, head_hi_sec: float = 5.0,
    seed: int = 0, xfade_sec: float = 0.06,
) -> tuple[np.ndarray, float]:
    """**冒頭は正常な推論結果、その後ろに崩壊音声を繋ぐ。**

    崩壊は早く始まりすぎることが多いので、頭の数秒だけ正常な生成を使う。
    接合位置は**正常側でいちばん区切りのいいところ** (長く静かな区間 = 息継ぎ・句点)
    を自動で選び、崩壊側も同じ時刻付近の区切りから採る (同じ語を二度言わないため)。
    **どちらもランダムには選ばない** (`find_break_point` 参照) — 無音で切って
    無音に繋ぐことで、頭から崩壊へシームレスに移る。

    `seed` は受け取るが**接合位置には使わない** (決定的に選ぶ)。同じ生成結果に対して
    同じ接合になるほうが、崩壊の当たり外れをシードで引き直すときに切り分けやすい。

    戻り値は (音声, 接合位置の秒)。
    """
    if normal.size == 0:
        return collapsed, 0.0

    hi = min(head_hi_sec, len(normal) / sr)
    lo = min(head_lo_sec, max(hi - 0.5, 0.0))
    cut = find_break_point(normal, sr, lo, hi)
    head = normal[:cut]

    # 崩壊側も同じ時刻の付近から採る。崩壊側の時間軸は正常側と一致しないが、
    # 崩壊音は元々テキストに対応していないので、おおよそで足りる。
    t = cut / sr
    if len(collapsed) / sr <= t + 0.5:
        tail = collapsed
    else:
        c_cut = find_break_point(collapsed, sr, max(t - COLLAPSE_BREAK_SPAN, 0.0),
                                 min(t + COLLAPSE_BREAK_SPAN, len(collapsed) / sr))
        tail = collapsed[c_cut:]

    return crossfade(head, tail, sr, xfade_sec), cut / sr
