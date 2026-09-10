param(
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 4479,
    [string]$ProxyUrl = "http://127.0.0.1:10793"
)

$ErrorActionPreference = "Stop"

function Test-TcpPort {
    param(
        [string]$HostName,
        [int]$PortNumber
    )
    try {
        $client = [System.Net.Sockets.TcpClient]::new()
        $async = $client.BeginConnect($HostName, $PortNumber, $null, $null)
        $connected = $async.AsyncWaitHandle.WaitOne(350)
        if ($connected) {
            $client.EndConnect($async)
        }
        $client.Close()
        return $connected
    } catch {
        return $false
    }
}

$ddgsCommand = Get-Command ddgs -ErrorAction SilentlyContinue
if (-not $ddgsCommand) {
    Write-Host "ddgs CLI was not found."
    Write-Host "Install optional free search support with:"
    Write-Host '  .\trip\Scripts\python.exe -m pip install "ddgs[api]"'
    exit 1
}

$argsList = @("api", "--host", $HostAddress, "--port", [string]$Port)
$proxyUri = [Uri]$ProxyUrl
if (Test-TcpPort -HostName $proxyUri.Host -PortNumber $proxyUri.Port) {
    $argsList += @("-pr", $ProxyUrl)
    Write-Host "Using local proxy $($proxyUri.Host):$($proxyUri.Port)."
} else {
    Write-Host "Local proxy $($proxyUri.Host):$($proxyUri.Port) not detected; starting DDGS without proxy."
}

Write-Host "Starting DDGS API at http://${HostAddress}:$Port ..."
& $ddgsCommand.Source @argsList
