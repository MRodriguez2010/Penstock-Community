param([switch]$Verify)
$ErrorActionPreference = "Stop"
if ($Verify) {
    $installers = @(Get-ChildItem desktop/src-tauri/target/release/bundle/nsis/*.exe)
    if ($installers.Count -eq 0) { throw "Windows installer missing" }
    foreach ($installer in $installers) {
        $signature = Get-AuthenticodeSignature $installer.FullName
        if ($signature.Status -ne "Valid" -or $signature.SignerCertificate.Thumbprint -ne $env:WINDOWS_CERT_THUMBPRINT -or !$signature.TimeStamperCertificate) {
            throw "Installer publisher or timestamp verification failed."
        }
    }
    return
}
if (!$env:WINDOWS_PFX_BASE64 -or !$env:WINDOWS_PFX_PASSWORD -or !$env:WINDOWS_TIMESTAMP_URL) {
    throw "Supply the signing certificate, password, and publisher timestamp URL."
}
$url = [uri]$env:WINDOWS_TIMESTAMP_URL
if ($url.Scheme -notin @("http","https") -or !$url.Host) { throw "Invalid timestamp URL." }
$pfx = Join-Path $env:RUNNER_TEMP "penstock-publisher.pfx"
try {
    [IO.File]::WriteAllBytes($pfx, [Convert]::FromBase64String($env:WINDOWS_PFX_BASE64))
    $password = ConvertTo-SecureString $env:WINDOWS_PFX_PASSWORD -AsPlainText -Force
    $cert = Import-PfxCertificate -FilePath $pfx -CertStoreLocation Cert:\CurrentUser\My -Password $password | Where-Object HasPrivateKey | Select-Object -First 1
    if (!$cert) { throw "No signing identity was imported." }
    "WINDOWS_CERT_THUMBPRINT=$($cert.Thumbprint)" | Out-File $env:GITHUB_ENV -Encoding utf8 -Append
    $override = @{bundle=@{windows=@{certificateThumbprint=$cert.Thumbprint;digestAlgorithm="sha256";timestampUrl=$url.AbsoluteUri;tsp=$true}}}
    $override | ConvertTo-Json -Depth 4 | Set-Content -Encoding utf8 desktop/src-tauri/tauri.windows.conf.json
} finally {
    if (Test-Path $pfx) { Remove-Item -LiteralPath $pfx }
}
