# [v2.2] Stable Deployment Script - ENGLISH PATH VERSION
# This version uses the "_op" suffix and is 100% ASCII.

$suffix = "_op"
$CurrentPath = $PSScriptRoot

if ($CurrentPath -like "*$suffix") {
    $Target = $CurrentPath
    $Source = $CurrentPath.Substring(0, $CurrentPath.Length - $suffix.Length)
} else {
    $Source = $CurrentPath
    $Target = $CurrentPath + $suffix
}

Write-Host "--------------------------------------------------"
Write-Host "Source: $Source"
Write-Host "Target: $Target"

if (!(Test-Path $Source)) {
    Write-Error "Source path not found: $Source"
    return
}

Write-Host "[1/3] Ensuring Operation Directory exists..."
if (!(Test-Path $Target)) { New-Item -Path $Target -ItemType Directory }

Write-Host "[2/3] Syncing Folders using Robocopy..."
$Folders = @("core", "gui", "strategy", "utils", "reporting")
foreach ($folder in $Folders) {
    if (Test-Path "$Source\$folder") {
        robocopy "$Source\$folder" "$Target\$folder" /E /Z /R:3 /W:5 /MT:8 /NJH /NJS /NDL /NC /NS /NP
    }
}

Write-Host "[3/3] Syncing Root Resource Files..."
$Extensions = @("*.py", "*.json", "*.csv", "*.md", "*.bat", "*.ps1")
foreach ($ext in $Extensions) {
    if (Test-Path "$Source\$ext") {
        Get-ChildItem -Path "$Source\$ext" | ForEach-Object {
            $fname = $_.Name
            if ($fname -ieq "trading_data.db") { return }
            if ($fname -ieq "virtual_db.json" -and (Test-Path "$Target\$fname")) { return }
            
            $destFile = Join-Path $Target $fname
            if ($_.FullName -ne $destFile) {
                Copy-Item $_.FullName -Destination $Target -Force
            }
        }
    }
}

Write-Host ""
Write-Host "[SUCCESS] Deployment Complete!"
Write-Host "--------------------------------------------------"
