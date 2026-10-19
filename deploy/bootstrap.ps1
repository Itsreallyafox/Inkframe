<#
.SYNOPSIS
    One-time first install of Inkframe on the Pi, from the desktop. Safe to re-run.

.DESCRIPTION
    1. Copies install.sh to the Pi (~/install.sh) and runs "install.sh --bootstrap"
       there: apt packages, groups, SPI, /opt/inkframe, bare repo + hook, venv,
       sudoers drop-in.
    2. If that exits 3 (SPI was just enabled), reboots the Pi and waits up to
       180 s for it to come back with a new boot id.
    3. Checks that /dev/spidev0.0 exists.
    4. Runs deploy.ps1 -SkipFrame: it adds the "pi" git remote if needed and the
       first push installs and starts the service.
    5. Prints the service state and GET /api/status.

    Before it touches the Pi it checks that there is something deployable:
    main committed with a clean working tree, unless -Snapshot or -Force says
    otherwise, so a dirty tree stops it in seconds instead of after the
    bootstrap and reboot.

    Windows PowerShell 5.1 compatible. Runbook: docs\DEPLOY.md.
    Run it as: powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\bootstrap.ps1

.PARAMETER Snapshot
    Passed to deploy.ps1: deploy the working tree as it is, uncommitted and
    untracked files included, as a snapshot commit on no local branch.

.PARAMETER Force
    Passed to deploy.ps1: deploy main even if the working tree has uncommitted
    changes (they are not deployed).
#>
[CmdletBinding()]
param(
    [switch]$Snapshot,
    [switch]$Force
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$PiHost = 'inkframe'
$SshOptions = @('-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10')
$PollOptions = @('-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5')
# notice a dropped connection quickly while the Pi goes down
$RebootOptions = $SshOptions + @('-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=2')
$RebootTimeoutSeconds = 180
$PollSeconds = 5
$RebootRequiredExit = 3

function Invoke-Native {
    <#
    .SYNOPSIS
        Run a native command; returns its stdout lines and leaves the exit
        code in $LASTEXITCODE. $ErrorActionPreference is relaxed for the call
        because Windows PowerShell 5.1 turns native stderr into terminating
        errors in some hosts. -IgnoreStderr discards stderr.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$ArgumentList = @(),
        [switch]$IgnoreStderr
    )
    $saved = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        if ($IgnoreStderr) {
            & $FilePath @ArgumentList 2>$null
        }
        else {
            & $FilePath @ArgumentList
        }
    }
    finally {
        $ErrorActionPreference = $saved
    }
}

function Invoke-Pi {
    <# .SYNOPSIS Run a shell command on the Pi over ssh; returns stdout lines. #>
    param([Parameter(Mandatory = $true)][string]$Command)
    Invoke-Native -FilePath 'ssh' -ArgumentList ($SshOptions + @($PiHost, $Command))
}

function Stop-WithError {
    <# .SYNOPSIS Print an error and exit the script with status 1. #>
    param([Parameter(Mandatory = $true)][string]$Message)
    Write-Host ('ERROR: ' + $Message) -ForegroundColor Red
    exit 1
}

function Write-StatusJson {
    <# .SYNOPSIS Pretty-print a JSON document, or print it raw if it does not parse. #>
    param([string]$Raw)
    try {
        $Raw | ConvertFrom-Json | ConvertTo-Json -Depth 6 | Write-Host
    }
    catch {
        Write-Host $Raw
    }
}

function Get-PiBootId {
    <# .SYNOPSIS The Pi's current boot id, or '' if it cannot be reached. #>
    $id = (Invoke-Native -FilePath 'ssh' -ArgumentList ($PollOptions + @($PiHost, 'cat /proc/sys/kernel/random/boot_id')) -IgnoreStderr) -join ''
    if ($LASTEXITCODE -ne 0) { return '' }
    return $id.Trim()
}

function Restart-Pi {
    <# .SYNOPSIS Reboot the Pi and wait until it is back with a new boot id. #>
    $before = Get-PiBootId
    if (-not $before) { Stop-WithError 'cannot read the boot id before rebooting' }
    Write-Host '==> rebooting the Pi (SPI was enabled)'
    # the connection drops while the command runs, so its exit code means nothing
    $null = Invoke-Native -FilePath 'ssh' -ArgumentList ($RebootOptions + @($PiHost, 'sudo -n reboot')) -IgnoreStderr
    $deadline = (Get-Date).AddSeconds($RebootTimeoutSeconds)
    while ($true) {
        Start-Sleep -Seconds $PollSeconds
        $now = Get-PiBootId
        if ($now -and $now -ne $before) {
            Write-Host '    the Pi is back'
            return
        }
        if ((Get-Date) -gt $deadline) {
            Stop-WithError ('the Pi did not come back within ' + $RebootTimeoutSeconds + ' s; check it, then re-run this script')
        }
        Write-Host '    waiting for the Pi...'
    }
}

if ($Snapshot -and $Force) { Stop-WithError '-Snapshot deploys uncommitted changes and -Force leaves them out; give one of the two' }

foreach ($tool in @('git', 'ssh', 'scp')) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { Stop-WithError ($tool + ' is not on PATH') }
}

# the same checks deploy.ps1 makes, done before the Pi is touched
if (-not $Snapshot) {
    $dirty = @(Invoke-Native -FilePath 'git' -ArgumentList @('-C', $RepoRoot, 'status', '--porcelain'))
    if ($LASTEXITCODE -ne 0) { Stop-WithError 'git status failed' }
    if ($dirty.Count -gt 0 -and -not $Force) {
        Invoke-Native -FilePath 'git' -ArgumentList @('-C', $RepoRoot, 'status', '--short')
        Stop-WithError 'the working tree has uncommitted changes (above); commit them, or re-run with -Snapshot to deploy them as they are'
    }
    foreach ($path in @('install.sh', 'deploy/post-receive-main')) {
        $null = Invoke-Native -FilePath 'git' -ArgumentList @('-C', $RepoRoot, 'cat-file', '-e', ('refs/heads/main:' + $path)) -IgnoreStderr
        if ($LASTEXITCODE -ne 0) { Stop-WithError ('main has no committed ' + $path + ', so the first push could not deploy; commit it, or re-run with -Snapshot') }
    }
}

# install.sh must reach the Pi with LF line endings
$installSh = Join-Path $RepoRoot 'install.sh'
if (-not (Test-Path -LiteralPath $installSh)) { Stop-WithError ('missing ' + $installSh) }
if ([System.IO.File]::ReadAllBytes($installSh) -contains [byte]13) {
    Stop-WithError 'install.sh has CRLF line endings; run "git add --renormalize ." and check .gitattributes'
}

Write-Host ('==> checking ssh ' + $PiHost)
$null = Invoke-Pi 'true'
if ($LASTEXITCODE -ne 0) { Stop-WithError ('cannot reach the Pi with "ssh ' + $PiHost + '" (key auth, non-interactive)') }

Write-Host '==> copying install.sh to the Pi (~/install.sh)'
# relative local path: an absolute "C:\..." would look like host "C" to scp
Push-Location -LiteralPath $RepoRoot
try {
    Invoke-Native -FilePath 'scp' -ArgumentList ($SshOptions + @('-q', 'install.sh', ($PiHost + ':install.sh')))
    $scpExit = $LASTEXITCODE
}
finally {
    Pop-Location
}
if ($scpExit -ne 0) { Stop-WithError ('scp install.sh failed (exit ' + $scpExit + ')') }

Write-Host '==> running install.sh --bootstrap on the Pi'
Invoke-Pi 'bash ~/install.sh --bootstrap'
$bootstrapExit = $LASTEXITCODE
if ($bootstrapExit -eq $RebootRequiredExit) {
    Restart-Pi
}
elseif ($bootstrapExit -ne 0) {
    Stop-WithError ('install.sh --bootstrap failed on the Pi (exit ' + $bootstrapExit + ')')
}

Write-Host '==> checking /dev/spidev0.0'
Invoke-Pi 'ls -l /dev/spidev0.0'
if ($LASTEXITCODE -ne 0) { Stop-WithError '/dev/spidev0.0 is missing on the Pi; SPI is not enabled (see docs\DEPLOY.md)' }

$deployArgs = @{ SkipFrame = $true }
if ($Snapshot) { $deployArgs['Snapshot'] = $true }
if ($Force) { $deployArgs['Force'] = $true }
Write-Host ('==> first deploy: deploy.ps1 -' + (@($deployArgs.Keys | Sort-Object) -join ' -'))
& (Join-Path $RepoRoot 'deploy.ps1') @deployArgs
if ($LASTEXITCODE -ne 0) { Stop-WithError 'deploy.ps1 failed (see above)' }

Write-Host '==> service state'
$state = (Invoke-Pi 'systemctl is-active inkframe') -join ''
Write-Host ('    inkframe: ' + $state)
if ($state -ne 'active') { Stop-WithError 'the inkframe service is not active; see: ssh inkframe "journalctl -u inkframe -n 100"' }

Write-Host '==> GET /api/status on the Pi'
$statusRaw = (Invoke-Pi 'curl -s -m 10 http://127.0.0.1/api/status') -join "`n"
if ($LASTEXITCODE -ne 0 -or -not $statusRaw) {
    Write-Warning 'GET /api/status failed on the Pi'
}
else {
    Write-StatusJson -Raw $statusRaw
}

Write-Host 'bootstrap complete: http://inkframe.local/'
exit 0
