[CmdletBinding()]
param(
    [string]$Python = "python",
    [switch]$SkipTests,
    [switch]$IncludeNpu
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $projectRoot
try {
    $requiredAssets = @(
        "tools\cpu-avs-workload",
        "tools\gpu-avs-workload",
        "tools\relay\kirin9030\avs-uart-relay",
        "tools\shaders\vulkan\fullscreen.vert.spv",
        "tools\shaders\vulkan\workload.frag.spv"
    )
    if ($IncludeNpu) { $requiredAssets += @("tools\npu-avs-workload", "tools\npu\profiles\kws01\manifest.json", "tools\npu\profiles\ic01\manifest.json", "tools\npu\profiles\ad01\manifest.json", "tools\npu\profiles\sww01\manifest.json") }
    foreach ($asset in $requiredAssets) {
        if (-not (Test-Path -LiteralPath (Join-Path $projectRoot $asset) -PathType Leaf)) {
            throw "Required release asset is missing: $asset. Stage the HarmonyOS workload builds and compiled SPIR-V before packaging."
        }
    }
    if (-not $SkipTests) {
        $oldErrorActionPreference = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        
        & $Python -m unittest discover -s tests -v
        

        $ErrorActionPreference = $oldErrorActionPreference
        if ($LASTEXITCODE -ne 0) { throw "Unit tests failed" }
    }
    $packageArguments = @("main.py", "validate", "--package")
    if ($IncludeNpu) { $packageArguments += "--include-npu" }
    & $Python @packageArguments
    if ($LASTEXITCODE -ne 0) { throw "Package validation failed; stage the required device assets and runtime configuration." }
    & $Python -c "import serial, yaml, PyInstaller"
    if ($LASTEXITCODE -ne 0) {
        throw "Packaging dependencies are missing. Install requirements-dev.txt into the selected Python environment."
    }
    $oldErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    
    & $Python -m PyInstaller --clean --noconfirm vmin_judge.spec
    
 
    $ErrorActionPreference = $oldErrorActionPreference
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed" }
    & (Join-Path $projectRoot "dist\vmin_judge.exe") --version
    if ($LASTEXITCODE -ne 0) { throw "Packaged executable smoke test failed" }
    & (Join-Path $projectRoot "dist\vmin_judge.exe") stress validate-plan --plan (Join-Path $projectRoot "config\soak\stress.example.json")
    if ($LASTEXITCODE -ne 0) { throw "Packaged stress command smoke test failed" }
}
finally {
    Pop-Location
}
