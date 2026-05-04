$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root ".venv\Scripts\python.exe"
$hooksDir = Join-Path $root "hooks"
$modelDir = Join-Path $root "models\base.en"

if (-not (Test-Path $python)) {
    throw "Missing virtual environment. Create .venv with Python 3.11 first."
}

Write-Host "Downloading bundled base.en model..."
& $python (Join-Path $root "app.py") --download-model base.en --output-dir $modelDir
if ($LASTEXITCODE -ne 0) {
    throw "Model download failed."
}

Write-Host "Building LiveTranslator.exe..."
$pyiArgs = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--windowed",
    "--onedir",
    "--name", "LiveTranslator",
    "--collect-data", "faster_whisper",
    "--collect-data", "av",
    "--collect-binaries", "av",
    "--collect-binaries", "ctranslate2",
    "--additional-hooks-dir", $hooksDir,
    "--exclude-module", "argostranslate",
    "--exclude-module", "spacy",
    "--exclude-module", "stanza",
    "--exclude-module", "torch",
    "--hidden-import", "ctranslate2",
    "--add-data", "$modelDir;models\base.en",
    "app.py"
)
& $python @pyiArgs
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed."
}

Write-Host ""
Write-Host "Build complete:"
Write-Host "  dist\LiveTranslator\LiveTranslator.exe"
