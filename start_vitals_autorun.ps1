$ErrorActionPreference = 'Stop'
$configPath = Join-Path $PSScriptRoot 'config.json'

if (Test-Path -LiteralPath $configPath) {
    $config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $bindAddress = [System.Net.IPAddress]::None

    # 仅在绑定指定网卡 IP 时等待。Serve 使用回环代理，0.0.0.0 可直接启动。
    if ([System.Net.IPAddress]::TryParse($config.host, [ref]$bindAddress) -and
        !$bindAddress.Equals([System.Net.IPAddress]::Any) -and
        !$bindAddress.Equals([System.Net.IPAddress]::IPv6Any) -and
        ![System.Net.IPAddress]::IsLoopback($bindAddress)) {
        $deadline = (Get-Date).AddMinutes(2)
        while (!(Get-NetIPAddress -IPAddress $config.host -AddressState Preferred -ErrorAction SilentlyContinue)) {
            if ((Get-Date) -ge $deadline) {
                Write-Error "Vitals bind address did not become available: $($config.host)"
                exit 1
            }
            Start-Sleep -Seconds 2
        }
    }
}

& (Join-Path $PSScriptRoot 'start_service.bat')
exit $LASTEXITCODE
