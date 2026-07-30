<#
.SYNOPSIS
    QwenTTSStudio を Windows 向けに PyInstaller で固める。

.DESCRIPTION
    onedir でビルドし、dist/QwenTTSStudio/ 以下に一式が出る。

    参照音声 (refs/) は spec が同梱するので、ここでは何もしない。
    モデルは同梱しない (約 3.4GB あるため)。-ModelPath を渡すとビルド後に
    dist へコピーして、単体で動く形にする。渡さない場合は配布先で
    setup_model.py を実行してもらう。

.EXAMPLE
    pwsh ./build_windows.ps1 -Venv D:\path\to\.venv
    pwsh ./build_windows.ps1 -Venv ... -ModelPath D:\...\snapshots\xxxx
#>
param(
    # 依存が入っている venv。省略時は現在の python を使う
    [string]$Venv = "",
    # 同梱するモデルの snapshot ディレクトリ (省略可)
    [string]$ModelPath = "",
    [switch]$Clean
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$py = if ($Venv) { Join-Path $Venv "Scripts\python.exe" } else { "python" }
if ($Venv -and -not (Test-Path $py)) { throw "venv の python が無い: $py" }

Write-Host "python: $py"
& $py -c "import sys; print(sys.version)"

# PyInstaller が無ければ入れる
& $py -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "PyInstaller を導入します..."
    & $py -m pip install pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller の導入に失敗" }
}

if ($Clean) {
    foreach ($d in @("build", "dist")) {
        if (Test-Path $d) { Remove-Item -Recurse -Force $d }
    }
}

$env:PYTHONUTF8 = "1"
& $py -m PyInstaller qwen_tts_studio.spec --noconfirm
if ($LASTEXITCODE -ne 0) { throw "ビルドに失敗" }

$out = Join-Path $PSScriptRoot "dist\QwenTTSStudio"

if ($ModelPath) {
    if (-not (Test-Path $ModelPath)) { throw "モデルが無い: $ModelPath" }
    Write-Host "モデルを同梱: $ModelPath (数 GB のコピーです)"
    Copy-Item -Recurse -Force $ModelPath (Join-Path $out "model")
}

$size = (Get-ChildItem -Recurse $out | Measure-Object -Property Length -Sum).Sum / 1GB
Write-Host ""
Write-Host "完成: $out  ($([math]::Round($size,2)) GB)"
Write-Host "起動: $out\QwenTTSStudio.exe"
