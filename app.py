#!/usr/bin/env python
"""Qwen3-TTS のゼロショット生成を GUI から回す。

やれること:
  - 任意のテキストを、任意の参照音声 (キャラのセット / 手持ちの音声ファイル) で読ませる
  - 崩壊エフェクトをかけて壊れた音声を作る (パラメータは全部スライダで振れる)
  - **冒頭 N 秒だけ正常な推論結果を使い、その後ろに崩壊音声を繋ぐ**

最後の一つが要点。崩壊は早く始まりすぎることが多いので、頭は普通に喋らせて、
区切りのいいところ (息継ぎ・読点) から崩し始める。接合位置は自動で探し、
指定した範囲内でランダムに選ぶ。

起動:
    python app.py --model <ローカル snapshot パス> [--refs <refs.json>[,...]] [--native]

**--model に HF の repo id を渡してはならない。** model_info() の通信で数分ハングする。
必ずローカル snapshot のパスを渡すこと。省略時は下記の順で探す:
  1. 環境変数 QWEN_TTS_MODEL
  2. 実行ファイル (または本ファイル) と同じ場所の `model/`
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import socket
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import soundfile as sf
import torch

import collapse_fx as fx

APP_TITLE = "たのしい🔞崩壊メーカー"
NOVTUBE_LOGO = "novtube_logo.webp"    # ヘッダの "presented by" に出す本体ロゴ
LANGUAGE = "japanese"
FRAMES_PER_SEC = 12.5     # 12.5Hz コーデック。max_new_tokens / 12.5 = 生成できる秒数
UPLOAD = "▼ ファイルから読み込む"

# 冒頭を正常な推論にする範囲の既定 (秒)。この範囲の中から**いちばん区切りのいい位置**
# (長く静かな区間 = 息継ぎ・句点) を選んで崩壊へ繋ぐ (`collapse_fx.find_break_point`)。
# **狭くしないこと** — 谷が 1 つも無い範囲を引くと語の途中で切れて接合が目立つ。
HEAD_DEFAULT_LO, HEAD_DEFAULT_HI = 2.0, 5.0

# 参照音声の長さの上限 (秒)。**用意してあるセットは最長 15.9 秒**で、それ以上長くても
# 崩壊の効きは変わらない。長い音声をそのまま渡すと prefill と KV キャッシュが伸びて
# VRAM 8GB では生成側が足りなくなるため、アップロードされた音声はここで切る。
MAX_REF_SEC = 20.0

# 読ませる文のプリセット。**キャラの性別で言い回しを切り替える。**
# **長い文ほど崩壊が深くなる** (崩壊は生成が進むほど進行するため)。短文だと崩れきる前に終わる。
#
# **ラベルに性別は書かない。**キャラを選んだ時点で性別は決まっているので、
# 選択肢に併記すると冗長になる (長 / 中 / 短 は生成尺に効くので残す)。
# 例外は「あなた専用キャラ」だけ — 性別が分からないので**両方**出し、
# そこでは `男性・` / `女性・` を頭に付けて区別する。

#: 全セットの末尾に足す共通プリセット (性別に依存しない検証用の地の文)。
_NARRATION: dict[str, str] = {
    "地の文 (t2・検証で最良)": (
        "窓の外では雨が降り続いていて、遠くの山はもう見えなくなっていた。"
        "彼女は本を閉じて、しばらくそのまま座っていた。"
    ),
}

_FEMALE: dict[str, str] = {
    "長: 誕生日": (
        "お誕生日おめでとう！ずっとこの日を楽しみにしてたんだよ。"
        "はい、これプレゼント。開けてみて。気に入ってくれるといいんだけど。"
    ),
    "長: 告白": (
        "あのね、ずっと前から、あなたのことが好きでした。"
        "言おうと思って、そのたびに言えなくて。"
        "でも今日は、どうしてもちゃんと伝えたかったの。"
    ),
    "長: 合格": (
        "受かった！ほんとうに受かったの！"
        "いちばん最初にあなたに伝えたくて、走って帰ってきちゃった。"
        "ありがとう。ずっと支えてくれてたから、がんばれたんだよ。"
    ),
    "長: 再会": (
        "ひさしぶり！うわ、ぜんぜん変わってないね。"
        "会いたかったよ、ほんとうに。今日この後、時間ある？"
        "積もる話が、それこそ山ほどあるんだから。"
    ),
    "中: プロポーズ": (
        "これからは、ずっと一緒にいてください。"
        "どんな日でも、あなたのとなりで笑っていたいんです。"
    ),
    "短: ありがとう": "今日はほんとうにありがとう。すごく楽しかった。また誘ってね。",
}

_MALE: dict[str, str] = {
    "長: 誕生日": (
        "誕生日おめでとう。ずっとこの日を楽しみにしてたんだ。"
        "ほら、これ。開けてみてくれよ。気に入るといいんだけどな。"
    ),
    "長: 告白": (
        "ずっと前から、君のことが好きだった。"
        "何度も言おうとして、そのたびに言えなくて。"
        "でも今日は、どうしてもちゃんと伝えたかったんだ。"
    ),
    "長: 合格": (
        "受かった。ほんとうに受かったんだ。"
        "いちばん最初に君に言いたくて、走って帰ってきた。"
        "ありがとう。ずっと支えてくれてたから、がんばれたんだよ。"
    ),
    "長: 再会": (
        "久しぶりだな。うわ、全然変わってないじゃないか。"
        "会いたかったよ、ほんとに。今日この後、時間あるか？"
        "話したいことが、それこそ山ほどあるんだ。"
    ),
    "中: プロポーズ": (
        "これからは、ずっと一緒にいてほしい。"
        "どんな日でも、君のとなりで笑っていたいんだ。"
    ),
    "短: ありがとう": "今日はほんとうにありがとう。すごく楽しかった。また誘ってくれよ。",
}

TEXT_PRESET_SETS: dict[str, dict[str, str]] = {
    "female": {**_FEMALE, **_NARRATION},
    "male": {**_MALE, **_NARRATION},
    # あなた専用キャラ用。**ここだけ性別を併記する** (どちらの声かは選ぶ人しか知らない)。
    "both": {**{f"女性・{k}": v for k, v in _FEMALE.items()},
             **{f"男性・{k}": v for k, v in _MALE.items()},
             **_NARRATION},
}

#: キャラ ID → プリセット群。**性別の SoT はキャラ設定資料**で、ここは写しでしかない。
CHARACTER_PRESET_GROUP: dict[str, str] = {
    "noa": "female",       # 文月 乃亜
    "ritsu": "female",     # 柊 律
    "priya": "female",     # プリヤ・シャルマ
    "yume": "female",      # 沢渡 ゆめ
    "reika": "female",     # 御影 怜香
    "suzu": "female",      # 御影 すず
    "kasumi": "female",    # 久遠 霞
    "tsukasa": "male",     # 御影 司
}
#: あなた専用キャラ / 未知のキャラ。性別が分からないので両方出す。
DEFAULT_PRESET_GROUP = "both"


def preset_group(char_id: str) -> str:
    return CHARACTER_PRESET_GROUP.get(char_id, DEFAULT_PRESET_GROUP)


def presets_for(char_id: str) -> dict[str, str]:
    return TEXT_PRESET_SETS[preset_group(char_id)]

_MODEL = None
_REFS: dict[str, dict] = {}
_OUT_DIR: Path | None = None

#: 崩壊版タイルを持っているキャラ。起動時に `assets/chibi_collapse/` を見て埋める。
COLLAPSE_TILE_CHARS: set[str] = set()

#: なれの果て (完全崩壊) のタイルを持っているキャラ。`assets/chibi_gore/`。
#: **ここに置く画像はモザイクを焼き込んだもの** (素のままは配布物に入れない)。
GORE_TILE_CHARS: set[str] = set()

#: タイル画像 URL に付ける版。**画像を作り直してもファイル名が変わらない**ため、
#: これが無いとブラウザが古い絵をキャッシュから出し続け、
#: 「差し替えたのに画面が変わらない」になる (実際に起きた)。
#: 中身が変わったときだけ変わるよう、素材の最終更新時刻から作る。
ASSET_VER = "0"


def app_dir() -> Path:
    """配布物の置き場所。PyInstaller で固めた場合は exe のあるフォルダ。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def resolve_model(arg: str | None) -> str:
    for cand in (arg, os.environ.get("QWEN_TTS_MODEL"), str(app_dir() / "model")):
        if cand and Path(cand).exists():
            return str(cand)
    raise SystemExit(
        "モデルが見つかりません。次のいずれかを用意してください:\n"
        "  --model <ローカル snapshot のパス>\n"
        "  環境変数 QWEN_TTS_MODEL\n"
        f"  {app_dir() / 'model'} にモデルを置く\n"
        "※ HF の repo id は渡さないでください (通信待ちで数分ハングします)"
    )


def pick_device(pref: str = "auto") -> tuple[str, torch.dtype]:
    """CUDA / MPS / CPU を選ぶ。

    Mac (Apple Silicon) には CUDA が無いので MPS に落ちる。**MPS での動作は未検証**で、
    CPU にさらに落ちると実用にならない速度になる (1.7B の自己回帰デコード)。
    """
    if pref != "auto":
        dev = pref
    elif torch.cuda.is_available():
        dev = "cuda:0"
    elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        dev = "mps"
    else:
        dev = "cpu"
    # fp16 は CPU で極端に遅くなるので CPU のときだけ fp32
    return dev, (torch.float32 if dev == "cpu" else torch.float16)


def load_refs(paths: list[str]) -> dict[str, dict]:
    """`--refs` で渡された refs.json を読む。

    **`CHARACTER_NAMES` に載っているキャラの参照しか読まない (許可リスト)。**
    このツールに出すキャラは意図して選んであり、素材が手元にあるからといって
    出していいわけではない。`--refs` は外から自由に渡せるので、ここで弾かないと
    「渡せば出る」構造のままになる (実際に読み込ませてしまったことがある)。
    **除外リストは持たない** — 出さないと決めたキャラの名前がコードに残ってしまうため。
    """
    out: dict[str, dict] = {}
    skipped = 0
    for p in paths:
        path = Path(p)
        if not path.exists():
            print(f"[warn] refs が無い: {path}")
            continue
        # 同名セットが複数ファイルにあるので、どの refs.json 由来か分かる名前にする
        tag = path.parent.name
        if char_of_group(tag) not in CHARACTER_NAMES:
            skipped += 1          # **名前もパスも出さない** (出さないと決めた側の情報になる)
            continue
        for r in json.loads(path.read_text(encoding="utf-8")):
            # **`wav` は相対パスを許す** (その refs.json の置き場所からの相対)。
            # 配布物に絶対パスは書けない — 作った機械のパスが埋まってしまう。
            wav = Path(r["wav"])
            if not wav.is_absolute():
                r = {**r, "wav": str((path.parent / wav).resolve())}
            out[f"{tag}/{r['name']}"] = r
    if skipped:
        print(f"[info] 登録外の参照を {skipped} 件読み飛ばした")
    return out


def _decode_with_av(path: str) -> np.ndarray:
    """libsndfile が読めない形式を PyAV で読む。**m4a / aac / mp4 用。**

    libsndfile は wav / flac / ogg / mp3 は読めるが **MPEG-4 コンテナ (m4a) は読めない**。
    PyAV なら追加のプロセスを起こさずにデコードできる (ffmpeg を subprocess で
    呼ぶ経路は、PyInstaller で固めたときに ffmpeg の同梱が要る)。
    デコードと同時に 24kHz mono へ落とす。
    """
    import av

    with av.open(path) as cont:
        stream = next((s for s in cont.streams if s.type == "audio"), None)
        if stream is None:
            raise ValueError("音声ストリームが見つかりません")
        res = av.audio.resampler.AudioResampler(format="fltp", layout="mono", rate=fx.SR)
        chunks: list[np.ndarray] = []

        def take(frames) -> None:
            # PyAV のバージョンで単体 / リストが返るのでどちらも受ける
            for f in (frames if isinstance(frames, list) else [frames]):
                if f is not None:
                    chunks.append(np.asarray(f.to_ndarray()).reshape(-1))

        for frame in cont.decode(stream):
            take(res.resample(frame))
        take(res.resample(None))          # flush

    if not chunks:
        raise ValueError("音声を取り出せませんでした")
    return np.concatenate(chunks).astype(np.float32)


def read_mono24k(path: str) -> np.ndarray:
    """任意の音声を 24kHz mono に揃える。参照は 24kHz でないと弾かれるため。"""
    try:
        y, sr = sf.read(path, dtype="float32", always_2d=False)
    except Exception:
        # libsndfile が扱えない形式 (m4a など)。**ここで諦めない。**
        return _decode_with_av(path)
    if y.ndim > 1:
        y = y.mean(axis=1)
    if sr != fx.SR:
        import librosa

        y = librosa.resample(y.astype(np.float32), orig_sr=sr, target_sr=fx.SR)
    return y.astype(np.float32)


# ---- 参照音声の自動書き起こし ----------------------------------------------
# ICL は `ref_text` (参照音声の書き起こし) が必須で、空だと `ValueError` になる。
# 手で打たせると「音声を入れたのに生成できない」で止まってしまうので、
# アップロード時に Whisper で自動生成する。
#
# **厳密さは要らない。**モデルが `ref_text` に見ているのは「音声との一致」ではなく
# 「日本語として自然か」なので、多少の誤字脱字は実害が小さい。だから
# beam_size=1 の最小構成で回す。
WHISPER_MODEL = os.environ.get("QWEN_TTS_STUDIO_WHISPER_MODEL", "small")
WHISPER_SR = 16000        # Whisper の入力レート。24kHz のまま渡すと内部で落とされる
_ASR = None


def ref_window(y: np.ndarray) -> np.ndarray:
    """参照として**実際にモデルへ渡す区間**を切り出す (端の無音落とし + 上限)。

    `synthesize()` が参照に掛けるのと同じ処理。書き起こしはこれを通してから取る:
    - 1 時間の音声をそのまま Whisper に流せば**1 時間ぶん書き起こす**ことになる
    - モデルが聴くのは 20 秒の窓だけなので、全体を書き起こすと**音と文がずれる**
    """
    y = fx.trim_edges(y)
    if len(y) / fx.SR > MAX_REF_SEC:
        y = fx.pick_loud_window(y, fx.SR, MAX_REF_SEC)
    return y


def _asr():
    """Whisper を遅延ロードする。**GPU は使わない。**

    生成側 (1.7B fp16 で 3.4GB + 生成で 5.3GB) だけで VRAM 8GB を使い切るので、
    ここに 0.5GB を足すと生成の途中で落ちる。参照は長くても 20 秒なので
    CPU int8 で数秒しかかからず、GPU に載せる利点がない。

    初回だけモデルを取りに行く (small で約 500MB、HF のキャッシュに残る)。
    """
    global _ASR
    if _ASR is None:
        from faster_whisper import WhisperModel

        _ASR = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    return _ASR


def transcribe(y: np.ndarray, sr: int = fx.SR) -> str:
    """波形を書き起こす。**ファイルパスは受けない** — 渡すのは `ref_window()` で
    切り出した後の区間だけ、という約束を型で示すため。
    **VAD は使わない** (別途 onnxruntime が要るわりに 20 秒では効果が薄い)。
    """
    if sr != WHISPER_SR:
        import librosa

        y = librosa.resample(y.astype(np.float32), orig_sr=sr, target_sr=WHISPER_SR)
    segs, _ = _asr().transcribe(np.ascontiguousarray(y, dtype=np.float32),
                                language="ja", beam_size=1, vad_filter=False)
    return "".join(s.text for s in segs).strip()


def transcribe_file(path: str) -> str:
    """アップロードされたファイルを**必要な区間だけ**書き起こす。"""
    return transcribe(ref_window(read_mono24k(path)))


class Params:
    """UI の値をまとめて生成関数へ渡す入れ物。"""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def synthesize(p: Params) -> tuple[np.ndarray, list[str], list[tuple[str, str, np.ndarray]]]:
    """生成本体。**UI から切り離してある** (別スレッドで走らせるため)。

    3 つ目の戻り値は**デバッグ用の参照音声** `(表示名, ファイル接尾辞, 波形)`。
    崩壊するかどうかは**参照側をどれだけ壊せたか**で決まるので、実際に
    モデルへ渡した音を耳で確かめられないと切り分けができない
    (「崩壊しない」の原因が参照の加工不足なのか生成尺なのか判別できない)。
    """
    log: list[str] = []
    refs: list[tuple[str, str, np.ndarray]] = []
    t_all = time.perf_counter()
    log.append(f"{p.mode}: {p.effect}"
               + ("" if p.effect == "none"
                  else f" (区切り {p.chunk_sec:.2f}s / 開始 {p.ramp_start:.2f}"
                       f" / 全壊 {p.ramp_hard:.2f} / 質感 {p.warp:.2f})"))

    if p.ref_choice == UPLOAD:
        if not p.ref_file:
            raise ValueError("参照音声のファイルが選ばれていません。")
        y = read_mono24k(p.ref_file)
        # **手入力があればそちらが勝つ。**空欄なら読み込み時に取った自動書き起こし。
        rtext = (p.ref_text or "").strip()
        auto = (getattr(p, "auto_text", "") or "").strip()
        src = "手入力"
        if not rtext:
            rtext, src = auto, "自動 (Whisper)"
        if not rtext:
            raise ValueError("参照音声の書き起こしが取れませんでした。"
                             "「詳細な調整」の書き起こし欄に手で入力してください。")
        log.append(f"参照: ファイル {Path(p.ref_file).name}")
        log.append(f"書き起こし ({src}): {rtext}")
    else:
        r = _REFS.get(p.ref_choice)
        if r is None:
            raise ValueError(f"参照セットが見つかりません: {p.ref_choice}")
        y = read_mono24k(r["wav"])
        rtext = (p.ref_text or "").strip() or r["ref_text"]
        log.append(f"参照: {p.ref_choice}")

    if p.do_trim:
        before = len(y) / fx.SR
        y = fx.trim_edges(y)
        log.append(f"端の無音を除去: {before:.2f}s → {len(y)/fx.SR:.2f}s")

    # **長すぎる参照は切り詰める。**用意してあるセットは最長 15.9 秒で、
    # それ以上長くても崩壊の効きは変わらない一方、prefill と KV キャッシュが伸びて
    # VRAM 8GB では生成側が足りなくなる (生成だけで既に 5.3GB 使う)。
    # **先頭から切らない** — 前置きの無音を掴まないよう、いちばん喋っている区間を採る。
    if len(y) / fx.SR > MAX_REF_SEC:
        before = len(y) / fx.SR
        y = fx.pick_loud_window(y, fx.SR, MAX_REF_SEC)
        log.append(f"参照が長いので切り詰め: {before:.2f}s → {len(y)/fx.SR:.2f}s "
                   f"(上限 {MAX_REF_SEC:.0f}s・よく喋っている区間を採用)")

    log.append(f"参照長 (発話) {len(y)/fx.SR:.2f}s")
    # `synth()` が書き出すのと**同じ加工** (ピーク正規化) を掛けた音を残す。
    # ここを素の `y` にすると「聴いた音」と「渡した音」がずれる。
    refs.append(("整形後 (端の無音除去・切り詰め後)", "plain", fx.normalize_peak(y)))

    max_new = max(int(p.dur_sec * FRAMES_PER_SEC), 25)
    gen_kw = {"temperature": float(p.temperature),
              "subtalker_temperature": float(p.sub_temperature)}

    def synth(ref_wave: np.ndarray, label: str) -> np.ndarray:
        tmp = Path(os.environ.get("TEMP", ".")) / f"_qwen_ref_{label}.wav"
        sf.write(str(tmp), fx.normalize_peak(ref_wave), fx.SR, subtype="PCM_16")
        torch.manual_seed(int(p.seed))
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(int(p.seed))
        t = time.perf_counter()
        wavs, sr = _MODEL.generate_voice_clone(
            text=p.text, language=LANGUAGE, ref_audio=str(tmp), ref_text=rtext,
            max_new_tokens=max_new, **gen_kw,
        )
        # numpy に写した時点で GPU 側の参照を落とす。**2 回生成する経路があるので、
        # 1 回目の KV キャッシュを抱えたまま 2 回目に入ると 8GB では足りない。**
        w = np.asarray(wavs[0], dtype=np.float32).copy()
        del wavs
        capped = len(w) / sr >= max_new / FRAMES_PER_SEC * 0.95
        log.append(f"  {label}: {len(w)/sr:.2f}s / 生成 {time.perf_counter()-t:.1f}s"
                   + ("  ← 上限張り付き (崩壊)" if capped else ""))
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        return w

    if p.effect == "none":
        out = synth(y, "通常")
    else:
        broken, added = fx.build_broken_ref(
            y, p.effect, fx.SR, seed=int(p.seed), chunk_sec=p.chunk_sec,
            start=p.ramp_start, hard=p.ramp_hard, warp=p.warp)
        if added:
            log.append(f"参照が短いので崩壊部分を {added} ブロック継ぎ足し: "
                       f"{len(y)/fx.SR:.2f}s → {len(broken)/fx.SR:.2f}s "
                       f"(毎回オフセットを変えるのでリピートにはならない)")
        refs.append((f"崩壊加工後 ({p.effect})", "broken", fx.normalize_peak(broken)))
        if p.normal_head:
            # **頭は正常な推論、その後ろに崩壊を繋ぐ。** 生成は 2 回まわる。
            log.append(f"生成 2 回 (冒頭 {p.head_lo:.1f}〜{p.head_hi:.1f}s は正常):")
            normal = synth(y, "通常")
            collapsed = synth(broken, "崩壊")
            out, cut = fx.splice_normal_then_collapse(
                normal, collapsed, fx.SR, p.head_lo, p.head_hi, seed=int(p.seed))
            log.append(f"接合位置 {cut:.2f}s (区切りのいいところを自動選択)")
        else:
            out = synth(broken, "崩壊")

    log.append(f"出力 {len(out)/fx.SR:.2f}s / 全体 {time.perf_counter()-t_all:.1f}s")

    if torch.cuda.is_available():
        # PyTorch のキャッシュアロケータは解放したブロックをドライバに返さないので、
        # nvidia-smi の値は最高水位まで上がって下がらない。8GB のカードでは
        # 32 秒生成を繰り返すと上限に張り付いて OOM するため、**毎回返却する**。
        # allocated が回を追って増えるなら本物のリーク、reserved だけならキャッシュ。
        alloc = torch.cuda.memory_allocated() / 1024**3
        peak = torch.cuda.max_memory_allocated() / 1024**3
        torch.cuda.empty_cache()
        reserved = torch.cuda.memory_reserved() / 1024**3
        torch.cuda.reset_peak_memory_stats()
        log.append(f"VRAM 実確保 {alloc:.2f} / ピーク {peak:.2f} / 解放後の予約 {reserved:.2f} GiB")
    return out, log, refs


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------

CSS = """
:root { --sur:#12141a; --sur2:#1a1d26; --line:#262a36; --fg:#e7e9ee; --dim:#8b90a0; --acc:#7c5cff; --bad:#ff6b6b; }
body { background: radial-gradient(1200px 600px at 20% -10%, #1c2030 0%, #0d0f14 60%); }
.q-page, .nicegui-content { padding: 0 !important; }
.card { background: var(--sur); border:1px solid var(--line); border-radius:16px; }
.card-h { color: var(--dim); font-size:12px; letter-spacing:.14em; text-transform:uppercase; }
.brand { font-weight:700; letter-spacing:-.02em; }
.pill { border:1px solid var(--line); border-radius:999px; padding:2px 10px;
        font-size:11px; color:var(--dim); }

/* ヘッダ。**左右のスペーサを同幅にしてタイトルロゴの中央を機械的に保証する。**
   クレジットは右スペーサの中に置く — `justify-center` の行に並べると
   クレジットのぶんロゴが左へ押し出される。`min-width:0` + `overflow:hidden` が
   要で、これが無いとクレジットが幅を主張してスペーサの同幅が崩れる。 */
.hdr { display:flex; align-items:center; width:100%; }
.hdr > .sp { flex:1 1 0; min-width:0; overflow:hidden; }
.hdr .credit { display:flex; align-items:center; gap:6px;
               padding-left:14px; white-space:nowrap; }
.hdr .credit span, .hdr .credit img { flex:none; }   /* 縮ませない (潰れて豆粒になる) */
.hdr .credit span { font-size:10px; letter-spacing:.10em; color:var(--dim); }
.hdr .credit img { width:130px; display:block; }   /* タイトルロゴ 260px のちょうど半分 */
/* 狭い窓ではクレジットを消す。**残すと右スペーサに収まらず、のべつべロゴが
   右端で切れる** (タイトルの中央は保たれるが見栄えが悪い)。 */
@media (max-width:800px) { .hdr .credit { display:none; } }

/* 崩壊度つまみ。色は値に応じて `--easy-col` を差し替える (Quasar の color prop は
   名前付き色しか受け付けないため、CSS 変数で塗り替える)。 */
.easy .q-slider__track { background: var(--easy-col) !important; }
.easy .q-slider__thumb { color: var(--easy-col) !important; }
.easy .q-slider__marker-labels, .easy .q-slider__markers { color: var(--easy-col); }
.easy .q-slider__inner { background: rgba(255,255,255,.10); }
.easy-lab { font-size:22px; font-weight:800; letter-spacing:-.01em;
            transition: opacity .15s, transform .15s; }

/* キャラ選択タイル。**正方形**のグリッドに並べる。 */
.char-grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(112px, 1fr));
             gap:10px; width:100%; }
/* **選択中のタイルは枠の色だけでは分からない。**選んでいない側を沈めて、
   選んだ側だけを持ち上げる (縁取り + 発光 + 拡大 + チェック印 + 名前帯の着色)。
   差が「色相の違い」だけだと、9 枚並んだ中では埋もれる。 */
.char-tile { position:relative; aspect-ratio:1/1; border:2px solid var(--line);
             border-radius:14px; background:var(--sur2); cursor:pointer;
             overflow:hidden; display:flex; flex-direction:column;
             align-items:center; justify-content:flex-end;
             filter: grayscale(.4) brightness(.72); opacity:.9;
             transition: border-color .12s, transform .12s, filter .12s,
                         opacity .12s, box-shadow .12s; }
.char-tile:hover { transform: translateY(-2px); filter:none; opacity:1; }
.char-tile.sel { border-color: var(--acc); filter:none; opacity:1; z-index:1;
                 transform: scale(1.05);
                 box-shadow: 0 0 0 3px rgba(124,92,255,.45),
                             0 10px 26px rgba(124,92,255,.38); }
.char-tile.sel:hover { transform: scale(1.05) translateY(-2px); }
.char-tile.sel .nm { background: linear-gradient(transparent, var(--acc) 46%); }
.char-tile.sel::after { content:"✓"; position:absolute; top:6px; right:6px; z-index:2;
                        width:22px; height:22px; border-radius:999px;
                        background:var(--acc); color:#fff;
                        font-size:13px; font-weight:800; line-height:1;
                        display:flex; align-items:center; justify-content:center;
                        box-shadow:0 2px 6px rgba(0,0,0,.5); }
.char-tile img { position:absolute; inset:0; width:100%; height:100%;
                 object-fit:cover; object-position:center top; }
.char-tile .nm { position:relative; z-index:1; width:100%; text-align:center;
                 font-size:12px; font-weight:700; padding:4px 2px;
                 background:linear-gradient(transparent, rgba(0,0,0,.82) 42%); }
.char-tile .qm { position:absolute; inset:0; display:flex; align-items:center;
                 justify-content:center; font-size:44px; font-weight:800;
                 color:var(--dim); }
/* デバッグ用ドロワー。**既定は閉じ、開くまでは灰色の細い 1 行に見せる。** */
.dbg { border:1px solid var(--line); border-radius:12px; }
.dbg .q-item { min-height:30px; padding:0 10px; color:var(--dim); }
.dbg .q-item__label { font-size:11px; letter-spacing:.08em; }
.dbg .q-expansion-item__content { padding:2px 10px 10px; }

.picked { font-size:12px; color:var(--dim); letter-spacing:.08em; }
.picked b { color:var(--fg); font-size:15px; margin-left:6px; letter-spacing:0; }
"""


UPLOAD_CHAR = "__upload__"      # 「あなた専用キャラ」の擬似キャラ ID

# キャラ ID → フルネーム。**のべつべ本体の `piperCharacters.ts` / `PiperCharacters.kt` と
# 同じ表記に揃えること** (chibi 画像も同じ素材を同梱している)。
# **`mossan_hoshi` だけ例外**: のべつべのキャストではなく作者本人の声
# (`refs/mossan_hoshi/`)。ライセンス条件が他と違う (TERMS.md 1-4、CC0 ではない)。
CHARACTER_NAMES: dict[str, str] = {
    "noa": "文月 乃亜",
    "tsukasa": "御影 司",
    "ritsu": "柊 律",
    "reika": "御影 怜香",
    "suzu": "御影 すず",
    "priya": "プリヤ・シャルマ",
    "yume": "沢渡 ゆめ",
    "kasumi": "久遠 霞",
    "mossan_hoshi": "mossan_hoshi",
}

# 参照 refs.json のフォルダ名 → キャラ ID。名前が一致しないものだけ書く。
REF_GROUP_TO_CHAR: dict[str, str] = {
    "refs_sets_v2": "noa",      # 手選び 6 セットは noa のもの
    "refs_sets": "noa",
}


def char_of_group(group: str) -> str:
    """参照フォルダ名からキャラ ID を引く。"""
    return REF_GROUP_TO_CHAR.get(group, group)


def char_of_ref(name: str) -> str:
    """参照名 (`<グループ>/<音声>`) からキャラ ID を引く。"""
    return char_of_group(name.split("/")[0])


def voice_of_ref(name: str) -> str:
    """参照名から音声名 (キャラ内での区別) を取り出す。"""
    return name.split("/", 1)[1] if "/" in name else name


# 崩壊度つまみの色。**段が上がるほど危険色**にする (通常 / 崩壊前夜 / 完全崩壊)。
EASY_COLORS = ("#3ddc97", "#ffc400", "#ff1717")
assert len(EASY_COLORS) == len(fx.EASY_STOPS), "段と色の数を揃えること"


def easy_color(t: float) -> str:
    """つまみ位置を色に写す。**段の間は線形補間する**ので色も連続して変わる。"""
    t = min(max(float(t), fx.EASY_MIN), fx.EASY_MAX)
    stops = [(p, c) for (p, _n, _x), c in zip(fx.EASY_STOPS, EASY_COLORS)]
    lo, hi = stops[0], stops[-1]
    for a, b in zip(stops, stops[1:]):
        if t <= b[0]:
            lo, hi = a, b
            break
    span = hi[0] - lo[0]
    r = (t - lo[0]) / span if span > 0 else 0.0
    a_rgb, b_rgb = (tuple(int(c[i:i + 2], 16) for i in (1, 3, 5)) for c in (lo[1], hi[1]))
    return "#%02x%02x%02x" % tuple(
        round(x + (y - x) * r) for x, y in zip(a_rgb, b_rgb))


def build_ui() -> None:
    from nicegui import ui

    ui.add_head_html(f"<style>{CSS}</style>")
    # **ダークテーマ固定。**`CSS` の配色 (`--sur` / body のグラデーション) が
    # 暗い前提で書かれているので、ライトにすると Quasar の部品だけが白くなり
    # 地の色と噛み合わない。切替の口 (`--light`) は用意しない。
    ui.dark_mode(True)

    def ref_label(name: str) -> str:
        """音声リストに出す表示名。**長さを併記する** — 崩壊は参照が長いほど確実に効く
        (実測: 発話 11.5 秒で 3/3、3.8 秒で 2/3、5.0 秒で 1/3)。"""
        r = _REFS.get(name)
        v = voice_of_ref(name)
        return v if r is None else f"{v}  ·  {r.get('duration_sec', 0):.1f}s"

    # **1 カラム。**崩壊度つまみを主役にするため、横に並べて視線を割らない。
    with ui.column().classes("w-full max-w-[760px] mx-auto px-6 py-7 gap-5"):
        # ---- ヘッダ (タイトルロゴを中央に、その右へ小さくクレジット) ----
        # ロゴがあれば画像、無ければ文字に落とす (同梱漏れでも動く)
        if (app_dir() / "assets" / "logo.png").is_file():
            credit = ""
            if (app_dir() / "assets" / NOVTUBE_LOGO).is_file():
                credit = ('<div class="credit"><span>presented by</span>'
                          f'<img src="/{NOVTUBE_LOGO}" alt="のべつべ！"></div>')
            # **`w-full` が要る。**NiceGUI の要素は幅を内容に合わせて縮むので、
            # 付けないと `.hdr` の 100% が「中身の幅」に解決され、
            # 左右のスペーサが痩せてロゴが左に寄る。
            ui.html('<div class="hdr">'
                    '<div class="sp"></div>'
                    '<img src="/logo.png" alt="" '
                    'style="width:260px; max-width:56vw; display:block">'
                    f'<div class="sp">{credit}</div>'
                    '</div>').classes("w-full")
        else:
            with ui.row().classes("w-full justify-center"):
                ui.label(APP_TITLE).classes("brand text-2xl")

        # キャラごとに参照をまとめる。**キャラ順は CHARACTER_NAMES の並び**に従う。
        # 登録外は `load_refs` が読み込み時点で落としているのでここには来ない
        # (**後ろへ回して出す、はしない**。タイルに出た時点で「出した」ことになる)。
        # **プリセットがキャラ属性に従うので、テキスト欄より先に決めておく。**
        by_char: dict[str, list[str]] = {}
        for n in sorted(_REFS):
            by_char.setdefault(char_of_ref(n), []).append(n)
        order = [c for c in CHARACTER_NAMES if c in by_char]

        state = {"upload_path": None, "auto_text": "", "asr_error": "",
                 "asr_busy": False, "blocked_msg": False,
                 "char": order[0] if order else UPLOAD_CHAR}

        # ---- 何を喋らせる ----
        with ui.card().classes("card w-full p-5 gap-3"):
            ui.label("何を喋らせる").classes("card-h")
            _init = presets_for(state["char"])
            _init_name = next(iter(_init))
            preset = ui.select(list(_init), value=_init_name) \
                .props("outlined dense options-dense").classes("w-full")
            text = ui.textarea(value=_init[_init_name]) \
                .props("outlined autogrow input-style='min-height:110px'") \
                .classes("w-full")

            # `applied` = 最後に**こちらが**流し込んだ本文。現在値との差が
            # 「ユーザーが手で書き換えたか」の判定になる (フラグとイベント順に
            # 依存しないので、set_value の再発火で誤判定しない)。
            # `silent` = プログラムから選択を変えている間だけ立てる。立てないと
            # 確認ダイアログが**ユーザーが操作していないのに**開く。
            tstate = {"applied": _init[_init_name], "name": _init_name,
                      "group": preset_group(state["char"]), "silent": False}

            def user_edited() -> bool:
                return (text.value or "").strip() != (tstate["applied"] or "").strip()

            def apply_preset(name: str) -> None:
                body = presets_for(state["char"]).get(name, "")
                tstate["name"] = name
                tstate["applied"] = body
                text.set_value(body)

            def set_preset_silently(name: str) -> None:
                tstate["silent"] = True
                preset.set_value(name)
                tstate["silent"] = False

            def on_preset_change(e) -> None:
                name = e.value
                if tstate["silent"] or name == tstate["name"] or name is None:
                    return
                if not user_edited():
                    apply_preset(name)
                    return
                # **手で書いた本文を黙って消さない。**選び直しはユーザーの操作なので
                # 確認してから上書きする (キャラ切替による差し替えとは扱いが違う)。
                def keep() -> None:
                    dlg.close()
                    set_preset_silently(tstate["name"])      # 選択を元へ戻す

                def overwrite() -> None:
                    dlg.close()
                    apply_preset(name)

                with ui.dialog() as dlg, ui.card().classes("card p-5 gap-3"):
                    ui.label("編集した内容が消えますがよろしいですか？") \
                        .classes("text-base font-bold")
                    ui.label("本文を書き換えています。プリセットを選び直すと上書きされます。") \
                        .classes("text-xs").style("color:var(--dim)")
                    with ui.row().classes("w-full justify-end gap-2"):
                        ui.button("やめる", on_click=keep).props("flat")
                        ui.button("上書きする", on_click=overwrite).props("unelevated")
                dlg.open()

            def sync_presets() -> None:
                """キャラが変わったらプリセットの顔ぶれを差し替える。

                **本文は上書きしない** — 手で書いた内容を消さないため。
                未編集のときだけ新しい属性の言い回しへ追随させる。
                """
                group = preset_group(state["char"])
                if group == tstate["group"]:
                    return
                edited = user_edited()                       # 差し替える前に見る
                tstate["group"] = group
                names = list(presets_for(state["char"]))
                name = tstate["name"] if tstate["name"] in names else names[0]
                tstate["silent"] = True
                preset.set_options(names, value=name)
                tstate["silent"] = False
                tstate["name"] = name
                if not edited:
                    apply_preset(name)

            preset.on_value_change(on_preset_change)
            ui.label("長い文ほど崩壊が深くなる。短文は崩れきる前に終わる") \
                .classes("text-xs").style("color:var(--dim)")

        # ---- 誰の声で (キャラのタイル → そのキャラの音声を選ぶ 2 段構え) ----
        with ui.card().classes("card w-full p-5 gap-3"):
            ui.label("誰の声で").classes("card-h")

            tiles: dict[str, object] = {}
            tile_imgs: dict[str, object] = {}     # 崩壊版へ差し替えるための img 要素
            grid = ui.element("div").classes("char-grid")

            voice_box = ui.column().classes("w-full gap-2")

            def render_voices() -> None:
                """選択中のキャラの音声リスト (専用キャラならファイル選択) を出す。"""
                voice_box.clear()
                with voice_box:
                    # 誰を選んでいるかを**文字でも出す**。タイルの見た目だけだと
                    # 「どれが選択中か」を絵から読み取らせることになる。
                    who = ("あなた専用キャラ" if state["char"] == UPLOAD_CHAR
                           else CHARACTER_NAMES.get(state["char"], state["char"]))
                    ui.html(f'<div class="picked">選択中 <b>{who}</b></div>')
                    if state["char"] == UPLOAD_CHAR:
                        # **拡張子を明示する。**Windows は m4a を audio/x-m4a や
                        # 空の MIME で寄越すことがあり、`audio/*` だけだと弾かれる。
                        up = ui.upload(auto_upload=True, max_files=1) \
                            .props("accept='audio/*,.m4a,.mp4,.aac,.wav,.mp3,.flac,.ogg' "
                                   "flat bordered").classes("w-full")
                        up.on_upload(on_upload)
                        ui.label("wav / mp3 / m4a / flac / ogg など。24kHz mono に自動変換します。"
                                 "書き起こしは自動で作ります") \
                            .classes("text-xs").style("color:var(--dim)")
                        if state["upload_path"]:
                            ui.label(f"読み込み済み: {Path(state['upload_path']).name}") \
                                .classes("text-xs").style("color:var(--acc)")
                        # **取れた書き起こしを見せる。**モデルへ何を渡しているかが
                        # 見えないと、崩れ方がおかしいときに切り分けができない。
                        if state.get("auto_text"):
                            ui.label(f"書き起こし: {state['auto_text']}") \
                                .classes("text-xs").style("color:var(--dim)")
                        elif state.get("asr_error"):
                            ui.label("書き起こしに失敗しました。"
                                     "「詳細な調整」の書き起こし欄に手で入力してください") \
                                .classes("text-xs").style("color:var(--bad)")
                        return
                    names = by_char.get(state["char"], [])
                    state["ref"] = names[0] if names else None
                    ui.radio({n: ref_label(n) for n in names},
                             value=state.get("ref")) \
                        .props("dense").classes("w-full") \
                        .on_value_change(lambda e: (state.__setitem__("ref", e.value),
                                                    mark_dirty()))

            def select_char(cid: str) -> None:
                state["char"] = cid
                for k, t in tiles.items():
                    t.classes(remove="sel") if k != cid else t.classes(add="sel")
                render_voices()
                sync_presets()          # 属性に合わせてプリセットを差し替える
                mark_dirty()

            def add_tile(cid: str, label: str, img: str | None) -> None:
                with grid:
                    t = ui.element("div").classes("char-tile")
                    with t:
                        if img:
                            # **差し替えるので要素を持っておく** (崩壊度つまみ連動)。
                            tile_imgs[cid] = ui.html(f'<img src="{img}" alt="">')
                        else:
                            ui.html('<div class="qm">？</div>')
                        ui.html(f'<div class="nm">{label}</div>')
                    t.on("click", lambda _=None, c=cid: select_char(c))
                    tiles[cid] = t

            for cid in order:
                add_tile(cid, CHARACTER_NAMES.get(cid, cid),
                         f"/chibi/{cid}.webp?v={ASSET_VER}")
            add_tile(UPLOAD_CHAR, "あなた専用キャラ", None)

            def paint_tiles(want: str) -> None:
                """つまみの位置に応じてタイルの絵を差し替える (素 / 崩壊 / なれの果て)。

                **その段の絵を持っていないキャラは 1 段手前へ落とす** (欠けたタイルを
                出さない)。「あなた専用キャラ」は ？ のままで差し替えの対象外。
                """
                if want == state.get("tile_state"):
                    return                      # 連続したつまみ操作で毎回描き直さない
                state["tile_state"] = want
                for cid, el in tile_imgs.items():
                    have = want
                    if have == fx.TILE_GORE and cid not in GORE_TILE_CHARS:
                        have = fx.TILE_COLLAPSE
                    if have == fx.TILE_COLLAPSE and cid not in COLLAPSE_TILE_CHARS:
                        have = fx.TILE_NORMAL
                    el.set_content(
                        f'<img src="/{have}/{cid}.webp?v={ASSET_VER}" alt="">')

            async def on_upload(e) -> None:
                # **NiceGUI 3.x で引数の形が変わった。**2.x の `e.name` /
                # `e.content.read()` は無く、`e.file.name` / `await e.file.read()`。
                # 旧 API のままだと例外はサーバ側のログにしか出ず、画面は
                # 「アップロード 100%」のまま。**生成して初めて「参照音声のファイルが
                # 選ばれていません」で落ちる**ので、原因が見えない壊れ方をする。
                d = Path(os.environ.get("TEMP", ".")) / "qwen_studio_upload"
                d.mkdir(parents=True, exist_ok=True)
                dst = d / e.file.name
                dst.write_bytes(await e.file.read())
                state["upload_path"] = str(dst)
                state["auto_text"] = ""
                state["asr_error"] = ""
                # **書き起こしが終わるまで生成させない** (理由は `gen_blocked`)。
                state["asr_busy"] = True
                ui.notify(f"読み込みました: {e.file.name}", type="positive")
                render_voices()
                mark_dirty()

                # **書き起こしはここで取る。**生成ボタンを押してからだと、
                # 数秒の無言の待ちが生成時間に混ざって原因が見えなくなる。
                # 別スレッドへ逃がすのは、CPU 推論の間 UI を止めないため。
                note = ui.notification("書き起こし中…", spinner=True, timeout=None)
                try:
                    state["auto_text"] = await asyncio.to_thread(transcribe_file, str(dst))
                except Exception as ex:     # noqa: BLE001  未導入 / 取得失敗は手入力に落とす
                    state["asr_error"] = str(ex)
                finally:
                    # **失敗しても必ず下ろす。**下りないと手入力に切り替えても
                    # 永久に押せないままになる。
                    state["asr_busy"] = False
                    note.dismiss()
                render_voices()
                mark_dirty()

            if order:
                tiles[order[0]].classes(add="sel")
            render_voices()

        # ---- チェックボックス (詳細に入れず表に出す) ----
        # **バーの上に説明文を置かない。**つまみを主役にするため文字を足さない。
        with ui.card().classes("card w-full p-4"):
            with ui.row().classes("w-full items-center gap-8"):
                normal_head = ui.checkbox("冒頭は正常な推論を使う", value=True) \
                    .tooltip("崩壊が早すぎるのを防ぐ。生成が 2 回走る")
                advanced = ui.checkbox("詳細設定を使う", value=False) \
                    .tooltip("崩壊度つまみを無効にし、下の詳細な調整で決める")
                ui.space()
                # **崩壊は確率的なので、シードは詳細設定に埋めない。**同じ設定のまま
                # 引き直す / 当たりを再現するための、いちばんよく触るつまみになる。
                # **3 つを 1 つの行にまとめて `flex-nowrap` にする** — 外側の行へ直に
                # 並べると、幅が足りないときにボタンだけ次の行へ落ちる。
                with ui.row().classes("items-center gap-2 flex-nowrap"):
                    ui.label("乱数シード").classes("text-sm").style("color:var(--dim)")
                    # 振り直しボタンは**ラベルと値の間**に置く。値の外側に離すと
                    # 何のボタンか分からなくなる。`set_value` は `on_value_change` を
                    # 通るので、生成ボタンのロックも自動で外れる。
                    reroll = ui.button(icon="refresh").props("flat dense round") \
                        .tooltip("シードを振り直す")
                    # **ページを開くたびに振り直す。**固定値だと、開き直しただけでは
                    # 同じ当たり外れを引き続けることになる (`@ui.page` なので
                    # `build_ui()` はクライアントごとに走り、ここで新しい値になる)。
                    seed = ui.number(value=random.randint(1, 999_999), format="%d") \
                        .props("outlined dense").classes("w-28") \
                        .tooltip("同じ値なら同じ結果になる。崩壊は当たり外れがあるので振り直す")
                    reroll.on_click(lambda: seed.set_value(random.randint(1, 999_999)))

        # ---- 詳細な調整 ----
        # **「詳細設定を使う」を入れたときだけ、チェックの直下に開いた状態で出す。**
        # 画面末尾に置くと、チェックしてから探しに行くことになる。
        adv_card = ui.card().classes("card w-full p-5 gap-2")
        adv_card.bind_visibility_from(advanced, "value")
        with adv_card:
            with ui.expansion("詳細な調整", value=True).classes("w-full").props("dense"):
                ui.label("参照音声").classes("card-h mt-2")
                ref_text = ui.input(
                    placeholder="参照音声の書き起こし (空欄なら自動。書けばそちらが優先)") \
                    .props("outlined dense").classes("w-full")
                do_trim = ui.checkbox("端の無音を落とす", value=True)
                ui.label("落とさないと出力の間が 0.4〜0.9 秒に伸びる") \
                    .classes("text-xs -mt-2").style("color:var(--dim)")

                ui.label("生成").classes("card-h mt-3")
                ui.label("生成尺の上限 (秒)").classes("text-xs").style("color:var(--dim)")
                # 崩壊時はこの上限まで喋り続けるので、既定を長くすると毎回そこまで待たされる。
                dur = ui.slider(min=3, max=60, step=1, value=20).props("label-always")
                ui.label("冒頭を正常な推論にする範囲 (秒)").classes("text-xs") \
                    .style("color:var(--dim)")
                # **既定は 2〜5 秒。**狭いと谷が 1 つも無い範囲を引くことがあり、
                # 語の途中で切れて接合が目立つ。広く取って、その中から
                # いちばん長い無音 (息継ぎ・句点) を選ばせる (`fx.find_break_point`)。
                head = ui.range(min=0.5, max=12, step=0.1,
                                value={"min": HEAD_DEFAULT_LO, "max": HEAD_DEFAULT_HI}) \
                    .props("label-always")

                ui.label("崩壊 (崩壊度つまみの代わりに直接指定する)").classes("card-h mt-3")
                ui.label("**「詳細設定を使う」を入れたときだけ有効。**"
                         "強く壊すほどノイズ化して音割れに聞こえる") \
                    .classes("text-xs").style("color:var(--dim)")
                effect = ui.select(fx.REF_EFFECTS, value="ramp_reverse") \
                    .props("outlined dense options-dense").classes("w-full")
                ui.label("区切りの長さ (秒) — 細かくすると崩壊しなくなる") \
                    .classes("text-xs mt-2").style("color:var(--dim)")
                chunk_sec = ui.slider(min=0.05, max=1.0, step=0.05, value=0.50) \
                    .props("label-always")
                ui.label("壊し始める位置 (割合)").classes("text-xs").style("color:var(--dim)")
                ramp_start = ui.slider(min=0.0, max=1.0, step=0.05, value=0.50) \
                    .props("label-always")
                ui.label("全部壊す位置 (割合)").classes("text-xs").style("color:var(--dim)")
                ramp_hard = ui.slider(min=0.1, max=1.0, step=0.05, value=0.9) \
                    .props("label-always")
                ui.label("質感を壊す量 — 逆再生だけでは元の声色が残る") \
                    .classes("text-xs").style("color:var(--dim)")
                warp = ui.slider(min=0.0, max=1.0, step=0.05, value=0.0) \
                    .props("label-always") \
                    .tooltip("後半ほど速度を落とし、ピッチとフォルマントをずらす。"
                             "上げるほど「誰の声か」が消える")

                for _w in (effect, chunk_sec, ramp_start, ramp_hard, warp):
                    _w.bind_enabled_from(advanced, "value")

                ui.label("サンプリング (崩壊にはほぼ効かない)").classes("card-h mt-3")
                ui.label("temperature").classes("text-xs").style("color:var(--dim)")
                temperature = ui.slider(min=0.1, max=3.0, step=0.05, value=0.9) \
                    .props("label-always")
                ui.label("subtalker_temperature").classes("text-xs").style("color:var(--dim)")
                sub_temperature = ui.slider(min=0.1, max=3.0, step=0.05, value=0.9) \
                    .props("label-always")

                # 乱数シードは**詳細設定ではなく表**に出してある (チェックボックスの行)。


        # ================= 崩壊度 (**この画面の主役。生成ボタンの直上**) =================
        # バーの上には何も置かない。段名 3 つだけで状態が読めるようにする。
        with ui.card().classes("card w-full p-7 gap-3"):
            # 既定は **通常** (崩壊なし)。壊すのは明示的に選んだときだけにする。
            easy = ui.slider(min=fx.EASY_MIN, max=fx.EASY_MAX, step=0.01,
                             value=fx.EASY_MIN) \
                .props("markers=1 track-size=14px thumb-size=34px") \
                .classes("w-full easy")
            labs = []
            with ui.row().classes("w-full justify-between"):
                for _pos, _name, _ in fx.EASY_STOPS:
                    labs.append(ui.label(_name).classes("easy-lab"))

            def paint(t: float) -> None:
                """つまみの色と段名の強調を、いまの値に合わせる。"""
                easy.style(f"--easy-col:{easy_color(t)}")
                for (pos, _n, _p), lab in zip(fx.EASY_STOPS, labs):
                    near = abs(t - pos) < 0.5
                    lab.style(f"color:{easy_color(pos)};"
                              f"opacity:{1.0 if near else 0.45};"
                              f"transform:scale({1.08 if near else 1.0})")

            paint(fx.EASY_MIN)
            easy.on_value_change(lambda e: (paint(e.value), mark_dirty()))
            # かんたん設定と詳細設定は排他。**両方が生きていると
            # どちらの値で生成されたのか分からなくなる。**
            easy.bind_enabled_from(advanced, "value", backward=lambda v: not v)

        # ---- 生成 ----
        with ui.row().classes("w-full items-center justify-center gap-3"):
            go = ui.button("生成する", icon="play_arrow") \
                .props("unelevated no-caps size=lg").classes("px-8") \
                .style("background:var(--acc)")
            spinner = ui.spinner(size="sm")
            spinner.set_visibility(False)
            status = ui.label("").classes("text-sm").style("color:var(--dim)")

        def gen_blocked() -> str:
            """生成できない理由を返す (押せるなら空文字)。

            **専用キャラは書き起こしが揃うまで押させない。**ICL は `ref_text` が必須で、
            書き起こしは読み込み後に CPU 推論で数秒かかる。その間押せてしまうと
            「参照はあるのにエラーで落ちる」か「空の書き起こしのまま生成される」の
            どちらかになり、どちらも原因が見えない壊れ方をする。
            プリセットのキャラは refs.json に書き起こしが同梱されているので対象外。
            """
            if state["char"] != UPLOAD_CHAR:
                return ""
            if not state["upload_path"]:
                return "音声ファイルを読み込んでください"
            if state.get("asr_busy"):
                return "書き起こし中です。終わるまでお待ちください"
            if not (state.get("auto_text") or (ref_text.value or "").strip()):
                return "書き起こしがありません。「詳細な調整」の書き起こし欄に入れてください"
            return ""

        def sync_tiles() -> None:
            """いまの設定 (かんたん / 詳細のどちらでも) をタイルの絵に反映する。

            判定軸は `start` に一本化してある — かんたん設定も詳細設定も最終的に
            同じ値へ落ちるので、**モードごとに別のしきい値を持たない**。
            """
            if advanced.value:
                eff, start = effect.value, ramp_start.value
            else:
                eff, _c, start, _h, _w = fx.easy_params(easy.value)
            paint_tiles(fx.tile_state(eff, start))

        # 生成が終わったらボタンを止める。**設定を一切変えずに押し直すのは誤操作**
        # (同じ結果を作り直すのに数分と VRAM を使う)。何か触れば復帰する。
        def mark_dirty() -> None:
            sync_tiles()
            why = gen_blocked()
            if why:
                go.disable()
                go.tooltip(why)
            else:
                go.enable()
                go.props(remove="disable")
                go.tooltip("")
            # **理由を文字でも出す。**押せないボタンだけ置くと故障に見える。
            # 消すのは直前がブロック表示だったときだけ (生成結果の表示を潰さない)。
            if why or state.get("blocked_msg"):
                status.text = why
            state["blocked_msg"] = bool(why)

        def mark_done() -> None:
            go.disable()
            go.tooltip("設定を変えると再び押せます (同じ設定での作り直しを防いでいます)")

        # 初期状態を反映する。**参照が 1 つも無いと起動直後から専用キャラが選ばれる**ので、
        # ここで評価しないとファイル未読込のまま押せてしまう。
        mark_dirty()

        with ui.card().classes("card w-full p-5 gap-3"):
            ui.label("結果").classes("card-h")
            # 崩壊させるとボコーダが ±1.0 でクランプして音が割れることがある
            # (正常部は割れない)。**再生の直前に出す** — 音量を上げたまま鳴らすと痛い。
            ui.label("⚠️ 音量注意 (音割れ発生することがあります)") \
                .classes("text-sm font-bold").style("color:#ffc400")
            player = ui.audio("").classes("w-full")
            player.set_visibility(False)
            # デバッグ用。**モデルへ実際に渡した参照音声**をここへ並べる
            # (崩壊の効きは参照の壊れ具合で決まるので、耳で確かめられないと
            # 「崩壊しない」の原因を参照側か生成尺かに切り分けられない)。
            ref_box = ui.column().classes("w-full gap-1")
            log_box = ui.log(max_lines=40).classes("w-full h-40 text-xs") \
                .style("background:var(--sur2); border-radius:12px")

        async def run_generate() -> None:
            from nicegui import run

            if advanced.value:
                eff, chunk, r_start, r_hard, wrp = (
                    effect.value, chunk_sec.value, ramp_start.value,
                    ramp_hard.value, warp.value)
                mode = "詳細設定"
            else:
                eff, chunk, r_start, r_hard, wrp = fx.easy_params(easy.value)
                mode = f"かんたん設定 [{fx.easy_label(easy.value)}]"

            ref_choice = (UPLOAD if state["char"] == UPLOAD_CHAR
                          else state.get("ref"))
            if not ref_choice:
                ui.notify("参照音声が選ばれていません", type="warning")
                return

            p = Params(
                text=text.value, ref_choice=ref_choice,
                ref_file=state["upload_path"], ref_text=ref_text.value,
                auto_text=state.get("auto_text", ""),
                do_trim=do_trim.value, effect=eff, mode=mode,
                chunk_sec=chunk, ramp_start=r_start, warp=wrp,
                ramp_hard=r_hard, normal_head=normal_head.value,
                head_lo=head.value["min"], head_hi=head.value["max"],
                dur_sec=dur.value, seed=int(seed.value or 0),
                temperature=temperature.value, sub_temperature=sub_temperature.value,
            )
            if not (p.text or "").strip():
                ui.notify("テキストが空です", type="warning")
                return

            go.disable()
            spinner.set_visibility(True)
            n = 2 if (p.effect != "none" and p.normal_head) else 1
            # 待ち時間を決めるのは尺そのものではない。**初回はウォームアップで
            # RTF が 22 まで落ち、崩壊は推論を 2 回まわす** — そこを伝える。
            status.text = f"生成中… (推論 {n} 回。初回生成時や崩壊生成時は数分かかります)"
            log_box.clear()
            try:
                # **UI スレッドを塞がない。** 生成は数分かかる
                out, lines, refs = await run.io_bound(synthesize, p)
                stamp = time.strftime("%Y%m%d_%H%M%S")
                path = _OUT_DIR / f"out_{stamp}.wav"
                sf.write(str(path), out, fx.SR)
                for line in lines:
                    log_box.push(line)
                log_box.push(f"保存: {path}")
                ref_box.clear()
                with ref_box:
                    # **既定は閉じておく。**普段は要らない情報なので、畳んだ細い行に
                    # しておき、切り分けたいときだけ開く。
                    with ui.expansion("デバッグ · モデルに渡した参照音声") \
                            .classes("w-full dbg").props("dense"):
                        for title, suffix, wave in refs:
                            rp = _OUT_DIR / f"out_{stamp}_ref_{suffix}.wav"
                            sf.write(str(rp), wave, fx.SR)
                            ui.label(f"{title} · {len(wave)/fx.SR:.2f}s") \
                                .classes("text-xs").style("color:var(--dim)")
                            ui.audio(f"/out/{rp.name}?t={stamp}").classes("w-full")
                player.set_source(f"/out/{path.name}?t={stamp}")
                player.set_visibility(True)
                status.text = f"完了 · {len(out)/fx.SR:.1f} 秒"
                spinner.set_visibility(False)
                mark_done()      # **成功時だけ止める。**失敗は押し直せないと困る
                return
            except Exception as e:  # noqa: BLE001  UI へそのまま出す
                log_box.push(f"[エラー] {e}")
                status.text = "失敗しました"
                ui.notify(str(e), type="negative")
            spinner.set_visibility(False)
            go.enable()

        # 設定を触ったら再び押せるようにする。**入力の取りこぼしがあると
        # 「変えたのに押せない」になる**ので、値を持つ部品は全部つなぐ。
        for _w in (text, preset, normal_head, advanced, ref_text, do_trim, effect,
                   dur, head, chunk_sec, ramp_start, ramp_hard, warp,
                   temperature, sub_temperature, seed):
            _w.on_value_change(lambda _e: mark_dirty())

        go.on_click(run_generate)


# --------------------------------------------------------------------------
# 多重起動ガード
# --------------------------------------------------------------------------
# モデルは 3.4GB を VRAM に載せる。二重に起動すると 8GB のカードでは即座に
# 上限へ張り付き、**両方とも応答しなくなる** (実際に 4 重起動で詰まらせた)。

# 排他は **OS が保持するファイルロック**で取る。自前の PID ファイル判定は使わない。
#
#   - OS ロックは「取れた / 取れない」の二択で**曖昧な状態が無い**。
#     プロセスが kill されても OS が自動で解放するので、死んだ記録が residue として
#     残って**二度と起動できなくなる詰み方をしない**。
#   - 以前ここをコマンドライン文字列の照合で書いて事故を起こした。相対パス起動
#     (`python app.py`) では絶対パスと一致せず、**ガードがあるのに素通りする**。
#     照合で「分からない」を「問題なし」に倒すと、保護されているつもりで無防備になる。
#
# 止めるのは**別インスタンスの存在が確定したときだけ**。ロック機構そのものが
# 使えない環境では**警告を出したうえで起動する** (起動できない人を作らない)。

_LOCK_PATH = Path(tempfile.gettempdir()) / "qwen_tts_studio.lock"
_INFO_PATH = _LOCK_PATH.with_suffix(".info")
_LOCK_FH = None            # プロセス生存中ずっと握る。GC で閉じさせないため保持する


def _try_lock(fh) -> bool:
    """非ブロッキングで排他ロックを取る。取れたら True、他が握っていたら False。"""
    try:
        import msvcrt
    except ImportError:
        import fcntl
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False
    try:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        return True
    except OSError:
        return False


def _record_self() -> None:
    """自分の PID と生成時刻を残す。**次のインスタンスが同一性を確認するため。**"""
    try:
        import psutil

        me = psutil.Process(os.getpid())
        _INFO_PATH.write_text(f"{me.pid}\n{me.create_time()}\n", "utf-8")
    except Exception:
        pass                                   # 診断用なので失敗しても起動は続ける


def _terminate_recorded() -> bool:
    """記録された前インスタンスを終了させる。終了できたら True。

    **PID だけでは信用しない。**記録した生成時刻と一致することを確認してから落とす
    (PID が再利用されていた場合に**無関係なプロセスを巻き添えにしない**ため)。
    """
    try:
        import psutil

        pid_s, ctime_s = _INFO_PATH.read_text("utf-8").splitlines()[:2]
        pid, ctime = int(pid_s), float(ctime_s)
        if pid == os.getpid():
            return False
        p = psutil.Process(pid)
        if abs(p.create_time() - ctime) > 1.0:
            print(f"  PID {pid} は記録と生成時刻が違うため触りません", flush=True)
            return False
    except Exception as e:
        print(f"  前インスタンスを特定できません ({e})", flush=True)
        return False

    print(f"  PID {pid} を終了します...", flush=True)
    try:
        p.terminate()
        p.wait(timeout=15)
    except Exception:
        try:
            p.kill()
            p.wait(timeout=10)
        except Exception as e:
            print(f"  終了できません: {e}", flush=True)
            return False
    return True


def _acquire_single_instance() -> None:
    """このプロセスだけが動いていることを保証する。"""
    global _LOCK_FH
    try:
        _LOCK_FH = open(_LOCK_PATH, "a+")
    except OSError as e:
        # ロックを作れない環境で起動できなくなるのは行き過ぎ。**黙らずに続ける。**
        print(f"[警告] 多重起動ガードを使えません ({e})。保護なしで起動します。"
              " 二重起動すると VRAM を使い切って両方とも応答しなくなります。", flush=True)
        return

    if _try_lock(_LOCK_FH):
        _record_self()
        return

    # ここに来た = **別のインスタンスが動いていることを OS が保証している。**
    print("別のインスタンスが起動しています。", flush=True)
    if _terminate_recorded():
        for _ in range(40):                    # 解放は OS 任せなので少し待つ
            if _try_lock(_LOCK_FH):
                _record_self()
                print("  引き継ぎました", flush=True)
                return
            time.sleep(0.5)

    raise SystemExit(
        "別のインスタンスが動いていて終了させられませんでした。\n"
        "  モデルは 3.4GB を VRAM に載せるため、二重起動すると\n"
        "  8GB のカードでは両方とも応答しなくなります。\n"
        "  そのインスタンスを手動で終了させてから起動し直してください。")


def main() -> None:
    global _MODEL, _REFS, _OUT_DIR

    # 同一プロセス内で二度目の `main()` に入らせない。モジュールが再読み込みされると
    # モジュール変数はリセットされるため、**プロセスに紐づく環境変数**で見る。
    # 二度走るとモデル (3.4GB) を再度 VRAM に載せてイベントループを塞ぐ。
    if os.environ.get("_QWEN_TTS_STUDIO_MAIN") == str(os.getpid()):
        return
    os.environ["_QWEN_TTS_STUDIO_MAIN"] = str(os.getpid())

    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=None,
                    help="**ローカル snapshot のパス**。repo id は不可。省略時は QWEN_TTS_MODEL / ./model")
    ap.add_argument("--refs", default=None,
                    help="refs.json のカンマ区切り。省略時は ./refs/*/refs.json を拾う")
    ap.add_argument("--outdir", default=None, help="生成した wav の保存先")
    ap.add_argument("--hf-home", default=None, help="HF キャッシュの場所")
    ap.add_argument("--device", default="auto", choices=["auto", "cuda:0", "mps", "cpu"])
    ap.add_argument("--port", type=int, default=7860)
    ap.add_argument("--native", action="store_true", help="ブラウザではなく専用ウィンドウで開く")
    ap.add_argument("--no-show", action="store_true",
                    help="ブラウザを自動で開かない (ヘッドレス起動・自動テスト用)")
    args = ap.parse_args()

    # **モデルを読む前に**行う。前のインスタンスが VRAM を掴んだままだと
    # こちらのロードが OOM で落ちるか、両方が上限に張り付いて応答しなくなる。
    _acquire_single_instance()

    with socket.socket() as s:
        # **`SO_REUSEADDR` を付けてはいけない。**Windows では使用中のポートにも
        # bind できてしまい、占有検出として機能しなくなる。
        try:
            s.bind(("127.0.0.1", args.port))
        except OSError as e:
            # ここまで来ている = 多重起動ガードは通っている。つまり**別のアプリ**が
            # 使っている。別ポートを渡せば起動できるので、逃げ道を必ず示す。
            raise SystemExit(
                f"ポート {args.port} を別のアプリが使っています ({e})。\n"
                f"  別のポートを指定してください: --port <番号>")

    if args.hf_home:
        os.environ["HF_HOME"] = args.hf_home
    # 配布物は通信させない。repo id 解決の通信待ちで固まる事故を構造的に防ぐ。
    os.environ.setdefault("HF_HUB_OFFLINE", "1")

    model_path = resolve_model(args.model)
    refs_paths = ([s.strip() for s in args.refs.split(",") if s.strip()] if args.refs
                  else [str(p) for p in sorted((app_dir() / "refs").glob("*/refs.json"))])
    _REFS = load_refs(refs_paths)
    print(f"参照セット {len(_REFS)} 件: {', '.join(sorted(_REFS)) or '(なし。ファイル入力のみ)'}")

    _OUT_DIR = Path(args.outdir) if args.outdir else app_dir() / "output"
    _OUT_DIR.mkdir(parents=True, exist_ok=True)

    device, dtype = pick_device(args.device)
    print(f"デバイス {device} / {str(dtype).replace('torch.', '')}", flush=True)
    if device == "cpu":
        print("[warn] CPU で動きます。1.7B の自己回帰デコードは実用にならない速度です。")

    from qwen_tts import Qwen3TTSModel

    print("モデルを読み込み中... (30〜60 秒)", flush=True)
    t = time.perf_counter()
    _MODEL = Qwen3TTSModel.from_pretrained(
        model_path, device_map=device, dtype=dtype, attn_implementation="sdpa"
    )
    print(f"読み込み完了 {time.perf_counter()-t:.1f}s", flush=True)

    from nicegui import app as nicegui_app, ui

    # 生成物はブラウザから直接再生させる (base64 に載せると数十 MB が毎回飛ぶ)
    nicegui_app.add_media_files("/out", _OUT_DIR)

    # キャラのタイル画像。**フォルダ内自己完結**にするため同梱物から配る
    # (別リポジトリへ切り出す前提。のべつべ本体の drawable と同じ素材)。
    logo = app_dir() / "assets" / "logo.png"
    if logo.is_file():
        nicegui_app.add_static_file(local_file=str(logo), url_path="/logo.png")

    novtube = app_dir() / "assets" / NOVTUBE_LOGO
    if novtube.is_file():
        nicegui_app.add_static_file(local_file=str(novtube), url_path=f"/{NOVTUBE_LOGO}")

    chibi_dir = app_dir() / "assets" / "chibi"
    if chibi_dir.is_dir():
        nicegui_app.add_static_files("/chibi", str(chibi_dir))
    else:
        print(f"[警告] chibi 画像が見つかりません ({chibi_dir})。タイルは ? 表示になります。")

    # 崩壊版タイル。**無くても動く** (差し替えが起きないだけ)。
    # 揃っているキャラだけを差し替え対象にする — 欠けたタイルを出さないため。
    for url, holder in ((fx.TILE_COLLAPSE, COLLAPSE_TILE_CHARS),
                        (fx.TILE_GORE, GORE_TILE_CHARS)):
        d = app_dir() / "assets" / url
        if d.is_dir():
            nicegui_app.add_static_files(f"/{url}", str(d))
            holder.update(p.stem for p in d.glob("*.webp"))

    global ASSET_VER
    stamps = [p.stat().st_mtime
              for name in (fx.TILE_NORMAL, fx.TILE_COLLAPSE, fx.TILE_GORE)
              for p in (app_dir() / "assets" / name).glob("*.webp")]
    ASSET_VER = str(int(max(stamps))) if stamps else "0"

    print(f"崩壊版タイル {len(COLLAPSE_TILE_CHARS)} 件 / "
          f"なれの果て {len(GORE_TILE_CHARS)} 件 / 画像版 {ASSET_VER}")

    # **NiceGUI 3.x では auto-index ページが廃止された。**2.x の「`ui.run()` の前に
    # 素で要素を作れば `/` に出る」書き方は通らず、**画面が真っ白**になる
    # (HTTP は 200、JS エラーも出ないので原因が見えにくい)。
    # `@ui.page` でページ関数として登録する。**クライアントごとに UI が組まれる**ので
    # 状態がブラウザ間で共有されなくなる副次的な利点もある。
    @ui.page("/")
    def _index() -> None:
        build_ui()

    # `dark=True` は**最初の描画から**ダークにするため (ページ側の `ui.dark_mode` だけだと
    # ソケット接続までの一瞬、白い画面がちらつく)。
    ui.run(title=APP_TITLE, port=args.port, native=args.native, dark=True,
           reload=False, show=not (args.native or args.no_show), favicon="🎙")


# **`__mp_main__` を入れてはいけない。**NiceGUI の作法として広まっている
# `if __name__ in {"__main__", "__mp_main__"}` は `reload=True` 用で、
# こちらは `reload=False` で使っている。入れるとサーバープロセス側でモジュールが
# 再読み込みされた際に `main()` が**二度目の実行**に入り、
# **モデル (3.4GB) をもう一度 VRAM に載せてイベントループを塞ぐ**。
# 症状は「ポートは listen しているのに HTTP に一切応答しない」で、原因が見えにくい。
if __name__ == "__main__":
    main()
