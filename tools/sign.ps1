<#
.SYNOPSIS
    Give the built executable a publisher, so Windows stops calling it unknown.

.DESCRIPTION
    "Unknown Publisher" appears because the executable has no Authenticode
    signature. Windows reads the publisher name from the signing certificate, so
    there is no setting that simply declares one - the file has to be signed.

    This creates a self-signed code-signing certificate with the requested
    subject (in the current user's certificate store, never the machine store)
    and signs the executable with it.

    What that achieves:
      * The file gains a digital signature naming the publisher, which shows on
        the Properties -> Digital Signatures tab, and in SmartScreen and UAC
        prompts in place of "Unknown Publisher".
      * On this machine, adding the certificate to Trusted Root and Trusted
        Publishers makes those prompts stop warning altogether. That is what
        -Trust does, and it needs administrator rights.
      * On other machines the signature is present but untrusted, so the name is
        shown and the warning remains until the certificate is trusted there.

    A self-signed certificate can never earn "verified publisher" reputation the
    way a commercial certificate does; it identifies the publisher, it does not
    vouch for them. That requires a certificate from a public authority.

.EXAMPLE
    .\sign.ps1 -Path dist\PDFConsolidator\PDFConsolidator.exe -Trust
#>
param(
    [Parameter(Mandatory = $true)][string]$Path,
    [string]$Publisher = "Xiaoyu-Ben-Wang",
    [switch]$Trust
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $Path)) {
    throw "Not found: $Path"
}

$subject = "CN=$Publisher"

Write-Host "Publisher : $Publisher"
Write-Host "Target    : $Path"

# ---- find or create the certificate -----------------------------------------
$cert = Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert -ErrorAction SilentlyContinue |
        Where-Object { $_.Subject -eq $subject } |
        Sort-Object NotAfter -Descending |
        Select-Object -First 1

if ($cert) {
    Write-Host "Certificate: reusing existing ($($cert.Thumbprint))"
} else {
    Write-Host "Certificate: creating a self-signed code-signing certificate ..."
    $cert = New-SelfSignedCertificate `
        -Type CodeSigningCert `
        -Subject $subject `
        -CertStoreLocation Cert:\CurrentUser\My `
        -KeyUsage DigitalSignature `
        -KeyAlgorithm RSA `
        -KeyLength 2048 `
        -NotAfter (Get-Date).AddYears(10) `
        -FriendlyName "PDF Consolidator publisher"
    Write-Host "Certificate: created ($($cert.Thumbprint))"
}

# ---- sign --------------------------------------------------------------------
$result = Set-AuthenticodeSignature -FilePath $Path -Certificate $cert `
    -HashAlgorithm SHA256

if ($result.Status -ne "Valid") {
    Write-Warning "Signature status is $($result.Status): $($result.StatusMessage)"
} else {
    Write-Host "Signed    : $($result.Status)"
}

# ---- trust (optional, needs administrator) -----------------------------------
if ($Trust) {
    $cerPath = Join-Path ([System.IO.Path]::GetDirectoryName($Path)) "pdfconsolidator-publisher.cer"
    Export-Certificate -Cert $cert -FilePath $cerPath -Force | Out-Null
    Write-Host "Exported  : $cerPath"

    foreach ($store in @("Root", "TrustedPublisher")) {
        try {
            Import-Certificate -FilePath $cerPath -CertStoreLocation "Cert:\LocalMachine\$store" | Out-Null
            Write-Host "Trusted   : LocalMachine\$store"
        } catch {
            Write-Warning "Could not add to LocalMachine\$store - run as administrator. ($($_.Exception.Message))"
        }
    }
    Write-Host ""
    Write-Host "Other machines: copy pdfconsolidator-publisher.cer to them and install it"
    Write-Host "into 'Trusted Root Certification Authorities' and 'Trusted Publishers'."
}

Write-Host ""
Write-Host "Verify with:"
Write-Host "  Get-AuthenticodeSignature '$Path' | Format-List Status, SignerCertificate"
