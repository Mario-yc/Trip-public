param(
    [switch]$SkipFormatCheck
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root "trip/Scripts/python.exe"
$frontend = Join-Path $root "frontend"

Write-Host "== Backend pytest =="
& $python -m pytest (Join-Path $root "backend/tests") -q --tb=short -p no:cacheprovider
if ($LASTEXITCODE -ne 0) {
    throw "Backend pytest failed with exit code $LASTEXITCODE"
}

Write-Host "== Frontend tests =="
Push-Location $frontend
try {
    npm test
    if ($LASTEXITCODE -ne 0) {
        throw "Frontend tests failed with exit code $LASTEXITCODE"
    }

    Write-Host "== Frontend build =="
    npm run build
    if ($LASTEXITCODE -ne 0) {
        throw "Frontend build failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}

Write-Host "== Backend lint =="
& $python -m ruff check (Join-Path $root "backend/src") (Join-Path $root "backend/tests")
if ($LASTEXITCODE -ne 0) {
    throw "Backend lint failed with exit code $LASTEXITCODE"
}

Write-Host "== Frontend lint =="
Push-Location $frontend
try {
    npm run lint
    if ($LASTEXITCODE -ne 0) {
        throw "Frontend lint failed with exit code $LASTEXITCODE"
    }

    if (-not $SkipFormatCheck) {
        Write-Host "== Frontend format check =="
        npm run format:check
        if ($LASTEXITCODE -ne 0) {
            throw "Frontend format check failed with exit code $LASTEXITCODE"
        }
    }
}
finally {
    Pop-Location
}
