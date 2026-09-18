# build_pyinstaller.ps1 - OMG freeze build (Phase 5 packaging + size reduction)
#
# Prerequisites:
#   - Python env with `pip install pyinstaller PySide6 pefile capstone`
#   - Binary assets live under $SourceLib (default ../OMGDev/lib). The script copies
#     ONLY the explicitly whitelisted files into resources/binaries, so loader/alchemy
#     can locate them at runtime via omg.core.paths.
#
# Size policy (do NOT revert):
#   This script copies ONLY the files explicitly listed in $AllowFiles below.
#   Never use `Copy-Item "*"` again. Historically copying the whole OMGDev/lib pulled
#   ffmpeg (192 MB, zero callers in the whole codebase), MysteriousRitual (d3d11 seed),
#   texture_tools.dll (dead modtools code), and duplicate copies of
#   d3d11.dll / upx.exe / pe_diversifier.dll into the release.
#
# Usage:
#   pwsh build_pyinstaller.ps1 [-SourceLib "../OMGDev/lib"] [-OutDir "dist"]
param(
    [string]$SourceLib = (Resolve-Path (Join-Path $PSScriptRoot "../OMGDev/lib") -ErrorAction SilentlyContinue).Path,
    [string]$OutDir    = "dist",
    [string]$WorkDir   = "build",
    [int]$WarnSizeMB   = 250
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot

# Join-Path in Windows PowerShell 5.1 accepts ONLY -Path + -ChildPath (two positional
# args). Passing 3+ segments throws "PositionalParameterNotFound". PS7's
# -AdditionalChildPath does not exist here, so chain Join-Path instead.
function Join-Many {
    $p = $args[0]
    for ($i = 1; $i -lt $args.Count; $i++) { $p = Join-Path $p $args[$i] }
    return $p
}

Write-Host "[OMG] Starting freeze build ..." -ForegroundColor Cyan

# 1) Generate the spec (PyInstaller auto-collects omg.* from the src layout;
#    resources go through the pyi_filter whitelist).
#    Deliberately NOT using --clean: under this sandbox it routes through os.remove to
#    delete the old toc, which triggers SHFileOperationW failure and crashes the build.
#    For a clean build, point -WorkDir / -OutDir at a fresh directory instead.
pyinstaller --noconfirm --workpath $WorkDir --distpath $OutDir OMG.spec

# 2) Whitelist binaries land in resources/binaries.
#    pathex=src + contents_directory=bin => runtime _MEIPASS = <OutDir>/OMG/bin
#    so the final binary path is <OutDir>/OMG/bin/resources/binaries.
#
#    Each entry must document WHO uses it, otherwise the next person cannot safely
#    delete or add anything:
$AllowFiles = @(
    # domain/inject/loader.py: LOADER_EXE_SRC -- the "loader" inject method (default) executes this
    "3DMigoto Loader.exe",
    # domain/inject/loader.py: INJECTOR_SRC (hook/inject methods) + launch/controller.py fallback path
    "3dmloader.dll"
)
# Explicitly NOT shipped (kept here as a written record of "evaluated and rejected"):
#   ffmpeg/            -> only consumer domain/media has zero importers in the whole codebase
#   MysteriousRitual/  -> d3d11 seed; changed to "must run one quick-build first"
#   texture_tools.dll  -> domain/modtools has zero importers in the whole codebase
#   upx.exe / pe_diversifier.dll / d3d11.dll
#                      -> byte-identical to resources/binaries/ritual/ (upx/pe_diversifier),
#                         or permanently shadowed by MysteriousRitual (d3d11.dll)

$BinTarget = Join-Many $Root $OutDir "OMG" "bin" "resources" "binaries"
New-Item -ItemType Directory -Force -Path $BinTarget | Out-Null

if ($SourceLib -and (Test-Path $SourceLib)) {
    Write-Host "[OMG] Whitelist-copying binary assets: $SourceLib -> $BinTarget"
    $missing = @()
    foreach ($name in $AllowFiles) {
        $src = Join-Path $SourceLib $name
        if (Test-Path $src) {
            # OMG.spec already collects resources/binaries/** via pyi_filter, and both
            # whitelist files now live in src/omg/resources/binaries/loader/. If the same
            # bytes are already in the tree, skip the copy instead of packing a duplicate.
            $dup = Get-ChildItem -Recurse -File -Path $BinTarget -Filter $name -ErrorAction SilentlyContinue |
                   Where-Object { $_.Length -eq (Get-Item $src).Length } | Select-Object -First 1
            if ($dup) {
                Write-Host ("  [SKIP] {0} (already in package as {1})" -f $name, $dup.FullName.Substring($BinTarget.Length + 1)) -ForegroundColor DarkGray
                continue
            }
            Copy-Item -Force $src $BinTarget
            Write-Host ("  [OK]   {0}" -f $name) -ForegroundColor DarkGray
        } else {
            $missing += $name
            Write-Warning ("  [MISS] {0} (absent in source dir; the corresponding runtime feature will degrade)" -f $name)
        }
    }
    if ($missing.Count -gt 0) {
        Write-Warning "[OMG] $($missing.Count) whitelist file(s) missing, please verify -SourceLib"
    }
} else {
    Write-Warning "[OMG] Binary asset dir '$SourceLib' not found; pass the OMGDev/lib path via -SourceLib"
}

# 3) Product size check (a size regression is a real bug, not a detail)
$AppDir = Join-Many $Root $OutDir "OMG"
if (Test-Path $AppDir) {
    $bytes = (Get-ChildItem -Recurse -File $AppDir | Measure-Object -Property Length -Sum).Sum
    $mb = [math]::Round($bytes / 1MB, 1)
    $line = "[OMG] Build done, product at $AppDir (size {0} MB)" -f $mb
    if ($mb -gt $WarnSizeMB) {
        Write-Warning ("$line -- exceeds {0} MB threshold, check for a runtime data dir being mispacked" -f $WarnSizeMB)
    } else {
        Write-Host $line -ForegroundColor Green
    }
}
