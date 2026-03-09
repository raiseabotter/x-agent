# Enable SSH on Windows — Self-elevating script
# Double-click or: powershell -ExecutionPolicy Bypass -File scripts\enable_ssh.ps1

# --- Self-elevate to admin if needed ---
if (-NOT ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Requesting administrator privileges..." -ForegroundColor Yellow
    $scriptPath = $MyInvocation.MyCommand.Path
    Start-Process powershell.exe -ArgumentList "-ExecutionPolicy Bypass -File `"$scriptPath`"" -Verb RunAs
    exit
}

Write-Host "=== SSH Server Setup ===" -ForegroundColor Cyan
Write-Host "Running as Administrator" -ForegroundColor Green

# 1. Install OpenSSH Server
Write-Host "`n--- Installing OpenSSH Server ---" -ForegroundColor Yellow
$sshCapability = Get-WindowsCapability -Online | Where-Object Name -like 'OpenSSH.Server*'
if ($sshCapability.State -eq 'Installed') {
    Write-Host "[OK] OpenSSH Server already installed" -ForegroundColor Green
} else {
    try {
        Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 -ErrorAction Stop
        Write-Host "[OK] OpenSSH Server installed" -ForegroundColor Green
    } catch {
        Write-Host "[ERROR] Failed to install OpenSSH Server: $_" -ForegroundColor Red
        Write-Host "Try: Settings > Apps > Optional Features > Add > OpenSSH Server" -ForegroundColor Yellow
        pause
        exit 1
    }
}

# 2. Start and enable SSH service
Write-Host "`n--- Starting SSH Service ---" -ForegroundColor Yellow
try {
    Start-Service sshd -ErrorAction Stop
    Set-Service -Name sshd -StartupType 'Automatic' -ErrorAction Stop
    Write-Host "[OK] SSH service started and set to auto-start" -ForegroundColor Green
} catch {
    Write-Host "[ERROR] Failed to start SSH service: $_" -ForegroundColor Red
    pause
    exit 1
}

# 3. Configure firewall
Write-Host "`n--- Configuring Firewall ---" -ForegroundColor Yellow
$firewallRule = Get-NetFirewallRule -Name 'sshd' -ErrorAction SilentlyContinue
if ($firewallRule) {
    Write-Host "[OK] Firewall rule already exists" -ForegroundColor Green
} else {
    try {
        New-NetFirewallRule -Name 'sshd' -DisplayName 'OpenSSH Server (sshd)' -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 -ErrorAction Stop
        Write-Host "[OK] Firewall rule created (port 22 inbound)" -ForegroundColor Green
    } catch {
        Write-Host "[WARN] Could not create firewall rule: $_" -ForegroundColor Yellow
        Write-Host "SSH may work on Tailscale but not on other networks" -ForegroundColor Gray
    }
}

# 4. Set default shell to PowerShell
Write-Host "`n--- Setting Default Shell ---" -ForegroundColor Yellow
try {
    New-ItemProperty -Path "HKLM:\SOFTWARE\OpenSSH" -Name DefaultShell -Value "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" -PropertyType String -Force -ErrorAction Stop | Out-Null
    Write-Host "[OK] Default SSH shell set to PowerShell" -ForegroundColor Green
} catch {
    Write-Host "[WARN] Could not set default shell: $_" -ForegroundColor Yellow
}

# 5. Verify
Write-Host "`n--- Verification ---" -ForegroundColor Yellow
$service = Get-Service sshd
Write-Host "SSH Service Status: $($service.Status)" -ForegroundColor $(if ($service.Status -eq 'Running') { 'Green' } else { 'Red' })
Write-Host "SSH Service Startup: $($service.StartType)" -ForegroundColor $(if ($service.StartType -eq 'Automatic') { 'Green' } else { 'Yellow' })

# Show connection info
$tailscaleIP = (tailscale ip -4 2>$null)
$hostname = hostname
$username = $env:USERNAME
Write-Host "`n=== Connection Info ===" -ForegroundColor Cyan
Write-Host "Hostname:     $hostname" -ForegroundColor White
Write-Host "Username:     $username" -ForegroundColor White
if ($tailscaleIP) {
    Write-Host "Tailscale IP: $tailscaleIP" -ForegroundColor White
    Write-Host "`nConnect with: ssh $username@$tailscaleIP" -ForegroundColor Green
} else {
    Write-Host "Tailscale:    not detected" -ForegroundColor Yellow
}

Write-Host "`n[DONE] SSH is ready." -ForegroundColor Green
pause
