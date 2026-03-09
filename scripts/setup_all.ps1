# x-agent Full Setup Script
# Usage: Right-click -> Run with PowerShell
#   OR: powershell -ExecutionPolicy Bypass -File scripts\setup_all.ps1

Write-Host "=== x-agent Setup ===" -ForegroundColor Cyan

# 0. Navigate to project root
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent $scriptDir
Set-Location $projectRoot
Write-Host "[OK] Project root: $projectRoot" -ForegroundColor Green

# 1. Git pull latest
Write-Host "`n--- Step 1: Git Pull ---" -ForegroundColor Yellow
git pull origin main 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "[WARN] git pull failed - continuing with local version" -ForegroundColor Red
}

# 2. Install Python dependencies
Write-Host "`n--- Step 2: Install Dependencies ---" -ForegroundColor Yellow
pip install playwright anthropic pyyaml 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] pip install failed" -ForegroundColor Red
    Write-Host "Make sure Python is installed and in PATH" -ForegroundColor Red
    pause
    exit 1
}
Write-Host "[OK] Python dependencies installed" -ForegroundColor Green

# 3. Install Playwright Chromium
Write-Host "`n--- Step 3: Install Playwright Chromium ---" -ForegroundColor Yellow
python -m playwright install chromium 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] Playwright chromium install failed" -ForegroundColor Red
    pause
    exit 1
}
Write-Host "[OK] Playwright Chromium installed" -ForegroundColor Green

# 4. Check ANTHROPIC_API_KEY
Write-Host "`n--- Step 4: Check API Key ---" -ForegroundColor Yellow
if ([string]::IsNullOrEmpty($env:ANTHROPIC_API_KEY)) {
    Write-Host "[WARN] ANTHROPIC_API_KEY not set!" -ForegroundColor Red
    Write-Host "Set it with: `$env:ANTHROPIC_API_KEY = 'sk-ant-...'" -ForegroundColor Yellow
    Write-Host "Or set it permanently: [System.Environment]::SetEnvironmentVariable('ANTHROPIC_API_KEY','sk-ant-...','User')" -ForegroundColor Yellow
} else {
    $keyPreview = $env:ANTHROPIC_API_KEY.Substring(0, [Math]::Min(12, $env:ANTHROPIC_API_KEY.Length)) + "..."
    Write-Host "[OK] ANTHROPIC_API_KEY set ($keyPreview)" -ForegroundColor Green
}

# 5. Extract cookies from Chrome
Write-Host "`n--- Step 5: Cookie Extraction ---" -ForegroundColor Yellow
$cookieFile = Join-Path $projectRoot "data\nagi_x_cookies.json"
if (Test-Path $cookieFile) {
    Write-Host "[OK] Cookie file already exists: $cookieFile" -ForegroundColor Green
    Write-Host "     Delete it and re-run to re-extract" -ForegroundColor Gray
} else {
    Write-Host "Auto-detecting browser and extracting cookies..." -ForegroundColor Cyan
    Write-Host "[NOTE] This will close Chrome temporarily!" -ForegroundColor Yellow
    $response = Read-Host "Continue? (y/n)"
    if ($response -eq 'y') {
        python scripts/setup_cookies.py --auto --cookie-file "data/nagi_x_cookies.json" 2>&1
        if (Test-Path $cookieFile) {
            Write-Host "[OK] Cookies extracted!" -ForegroundColor Green
        } else {
            Write-Host "[WARN] Auto-detect failed. Trying manual login..." -ForegroundColor Yellow
            Write-Host "A browser window will open. Log into X, then press Enter." -ForegroundColor Cyan
            python scripts/setup_cookies.py --manual --cookie-file "data/nagi_x_cookies.json" 2>&1
            if (Test-Path $cookieFile) {
                Write-Host "[OK] Cookies extracted via manual login!" -ForegroundColor Green
            } else {
                Write-Host "[ERROR] Cookie extraction failed." -ForegroundColor Red
                Write-Host "  Try: python scripts/setup_cookies.py --manual --cookie-file data/nagi_x_cookies.json" -ForegroundColor Yellow
            }
        }
    } else {
        Write-Host "[SKIP] Cookie extraction skipped" -ForegroundColor Yellow
    }
}

# 6. Enable SSH (optional, for remote management)
Write-Host "`n--- Step 6: Enable SSH (optional) ---" -ForegroundColor Yellow
$sshdService = Get-Service -Name sshd -ErrorAction SilentlyContinue
if ($sshdService) {
    Write-Host "[OK] SSH already installed (Status: $($sshdService.Status))" -ForegroundColor Green
    if ($sshdService.Status -ne 'Running') {
        Start-Service sshd -ErrorAction SilentlyContinue
        Set-Service -Name sshd -StartupType 'Automatic' -ErrorAction SilentlyContinue
        Write-Host "[OK] SSH service started" -ForegroundColor Green
    }
} else {
    Write-Host "Installing OpenSSH Server..." -ForegroundColor Cyan
    try {
        Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 -ErrorAction Stop
        Start-Service sshd
        Set-Service -Name sshd -StartupType 'Automatic'
        Write-Host "[OK] SSH installed and started" -ForegroundColor Green
    } catch {
        Write-Host "[WARN] SSH install failed: $_" -ForegroundColor Red
        Write-Host "       This is optional - x-agent works without SSH" -ForegroundColor Gray
    }
}

# 7. Dry run test
Write-Host "`n--- Step 7: Dry Run Test ---" -ForegroundColor Yellow
Write-Host "Running: python run.py --dry-run" -ForegroundColor Cyan
Write-Host "Press Ctrl+C to stop after a few seconds`n" -ForegroundColor Gray
python run.py --config configs/nagi.yaml --dry-run 2>&1

Write-Host "`n=== Setup Complete ===" -ForegroundColor Cyan
pause
