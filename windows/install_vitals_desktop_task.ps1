$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\pythonw.exe'
$handler = Join-Path $PSScriptRoot 'desktop_action.py'
$taskName = 'Vitals Desktop Control'

if (!(Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'Start Vitals once to create its Python environment before installing desktop controls.'
}

$autostart = Get-ScheduledTask -TaskName 'Vitals Autostart' -ErrorAction SilentlyContinue
$userId = if ($autostart) {
    $definition = [xml](Export-ScheduledTask -TaskName 'Vitals Autostart' -ErrorAction Stop)
    $definition.Task.Principals.Principal.UserId
} else {
    [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
}
$user = [Security.Principal.SecurityIdentifier]::new([string]$userId).Translate([Security.Principal.NTAccount]).Value
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing -and ($existing.Actions.Count -ne 1 -or $existing.Actions[0].Execute -ne $python -or
    !$existing.Actions[0].Arguments.Contains($handler))) {
    throw 'An unrelated task already uses the Vitals Desktop Control name.'
}

$action = New-ScheduledTaskAction -Execute $python -WorkingDirectory $root `
    -Argument ('-I "{0}" "$(Arg0)" "$(Arg1)"' -f $handler)
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Settings $settings -Force -ErrorAction Stop | Out-Null
Write-Output "Installed $taskName for $user."
