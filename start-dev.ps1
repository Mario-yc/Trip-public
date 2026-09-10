[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$BackendPort = 8000,

    [ValidateRange(1, 65535)]
    [int]$FrontendPort = 5173
)

$ErrorActionPreference = "Stop"

function Get-ListeningProcessDescription {
    param([int]$Port)

    $listener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $listener) {
        return $null
    }

    try {
        $process = Get-Process -Id $listener.OwningProcess -ErrorAction Stop
        return "PID $($listener.OwningProcess) ($($process.ProcessName))"
    } catch {
        return "PID $($listener.OwningProcess)"
    }
}

function ConvertTo-PowerShellLiteral {
    param([string]$Value)

    return "'" + $Value.Replace("'", "''") + "'"
}

if ($BackendPort -eq $FrontendPort) {
    throw "BackendPort and FrontendPort must be different."
}

$repoRoot = $PSScriptRoot
$python = Join-Path $repoRoot "trip\Scripts\python.exe"
$frontend = Join-Path $repoRoot "frontend"
$frontendPackage = Join-Path $frontend "package.json"
$hostAddress = "localhost"
$frontendOrigin = "http://${hostAddress}:$FrontendPort"
$apiBaseUrl = "http://${hostAddress}:$BackendPort/api"

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Backend Python was not found: $python"
}

if (-not (Test-Path -LiteralPath $frontendPackage -PathType Leaf)) {
    throw "Frontend package.json was not found: $frontendPackage"
}

$npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
if (-not $npm) {
    $npm = Get-Command npm -ErrorAction SilentlyContinue
}
if (-not $npm) {
    throw "npm was not found in PATH. Install Node.js, then run this script again."
}

foreach ($service in @(
        @{ Name = "Backend"; Port = $BackendPort },
        @{ Name = "Frontend"; Port = $FrontendPort }
    )) {
    $owner = Get-ListeningProcessDescription -Port $service.Port
    if ($owner) {
        throw "$($service.Name) port $($service.Port) is already in use by $owner. Stop that process or choose another port."
    }
}

$terminal = (Get-Process -Id $PID).Path
if (-not (Test-Path -LiteralPath $terminal -PathType Leaf)) {
    throw "Could not determine the current PowerShell executable."
}

$quotedRoot = ConvertTo-PowerShellLiteral -Value $repoRoot
$quotedPython = ConvertTo-PowerShellLiteral -Value $python
$quotedFrontend = ConvertTo-PowerShellLiteral -Value $frontend
$quotedNpm = ConvertTo-PowerShellLiteral -Value $npm.Source

$backendCommand = @"
`$Host.UI.RawUI.WindowTitle = 'Trip Backend ($hostAddress`:$BackendPort)'
Set-Location -LiteralPath $quotedRoot
`$env:FRONTEND_ORIGIN = '$frontendOrigin'
& $quotedPython -m uvicorn src.main:app --app-dir backend --host $hostAddress --port $BackendPort --reload
"@

$frontendCommand = @"
`$Host.UI.RawUI.WindowTitle = 'Trip Frontend ($hostAddress`:$FrontendPort)'
Set-Location -LiteralPath $quotedFrontend
`$env:VITE_API_BASE_URL = '$apiBaseUrl'
& $quotedNpm run dev -- --host $hostAddress --port $FrontendPort --strictPort
"@

Start-Process -FilePath $terminal -ArgumentList @("-NoExit", "-Command", $backendCommand) | Out-Null
Start-Process -FilePath $terminal -ArgumentList @("-NoExit", "-Command", $frontendCommand) | Out-Null

Write-Host "Backend starting:  http://${hostAddress}:$BackendPort"
Write-Host "Frontend starting: http://${hostAddress}:$FrontendPort"
Write-Host "Two PowerShell windows were opened. Close either window or press Ctrl+C in it to stop that service."
