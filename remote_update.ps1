# [v2.2] Remote Update Handover Script - ENGLISH PATH VERSION
# This version uses the "_op" suffix and is 100% ASCII.

$ErrorActionPreference = "Continue"

# "_op" suffix for English naming convention
$suffix = "_op"
$OpPath = $PSScriptRoot

# Dynamic RepoPath derivation
if ($OpPath -like "*$suffix") {
    $RepoPath = $OpPath.Substring(0, $OpPath.Length - $suffix.Length)
} else {
    # Transition fallback: if we are still using the Korean name
    $RepoPath = $OpPath -replace "_\w+$", "" # Generic suffix removal
}

$LogFile = Join-Path $OpPath "remote_update_log.txt"

function Log-Message($msg) {
    if ($null -eq $msg) { $msg = "[null]" }
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    try {
        "$timestamp - $msg" | Out-File -FilePath $LogFile -Append -Encoding UTF8 -ErrorAction SilentlyContinue
    } catch {}
    Write-Host $msg -ForegroundColor White
}

Log-Message "--------------------------------------------------"
Log-Message "[REMOTE] Remote Update Sequence Started (v2.2)"

# 1. Wait for file locks to release
Log-Message "[1/4] Waiting for application to close (5s)..."
Start-Sleep -Seconds 5

try {
    # 2. Pull from Git
    Log-Message "[2/4] Pulling latest code from Git in $RepoPath"
    if (Test-Path "$RepoPath\.git") {
        Set-Location $RepoPath
        git config --global --add safe.directory $RepoPath 2>&1 | Out-Null
        git pull 2>&1 | Tee-Object -FilePath $LogFile -Append
    } else {
        Log-Message "Error: .git folder not found in $RepoPath"
    }
    
    # 3. Deploy to Operation Folder
    Log-Message "[3/4] Running Deployment Sync (deploy.ps1)..."
    $DeployScript = Join-Path $RepoPath "deploy.ps1"
    if (Test-Path $DeployScript) {
        Set-Location $RepoPath
        powershell.exe -ExecutionPolicy Bypass -File "$DeployScript" 2>&1 | Tee-Object -FilePath $LogFile -Append
    } else {
        Log-Message "Error: deploy.ps1 not found at $DeployScript"
    }
    
    # 4. Restart Application
    Log-Message "[4/4] Restarting Application in $OpPath"
    if (Test-Path $OpPath) {
        Set-Location $OpPath
        $AppBatch = Join-Path $OpPath "run_app.bat"
        if (Test-Path $AppBatch) {
            Start-Process -FilePath "cmd.exe" -ArgumentList "/c run_app.bat --auto-start" -WorkingDirectory $OpPath
            Log-Message "[SUCCESS] Remote Update Sequence Completed!"
        } else {
            Log-Message "Error: run_app.bat not found at $AppBatch"
        }
    } else {
        Log-Message "Error: Operation path not found: $OpPath"
    }

} catch {
    Log-Message "EXCEPTION during update: $_"
}

Log-Message "--------------------------------------------------"
Start-Sleep -Seconds 2
