[CmdletBinding()]
param(
    [string]$Python = "python",
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $projectRoot
try {
    $requiredAssets = @(
        "tools\cpu-avs-workload",
        "tools\gpu-avs-workload",
        "tools\shaders\vulkan\fullscreen.vert.spv",
        "tools\shaders\vulkan\workload.frag.spv"
    )
    foreach ($asset in $requiredAssets) {
        if (-not (Test-Path -LiteralPath (Join-Path $projectRoot $asset) -PathType Leaf)) {
            throw "Required release asset is missing: $asset. Stage the HarmonyOS workload builds and compiled SPIR-V before packaging."
        }
    }
    if (-not $SkipTests) {
        & $Python -m unittest discover -s tests -v
        if ($LASTEXITCODE -ne 0) { throw "Unit tests failed" }
    }
    & $Python -c "import serial, yaml, PyInstaller"
    if ($LASTEXITCODE -ne 0) {
        throw "Packaging dependencies are missing. Install requirements-dev.txt into the selected Python environment."
    }
    & $Python -m PyInstaller --clean --noconfirm vmin_judge.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed" }
    & (Join-Path $projectRoot "dist\vmin_judge.exe") --version
    if ($LASTEXITCODE -ne 0) { throw "Packaged executable smoke test failed" }
}
finally {
    Pop-Location
}
