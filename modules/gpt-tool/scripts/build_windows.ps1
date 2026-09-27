$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$Python = if ($env:PYTHON_BIN) { $env:PYTHON_BIN } else { "python" }
& $Python -m pip install -q -U -r requirements.txt -r requirements-build.txt
& $Python -m nuitka `
    "--mode=onefile" `
    "--assume-yes-for-downloads" `
    "--windows-console-mode=disable" `
    "--output-dir=dist" `
    "--output-filename=GPT-Tool.exe" `
    "--include-data-dir=web=web" `
    "--company-name=shoptaikhoan" `
    "--product-name=shoptaikhoan - auto import" `
    "--file-description=shoptaikhoan - auto import" `
    "--file-version=1.0.0.0" `
    "--product-version=1.0.0.0" `
    "gpt_tool/server.py"

if (-not (Test-Path "dist\GPT-Tool.exe")) {
    throw "Build completed without dist\GPT-Tool.exe"
}
Write-Host "Built: $Root\dist\GPT-Tool.exe"
