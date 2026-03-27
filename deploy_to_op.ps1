# deploy_to_op.ps1
# [v1.1] UTF-8 Encoding-safe Deployment Script

$Source = Get-Location
# 운영 폴더 이름을 직접 쓰는 대신, 상위 폴더 기준의 상대 경로를 사용하거나 English Alias를 사용합니다.
$TargetName = "바이브_운영"
$Target = Join-Path (Split-Path $Source) $TargetName

$ExcludeList = @("trading_data.db", "virtual_db.json", "deploy_to_op.ps1")

Write-Host "--------------------------------------------------" -ForegroundColor Cyan
Write-Host "🚀 [Operation Deployment] : $Source -> $Target" -ForegroundColor Cyan
Write-Host "--------------------------------------------------" -ForegroundColor Cyan

if (-not (Test-Path $Target)) {
    Write-Host "📂 Creating Operation Folder..."
    New-Item -ItemType Directory -Path $Target -Force | Out-Null
}

$Folders = @("core", "gui", "strategy", "utils")
foreach ($f in $Folders) {
    if (Test-Path "$Source\$f") {
        Write-Host "📦 Copying folder: $f"
        $destPath = Join-Path $Target $f
        if (-not (Test-Path $destPath)) { New-Item -ItemType Directory -Path $destPath -Force | Out-Null }
        Copy-Item -Path "$Source\$f\*" -Destination $destPath -Recurse -Force
    }
}

Write-Host "📄 Syncing root files (*.py, *.json, *.csv)..."
Get-ChildItem -Path $Source -File | Where-Object { 
    $_.Extension -match "\.(py|json|csv|md)$" -and $_.Name -notin $ExcludeList 
} | ForEach-Object {
    Copy-Item -Path $_.FullName -Destination $Target -Force
}

$vdb = "virtual_db.json"
if (Test-Path "$Source\$vdb") {
    if (-not (Test-Path "$Target\$vdb")) {
        Write-Host "🆕 Initializing virtual trade data..."
        Copy-Item -Path "$Source\$vdb" -Destination $Target -Force
    } else {
        Write-Host "🛡️ Existing trade data preserved." -ForegroundColor Yellow
    }
}

Write-Host "`n✅ Deployment Complete!" -ForegroundColor Green
Write-Host "💡 Run Get_Price_GUI.py in $Target" -ForegroundColor Green
Write-Host "--------------------------------------------------" -ForegroundColor Cyan
