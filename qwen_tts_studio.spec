# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec。**onedir 専用。**

onefile にしてはならない。torch + CUDA ランタイムだけで数 GB あり、onefile は
起動のたびに全部を temp へ展開するので、起動に数分かかるうえディスクも食う。

同梱しないもの:
  - モデル本体 (1.7B fp16 で約 3.4GB)。exe と同じ場所の `model/` に置くか
    QWEN_TTS_MODEL で場所を渡す。`setup_model.py` で取得できる。

同梱するもの:
  - 参照音声 `refs/`。リポジトリに入っている (Common Voice / CC-0)。
    入れないとキャラタイルが 1 枚も出ず、ファイル入力しか使えない配布物になる。

ビルド:
    pyinstaller qwen_tts_studio.spec --noconfirm
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, copy_metadata

datas, binaries, hiddenimports = [], [], []

# NiceGUI はフロントエンドの静的ファイル (Vue / Quasar) とテンプレートを実行時に読む。
# collect_all しないと起動直後に「テンプレートが無い」で落ちる。
# バージョンを importlib.metadata で見るので copy_metadata も要る。
for pkg in ("nicegui", "uvicorn", "fastapi", "starlette", "psutil"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
        datas += copy_metadata(pkg)
    except Exception as e:      # noqa: BLE001  未導入の補助パッケージは飛ばす
        print(f"[spec] skip {pkg}: {e}")

# モデル定義・トークナイザ・音声コーデックの設定ファイル群
for pkg in ("qwen_tts", "transformers", "tokenizers"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# torch と CUDA ランタイム。nvidia-* は DLL の塊なので collect_all で丸ごと持つ。
for pkg in ("torch", "nvidia"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception as e:      # noqa: BLE001  CPU 版 torch には nvidia が無い
        print(f"[spec] skip {pkg}: {e}")

# soundfile / librosa は native ライブラリとデータを持つ
# `av` は m4a のデコードに使う (libsndfile が MPEG-4 を読めないため)。
# `faster_whisper` は参照音声の自動書き起こしに使う。推論エンジンの `ctranslate2` は
# DLL の塊なので collect_all で丸ごと持つ。**モデル本体は同梱しない** (初回実行時に
# HF から落ちてくる)。
for pkg in ("soundfile", "librosa", "soxr", "numba", "lazy_loader", "av",
            "faster_whisper", "ctranslate2"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception as e:      # noqa: BLE001
        print(f"[spec] skip {pkg}: {e}")

# `app_dir()` は frozen 時に **exe のあるフォルダ**を返す。`assets/` はそこから
# 相対で読むので、同じ階層に展開されるよう `.` 側にも同名で置く。
# 入れ忘れるとタイトルロゴもキャラタイルも出ない (文字と ? に落ちる)。
datas += [("collapse_fx.py", "."), ("assets", "assets")]

# 参照音声も同梱する (上の docstring を参照)。
if Path("refs").is_dir():
    datas += [("refs", "refs")]

a = Analysis(
    ["app.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports + ["collapse_fx"],
    hookspath=[],
    runtime_hooks=[],
    # 巨大なわりに使わないものを外す。torch のテストは特に重い。
    excludes=["tkinter", "matplotlib", "pytest", "IPython", "notebook",
              "torch.test", "torch.testing", "torch.distributed.elastic"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="QwenTTSStudio",
    debug=False,
    strip=False,
    upx=False,          # UPX は torch/CUDA の DLL を壊すことがある。使わない
    console=True,       # ログを見せる。GUI はブラウザ側なのでコンソールを消す利点が薄い
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="QwenTTSStudio",
)
