# push_to_github.ps1
# Запускати з PowerShell: .\push_to_github.ps1
# Або правою кнопкою -> "Run with PowerShell"

Set-Location $PSScriptRoot

Write-Host ""
Write-Host "================================================" -ForegroundColor Cyan
Write-Host "  NEXUS — Push to GitHub" -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan
Write-Host ""

# Remove lock files
Remove-Item -Force ".git\index.lock" -ErrorAction SilentlyContinue
Remove-Item -Force ".git\HEAD.lock" -ErrorAction SilentlyContinue
Remove-Item -Force ".git\MERGE_HEAD" -ErrorAction SilentlyContinue
Write-Host "[1/3] Lock files cleared" -ForegroundColor Green

# Check latest commit
$logOutput = git log --oneline -1 2>&1
Write-Host "[2/3] Latest commit: $logOutput" -ForegroundColor Yellow
Write-Host ""

# Get GitHub token
Write-Host "Enter your GitHub Personal Access Token" -ForegroundColor White
Write-Host "(Settings -> Developer Settings -> Personal Access Tokens -> Tokens (classic))" -ForegroundColor Gray
Write-Host ""
$token = Read-Host -AsSecureString "GitHub Token"
$tokenPlain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
    [Runtime.InteropServices.Marshal]::SecureStringToBSTR($token)
)

if ($tokenPlain.Length -lt 10) {
    Write-Host "Token too short, aborting." -ForegroundColor Red
    exit 1
}

# Build authenticated URL
$remoteUrl = "https://${tokenPlain}@github.com/nexus-nikita/nexus-ai.git"

Write-Host ""
Write-Host "[3/3] Pushing to deploy14..." -ForegroundColor Yellow
$result = git push $remoteUrl deploy14 2>&1
Write-Host $result

if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "================================================" -ForegroundColor Green
    Write-Host "  PUSHED! Render deploy ~2 min." -ForegroundColor Green
    Write-Host "  https://nexus-ai-48sm.onrender.com/healthz" -ForegroundColor Green
    Write-Host "================================================" -ForegroundColor Green

    Write-Host ""
    Write-Host "Запустити keepalive (щоб Render не засипав)? [y/N]" -ForegroundColor Cyan
    $ans = Read-Host
    if ($ans -match "^[yY]") {
        Write-Host "Запускаю keepalive.py у фоні..." -ForegroundColor Green
        Start-Process python -ArgumentList "keepalive.py" -WindowStyle Normal
    }
} else {
    Write-Host ""
    Write-Host "Push failed. Check your token has 'repo' scope." -ForegroundColor Red
}

Write-Host ""
Read-Host "Press Enter to close"
