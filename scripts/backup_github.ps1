#requires -Version 5.1
<#
.SYNOPSIS
  GitHub 원격 백업 — realtime_session_logs 를 항상 포함한 뒤 커밋·푸시합니다.
.DESCRIPTION
  - 세션 로그(일별 HTML·session_events.jsonl 등)는 먼저 스테이징합니다.
  - 이후 저장소 전체 변경분을 스테이징합니다(.gitignore 제외).
.PARAMETER Message
  커밋 메시지. 생략 시 타임스탬프 기반 chore 메시지를 사용합니다.
.PARAMETER DryRun
  스테이징·상태만 출력하고 커밋/푸시는 하지 않습니다.
#>
param(
    [string]$Message = "",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RepoRoot

if (-not (Test-Path (Join-Path $RepoRoot ".git"))) {
    throw "Git 저장소 루트를 찾을 수 없습니다: $RepoRoot"
}

Write-Host "== [백업] 저장소: $RepoRoot"

# 세션 로그는 백업 시 항상 포함 (신규 일자 폴더 포함)
if (Test-Path (Join-Path $RepoRoot "realtime_session_logs")) {
    Write-Host "-> realtime_session_logs/ 스테이징"
    git add -A -- "realtime_session_logs/"
    if ($LASTEXITCODE -ne 0) { throw "git add realtime_session_logs 실패" }
} else {
    Write-Host "-> realtime_session_logs 없음 (건너뜀)"
}

Write-Host "-> 나머지 변경분 스테이징 (.gitignore 준수)"
git add -A
if ($LASTEXITCODE -ne 0) { throw "git add -A 실패" }

git status --short

if ($DryRun) {
    Write-Host "[DryRun] 커밋/푸시 생략"
    exit 0
}

git diff --cached --quiet
if ($LASTEXITCODE -eq 0) {
    Write-Host "커밋할 변경이 없습니다."
    exit 0
}

if (-not $Message) {
    $Message = "chore: backup $(Get-Date -Format 'yyyy-MM-dd HH:mm') (세션 로그 포함)"
}

git commit -m $Message
if ($LASTEXITCODE -ne 0) { throw "git commit 실패" }

$branch = (git rev-parse --abbrev-ref HEAD).Trim()
Write-Host "-> push origin $branch"
git push origin $branch
if ($LASTEXITCODE -ne 0) { throw "git push 실패" }

Write-Host "완료."
