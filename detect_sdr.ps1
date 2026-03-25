# Search for devices with the common RTL2832U hardware ID
$sdr = Get-PnpDevice | Where-Object { $_.InstanceId -match "USB\\VID_0BDA&PID_283[28]" }

if ($sdr) {
    Write-Host "✅ RTL-SDR Detected!" -ForegroundColor Green
    $sdr | Select-Object FriendlyName, Status, InstanceId | Format-Table

    Write-Host "Check WinUSB driver assignment:" -ForegroundColor Green
    Get-PnpDevice | Where-Object { $_.InstanceId -match "PID_2838" } | Select-Object FriendlyName, Status, Service
} else {
    Write-Host "❌ No RTL-SDR found." -ForegroundColor Red
}
