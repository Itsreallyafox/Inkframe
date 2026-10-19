<#
.SYNOPSIS
    Deploy to the Inkframe Pi with git push, then show its status.

.DESCRIPTION
    1. Makes sure the git remote "pi" (inkframe:/opt/inkframe/repo.git) exists.
    2. Picks the commit to deploy: the local branch (default main), refusing
       uncommitted or untracked changes unless -Force; or, with -Snapshot, a
       snapshot commit of the working tree as it is.
    3. Refuses a commit without install.sh and deploy/post-receive-main, which
       the Pi's hook needs to deploy anything.
    4. git push pi <commit>:main. The Pi's post-receive hook checks it out,
       updates/restarts the service and runs a health check; its output shows
       up as "remote:" lines. Pushes are fast-forward only, except that a
       snapshot on the Pi is always replaced (force push).
    5. Reads the result the hook recorded in /opt/inkframe/last-deploy, because
       git push succeeds even when the hook fails.
    6. Prints GET /api/status from the Pi.
    7. Copies /opt/inkframe/data/last_frame.png to .\data\pi_last_frame.png
       (unless -SkipFrame).

    Windows PowerShell 5.1 compatible. Runbook: docs\DEPLOY.md.
    Run it as: powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy.ps1

.PARAMETER Force
    Deploy the branch even if the working tree has uncommitted or untracked
    changes (they are not deployed: only committed work is pushed).

.PARAMETER Snapshot
    Deploy the working tree as it is, uncommitted and untracked (not ignored)
    files included, without committing. A commit object is built in a
    temporary index (parent HEAD, trailer "Inkframe-Snapshot: <HEAD sha>") and
    pushed to the Pi's main; the local index, HEAD, branches and git config
    are not touched. The next deploy replaces the snapshot on the Pi. Cannot
    be combined with -Force or -Branch.

.PARAMETER SkipFrame
    Do not fetch last_frame.png from the Pi.

.PARAMETER Branch
    Local branch to deploy; it is pushed to the Pi's main. Default: main.
#>
[CmdletBinding()]
param(
    [switch]$Force,
    [switch]$Snapshot,
    [switch]$SkipFrame,
    [string]$Branch = 'main'
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$RepoRoot = $PSScriptRoot
$RemoteName = 'pi'
$RemoteUrl = 'inkframe:/opt/inkframe/repo.git'
$PiHost = 'inkframe'
$PiRepo = '/opt/inkframe/repo.git'
$SshOptions = @('-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10')
$DeployRecord = '/opt/inkframe/last-deploy'
$RemoteFrame = '/opt/inkframe/data/last_frame.png'
# files the Pi's post-receive hook runs; a commit without them cannot deploy
$RequiredFiles = @('install.sh', 'deploy/post-receive-main')
# commit-message trailer that marks a snapshot made by -Snapshot
$SnapshotTrailer = 'Inkframe-Snapshot'

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

function Invoke-Git {
    <# .SYNOPSIS Run git in the repo root; returns stdout lines, exit code in $LASTEXITCODE. #>
    param(
        [Parameter(Mandatory = $true)][string[]]$ArgumentList,
        [switch]$IgnoreStderr
    )
    Invoke-Native -FilePath 'git' -ArgumentList (@('-C', $RepoRoot) + $ArgumentList) -IgnoreStderr:$IgnoreStderr
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

function Get-Revision {
    <# .SYNOPSIS The object name a revision resolves to, or '' if it does not resolve. #>
    param([Parameter(Mandatory = $true)][string]$Revision)
    $name = (Invoke-Git @('rev-parse', '--verify', '--quiet', $Revision) -IgnoreStderr) -join ''
    if ($LASTEXITCODE -ne 0) { return '' }
    return $name.Trim()
}

function Get-PiMain {
    <#
    .SYNOPSIS
        The Pi's main as a hashtable (Sha, Tree, Parent, IsSnapshot), or $null
        if nothing has been pushed yet. Stops the script if the Pi is unreachable.
    #>
    $line = (Invoke-Git @('ls-remote', $RemoteName, 'refs/heads/main')) -join ' '
    if ($LASTEXITCODE -ne 0) { Stop-WithError ('cannot reach the Pi (git ls-remote ' + $RemoteName + ' failed); check: ssh ' + $PiHost + ' true') }
    if (-not $line.Trim()) { return $null }
    $sha = ($line.Trim() -split '\s+')[0]
    # first line "<tree>:<parents>", then the commit message
    $lines = @(Invoke-Pi ('git --git-dir=' + $PiRepo + ' log -1 --format=%T:%P%n%B ' + $sha))
    if ($LASTEXITCODE -ne 0 -or $lines.Count -lt 1) { Stop-WithError ('cannot read commit ' + $sha + ' in ' + $PiRepo + ' on the Pi') }
    $treeAndParents = @($lines[0].Trim() -split ':')
    $marker = '^' + [regex]::Escape($SnapshotTrailer) + ':'
    return @{
        Sha        = $sha
        Tree       = $treeAndParents[0]
        Parent     = @($treeAndParents[1] -split ' ')[0]
        IsSnapshot = (@($lines | Where-Object { $_ -match $marker }).Count -gt 0)
    }
}

function Get-WorkingTreeTree {
    <#
    .SYNOPSIS
        Write a tree object of the working tree as "git add --all" would stage
        it (tracked files plus untracked, not ignored, ones) and return its
        name. It is built in a temporary index, so the real index is untouched.
    #>
    param([Parameter(Mandatory = $true)][string]$HeadSha)
    $gitDir = (Invoke-Git @('rev-parse', '--absolute-git-dir')) -join ''
    if ($LASTEXITCODE -ne 0 -or -not $gitDir) { Stop-WithError 'git rev-parse --absolute-git-dir failed' }
    $tempIndex = Join-Path $gitDir.Trim() 'inkframe-snapshot.index'
    $savedIndex = $env:GIT_INDEX_FILE
    $env:GIT_INDEX_FILE = $tempIndex
    try {
        $null = Invoke-Git @('read-tree', $HeadSha)
        if ($LASTEXITCODE -ne 0) { Stop-WithError 'git read-tree HEAD into the temporary index failed' }
        $null = Invoke-Git @('add', '--all')
        if ($LASTEXITCODE -ne 0) { Stop-WithError 'git add --all into the temporary index failed' }
        $tree = (Invoke-Git @('write-tree')) -join ''
        if ($LASTEXITCODE -ne 0 -or -not $tree) { Stop-WithError 'git write-tree from the temporary index failed' }
        return $tree.Trim()
    }
    finally {
        $env:GIT_INDEX_FILE = $savedIndex
        if (Test-Path -LiteralPath $tempIndex) { Remove-Item -LiteralPath $tempIndex -Force }
    }
}

function New-SnapshotCommit {
    <# .SYNOPSIS Create the snapshot commit object for a tree (parent HEAD, on no branch); returns its name. #>
    param(
        [Parameter(Mandatory = $true)][string]$Tree,
        [Parameter(Mandatory = $true)][string]$HeadSha
    )
    $subject = 'Inkframe snapshot of the working tree on ' + $HeadSha.Substring(0, 7)
    $body = 'Made by deploy.ps1 -Snapshot from uncommitted changes. It is on no branch; the next deploy replaces it on the Pi.'
    $trailer = $SnapshotTrailer + ': ' + $HeadSha
    $sha = (Invoke-Git @('commit-tree', $Tree, '-p', $HeadSha, '-m', $subject, '-m', $body, '-m', $trailer)) -join ''
    if ($LASTEXITCODE -ne 0 -or -not $sha) { Stop-WithError 'git commit-tree failed (is user.name/user.email set?)' }
    return $sha.Trim()
}

if ($Snapshot -and $Force) { Stop-WithError '-Snapshot deploys uncommitted changes and -Force leaves them out; give one of the two' }
if ($Snapshot -and $PSBoundParameters.ContainsKey('Branch')) { Stop-WithError '-Snapshot deploys the checked-out working tree on top of HEAD; it cannot be combined with -Branch' }

# a missing tool would leave a stale $LASTEXITCODE behind, so check up front
foreach ($tool in @('git', 'ssh', 'scp')) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { Stop-WithError ($tool + ' is not on PATH') }
}

# 1. the "pi" remote
$remotes = @(Invoke-Git @('remote'))
if ($LASTEXITCODE -ne 0) { Stop-WithError ('git remote failed in ' + $RepoRoot) }
if ($remotes -contains $RemoteName) {
    $url = (Invoke-Git @('remote', 'get-url', $RemoteName)) -join ''
    if ($url -ne $RemoteUrl) {
        Write-Warning ("git remote '{0}' points at '{1}', expected '{2}'; using it as is" -f $RemoteName, $url, $RemoteUrl)
    }
}
else {
    Invoke-Git @('remote', 'add', $RemoteName, $RemoteUrl)
    if ($LASTEXITCODE -ne 0) { Stop-WithError ('could not add git remote ' + $RemoteName) }
    Write-Host ("added git remote {0} -> {1}" -f $RemoteName, $RemoteUrl)
}

# 2. the commit to deploy
$piMain = Get-PiMain
$dirty = @(Invoke-Git @('status', '--porcelain'))
if ($LASTEXITCODE -ne 0) { Stop-WithError 'git status failed' }
$isSnapshot = $false
$headSha = ''
if ($Snapshot) {
    $headSha = Get-Revision 'HEAD^{commit}'
    if (-not $headSha) { Stop-WithError '-Snapshot needs at least one commit to build on (HEAD does not resolve)' }
    if ($dirty.Count -gt 0) {
        Write-Host '==> snapshot of the working tree, including these uncommitted changes:'
        Invoke-Git @('status', '--short')
    }
    $tree = Get-WorkingTreeTree -HeadSha $headSha
    if ($tree -eq (Get-Revision ($headSha + '^{tree}'))) {
        Write-Host '==> the working tree matches HEAD; deploying HEAD itself'
        $deploySha = $headSha
    }
    elseif ($piMain -and $piMain.IsSnapshot -and $piMain.Tree -eq $tree -and $piMain.Parent -eq $headSha) {
        Write-Host '==> the Pi already has a snapshot of this working tree'
        $deploySha = $piMain.Sha
        $isSnapshot = $true
    }
    else {
        $deploySha = New-SnapshotCommit -Tree $tree -HeadSha $headSha
        $isSnapshot = $true
    }
}
else {
    if ($dirty.Count -gt 0) {
        Invoke-Git @('status', '--short')
        if ($Force) {
            Write-Warning 'uncommitted changes above are NOT deployed (-Force): only committed work is pushed'
        }
        else {
            Stop-WithError 'the working tree has uncommitted changes (above); commit them, re-run with -Snapshot to deploy them as they are, or with -Force to deploy only what is committed'
        }
    }
    $deploySha = Get-Revision ('refs/heads/' + $Branch)
    if (-not $deploySha) { Stop-WithError ("no local branch '" + $Branch + "'") }
}
$shortSha = $deploySha.Substring(0, 7)

# 3. the hook needs install.sh and deploy/post-receive-main from the commit
foreach ($path in $RequiredFiles) {
    $null = Invoke-Git @('cat-file', '-e', ($deploySha + ':' + $path)) -IgnoreStderr
    if ($LASTEXITCODE -ne 0) {
        Stop-WithError ("commit {0} has no {1}, so the Pi could not deploy it; commit the deploy files, or deploy the working tree with -Snapshot" -f $shortSha, $path)
    }
}

# 4. push (skipped when the Pi already has this commit: the hook would not run)
if ($piMain -and $piMain.Sha -eq $deploySha) {
    Write-Host ("==> the Pi's main is already at {0}; nothing to push" -f $shortSha)
}
else {
    $refspec = $deploySha + ':refs/heads/main'
    if ($piMain -and $piMain.IsSnapshot) {
        # a snapshot is on no branch, so nothing descends from it: overwrite it
        Write-Host ('==> replacing snapshot {0} on the Pi' -f $piMain.Sha.Substring(0, 7))
        $refspec = '+' + $refspec
    }
    if ($isSnapshot) {
        Write-Host ("==> git push {0} snapshot {1} (working tree on {2}) to main" -f $RemoteName, $shortSha, $headSha.Substring(0, 7))
    }
    else {
        Write-Host ("==> git push {0} {1}:main ({2})" -f $RemoteName, $Branch, $shortSha)
    }
    Invoke-Git @('push', $RemoteName, $refspec)
    if ($LASTEXITCODE -ne 0) { Stop-WithError ('git push failed (exit ' + $LASTEXITCODE + ')') }
}

# 5. what the hook recorded: "<sha> running|ok|failed <utc time>"
$record = (Invoke-Pi ('cat ' + $DeployRecord)) -join ' '
$recordExit = $LASTEXITCODE
$fields = @()
if ($record) { $fields = @($record.Trim() -split '\s+') }
$retry = ('ssh {0} "bash /opt/inkframe/app/deploy/post-receive-main 0 {1}"' -f $PiHost, $deploySha)
if ($recordExit -ne 0 -or $fields.Count -lt 2) {
    Stop-WithError ('no deploy record on the Pi (' + $DeployRecord + '); the post-receive hook did not run to completion. See the remote: lines above.')
}
if ($fields[0] -ne $deploySha) {
    Stop-WithError ('the Pi recorded a deploy of ' + $fields[0] + ', not ' + $deploySha + '; the hook did not deploy this push. See the remote: lines above. To re-run it: ' + $retry)
}
if ($fields[1] -ne 'ok') {
    Stop-WithError ('the deploy of ' + $shortSha + ' on the Pi is "' + $fields[1] + '" (' + $record.Trim() + '). See the remote: lines above; after fixing, deploy again or re-run: ' + $retry)
}

# 6. status
Write-Host '==> GET /api/status on the Pi'
$statusRaw = (Invoke-Pi 'curl -s -m 10 http://127.0.0.1/api/status') -join "`n"
if ($LASTEXITCODE -ne 0 -or -not $statusRaw) {
    Write-Warning 'GET /api/status failed on the Pi'
}
else {
    Write-StatusJson -Raw $statusRaw
}

# 7. the frame the Pi last rendered
if (-not $SkipFrame) {
    $dataDir = Join-Path $RepoRoot 'data'
    if (-not (Test-Path -LiteralPath $dataDir)) {
        $null = New-Item -ItemType Directory -Path $dataDir
    }
    # relative local path: an absolute "C:\..." would look like host "C" to scp
    Push-Location -LiteralPath $RepoRoot
    try {
        Invoke-Native -FilePath 'scp' -ArgumentList ($SshOptions + @('-q', ($PiHost + ':' + $RemoteFrame), 'data/pi_last_frame.png'))
        $scpExit = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }
    if ($scpExit -ne 0) {
        Write-Warning ('could not fetch ' + $RemoteFrame + ' (nothing rendered on the Pi yet?)')
    }
    else {
        Write-Host ('last frame: ' + (Join-Path $dataDir 'pi_last_frame.png'))
    }
}

if ($isSnapshot) {
    Write-Host ('deployed snapshot ' + $shortSha + ' of the uncommitted working tree on ' + $headSha.Substring(0, 7) + '; the next deploy replaces it')
}
else {
    Write-Host ('deployed ' + $shortSha)
}
exit 0
