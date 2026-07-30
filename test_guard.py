#!/usr/bin/env python
"""起動まわりのガードの回帰テスト。**モデルは一切読まない** (VRAM を使わない)。

    python test_guard.py

守る対象は 2 つ。**(A) 多重起動ガード**と、**(B) 参照の許可リスト**。

(B) は「このツールに出すキャラは意図して選んである」を守る。`--refs` は外から
自由に渡せるので、渡されたものを素直に読むと**出さないと決めたキャラが出る**
(実際に起こした)。許可リストは `CHARACTER_NAMES`、除外リストは**持たない**
(出さないと決めた名前がコードに残るため)。

(A) は過去に「コマンドライン文字列の照合で前インスタンスを特定する」実装を入れ、
相対パス起動 (`python app.py`) で照合が必ず外れて**ガードがあるのに素通りする**
事故を起こした。モデルは 3.4GB を VRAM に載せるため、素通りしたまま起動を
繰り返すと VRAM を使い切って全インスタンスが応答しなくなる。

このテストが守る性質:

  1. 誰も居なければ即座に取れる
  2. 前インスタンスが居れば、それを終了させて引き継ぐ
  3. 前インスタンスを終了できなければ **起動しない** (fail closed)
  4. ロック機構そのものが使えなければ **警告のうえ起動する** (fail open)
     — ここを fail closed にすると、ロックを作れない環境の人が永久に起動できない
  5. PID が再利用された別プロセスを巻き添えにしない
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

STUDIO = Path(__file__).resolve().parent
PY = sys.executable

# **本物のロックには触らない。**このテストは `cleanup()` でロックと info を消すので、
# アプリを起動したまま走らせると**動いているインスタンスの info を消してしまい**、
# 次の起動が「前インスタンスを特定できません」で拒否される (実際にやらかした)。
# 専用の一時フォルダを使い、子プロセスにも env で渡す。
LOCK = Path(tempfile.mkdtemp(prefix="qwen_guard_test_")) / "qwen_tts_studio.lock"
INFO = LOCK.with_suffix(".info")
ENV = dict(os.environ, PYTHONUTF8="1", PYTHONPATH=str(STUDIO),
           QWEN_TTS_STUDIO_TEST_LOCK=str(LOCK))

#: 子プロセス側でロックの置き場所をテスト用へ差し替える前置き。
USE_TEST_LOCK = """
import os
from pathlib import Path
app._LOCK_PATH = Path(os.environ["QWEN_TTS_STUDIO_TEST_LOCK"])
app._INFO_PATH = app._LOCK_PATH.with_suffix(".info")
"""

HOLDER = """
import os, sys, time
sys.path.insert(0, r"{studio}")
import app
""" + USE_TEST_LOCK + """
app._acquire_single_instance()
print("HOLDER_OK", os.getpid(), flush=True)
time.sleep(300)
"""

TAKER = """
import sys
sys.path.insert(0, r"{studio}")
import app
""" + USE_TEST_LOCK + """
try:
    app._acquire_single_instance()
    print("TAKER_OK", flush=True)
except SystemExit as e:
    print("TAKER_REFUSED", flush=True)
    print(e, flush=True)
"""

BLOCKED = """
import sys
sys.path.insert(0, r"{studio}")
from pathlib import Path
import app
app._LOCK_PATH = Path("Z:/does/not/exist/qwen.lock")   # 作れない場所
app._INFO_PATH = app._LOCK_PATH.with_suffix(".info")
app._acquire_single_instance()
print("STARTED_ANYWAY", flush=True)
"""

_ok = _fail = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _ok, _fail
    if cond:
        _ok += 1
        print(f"  PASS  {name}")
    else:
        _fail += 1
        print(f"  FAIL  {name}  {detail}")


def run(src: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run([PY, "-u", "-c", src.format(studio=STUDIO)],
                          cwd=str(STUDIO), env=ENV, capture_output=True, text=True,
                          timeout=timeout, encoding="utf-8", errors="replace")


def spawn(src: str) -> subprocess.Popen:
    return subprocess.Popen([PY, "-u", "-c", src.format(studio=STUDIO)],
                            cwd=str(STUDIO), env=ENV, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace")


def wait_holder(p: subprocess.Popen, timeout: int = 120) -> int | None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        line = p.stdout.readline()
        if not line and p.poll() is not None:
            return None
        if "HOLDER_OK" in line:
            return int(line.split()[1])
    return None


def stop(p) -> None:
    try:
        p.kill()
        p.wait(timeout=10)
    except Exception:
        pass


def cleanup() -> None:
    for f in (LOCK, INFO):
        try:
            f.unlink(missing_ok=True)
        except Exception:
            pass


def main() -> int:
    print("=" * 70)
    print("1. 誰も居ないときは取れる")
    cleanup()
    r = run(TAKER)
    check("取得できる", "TAKER_OK" in r.stdout, r.stdout + r.stderr[-400:])

    print("\n2. 前インスタンスが居れば終了させて引き継ぐ")
    cleanup()
    h = spawn(HOLDER)
    hp = wait_holder(h)
    check("holder がロックを取った", hp is not None)
    if hp:
        info = INFO.read_text("utf-8").split()
        check("info に holder の PID が記録される", bool(info) and int(info[0]) == hp,
              f"info={info} holder={hp}")
        r = run(TAKER)
        check("taker が引き継げた", "TAKER_OK" in r.stdout, r.stdout)
        check("holder を終了させた", "を終了します" in r.stdout, r.stdout)
        time.sleep(1)
        check("holder が実際に消えた", h.poll() is not None, f"rc={h.poll()}")
    stop(h)

    print("\n3. 終了できない相手なら起動しない (fail closed)")
    cleanup()
    h = spawn(HOLDER)
    if wait_holder(h):
        INFO.write_text("999999\n0.0\n", "utf-8")      # 特定できない状態にする
        r = run(TAKER)
        check("拒否して起動しない", "TAKER_REFUSED" in r.stdout, r.stdout)
        check("無関係な PID を落としに行かない",
              ("触りません" in r.stdout or "特定できません" in r.stdout), r.stdout)
        check("holder は生きたまま", h.poll() is None)
    stop(h)

    print("\n4. ロック機構が使えないなら警告して起動する (fail open)")
    cleanup()
    r = run(BLOCKED)
    check("起動を止めない", "STARTED_ANYWAY" in r.stdout, r.stdout + r.stderr[-300:])
    check("黙って素通りせず警告する", "[警告]" in r.stdout, r.stdout)

    print("\n5. PID 再利用の巻き添え防止")
    cleanup()
    victim = subprocess.Popen([PY, "-c", "import time; time.sleep(120)"])
    h = spawn(HOLDER)
    if wait_holder(h):
        INFO.write_text(f"{victim.pid}\n0.0\n", "utf-8")   # 生成時刻が一致しない
        r = run(TAKER)
        check("生成時刻が違うプロセスは落とさない", victim.poll() is None,
              f"victim rc={victim.poll()}")
        check("落とせないので起動を拒否する", "TAKER_REFUSED" in r.stdout, r.stdout)
    stop(victim)
    stop(h)

    print("\n6. 登録外のキャラの参照は読み込まない (許可リスト)")
    check_refs_allowlist()

    print("\n7. 接合位置はいちばん長い無音を選ぶ (ランダムにしない)")
    check_break_point()

    cleanup()
    print("\n" + "=" * 70)
    print(f"PASS {_ok} / FAIL {_fail}")
    return 1 if _fail else 0


def check_break_point() -> None:
    """`find_break_point` が**浅い谷ではなく長い無音**を選ぶこと。

    ここをランダム選択に戻すと、語の途中の小さな谷を掴んで「ぶつっと切り替わった」
    接合になる (実際にそうなっていた)。**呼び出しごとに結果が変わらないこと**も見る。
    """
    import numpy as np

    import collapse_fx as fx

    sr = fx.SR
    y = (np.random.default_rng(0).standard_normal(sr * 6) * 0.3).astype(np.float32)
    # 2.5s に浅く短い谷 (60ms)、3.8s に深く長い無音 (400ms) を作る
    y[int(2.50 * sr):int(2.56 * sr)] *= 0.2
    y[int(3.80 * sr):int(4.20 * sr)] = 0.0

    cut = fx.find_break_point(y, sr, 2.0, 5.0) / sr
    check("長い無音のほうを選ぶ", 3.85 < cut < 4.15, f"cut={cut:.3f}s")
    check("呼ぶたびに同じ位置を返す",
          fx.find_break_point(y, sr, 2.0, 5.0) == fx.find_break_point(y, sr, 2.0, 5.0))

    # 無音が無い区間では「いちばん静かなところ」に落ちる (範囲の端に逃げない)
    flat = (np.random.default_rng(1).standard_normal(sr * 6) * 0.3).astype(np.float32)
    flat[int(3.0 * sr):int(3.02 * sr)] *= 0.05
    cut2 = fx.find_break_point(flat, sr, 2.0, 5.0) / sr
    check("無音が無ければ最小音量の位置", 2.95 < cut2 < 3.10, f"cut={cut2:.3f}s")


def check_refs_allowlist() -> None:
    """`--refs` に登録外のフォルダを混ぜても、読み込まれないこと。

    **落とした側の名前を出力に出さないこと**も併せて確認する。ログに出れば
    「誰を外したか」がそのまま公開リポジトリの実行ログに残る。
    """
    import io
    import json
    from contextlib import redirect_stdout

    sys.path.insert(0, str(STUDIO))
    import app                                        # モデルは読まない

    known = next(iter(app.CHARACTER_NAMES))           # 許可されているキャラ
    unknown = "zz_not_registered"                     # 許可リストに無いフォルダ
    tmp = Path(tempfile.mkdtemp(prefix="qwen_refs_test_"))
    paths = []
    for group in (known, unknown):
        d = tmp / group
        d.mkdir()
        (d / "refs.json").write_text(json.dumps(
            [{"name": "normal_01", "wav": str(d / "a.wav"), "ref_text": "てすと"}]
        ), encoding="utf-8")
        paths.append(str(d / "refs.json"))

    buf = io.StringIO()
    with redirect_stdout(buf):
        refs = app.load_refs(paths)
    out = buf.getvalue()

    groups = {k.split("/")[0] for k in refs}
    check("許可されたキャラは読み込む", known in groups, f"groups={groups}")
    check("登録外は読み込まない", unknown not in groups, f"groups={groups}")
    check("落とした相手の名前を出力しない", unknown not in out, out)
    check("落としたこと自体は分かる", "読み飛ばした" in out, out)


if __name__ == "__main__":
    sys.exit(main())
