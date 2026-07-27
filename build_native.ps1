# SPDX-License-Identifier: GPL-3.0-or-later

param(
    [ValidateSet("Debug", "Release")]
    [string]$Configuration = "Release"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Build = Join-Path $Root "build"

cmake -S $Root -B $Build -G "Visual Studio 17 2022" -A x64
if ($LASTEXITCODE -ne 0) { throw "CMake configure failed with exit code $LASTEXITCODE." }
cmake --build $Build --config $Configuration --parallel
if ($LASTEXITCODE -ne 0) { throw "Native build failed with exit code $LASTEXITCODE." }
cmake --install $Build --config $Configuration --prefix $Root
if ($LASTEXITCODE -ne 0) { throw "Native install failed with exit code $LASTEXITCODE." }
