# PP Tools installer
# Downloads the latest PP Tools pyRevit extension from GitHub and installs
# it for the current user. No admin rights required. pyRevit must already
# be installed (https://github.com/pyrevitlabs/pyRevit/releases).

$ErrorActionPreference = "Stop"

# ------------------------------------------------------------- settings --
$GithubRepo = "Royroyan09/pp-tools"
$Branch = "main"
# --------------------------------------------------------------------------

if ($GithubRepo -like "*your-github-username*") {
    Write-Host "ERROR: installer not configured. Edit `$GithubRepo at the top of install.ps1." -ForegroundColor Red
    exit 1
}

$PyRevitDir = Join-Path $env:APPDATA "pyRevit"
if (-not (Test-Path $PyRevitDir)) {
    Write-Host "ERROR: pyRevit does not appear to be installed for this user." -ForegroundColor Red
    Write-Host "Install it first: https://github.com/pyrevitlabs/pyRevit/releases"
    exit 1
}

$ExtensionsDir = Join-Path $PyRevitDir "Extensions"
$TargetDir = Join-Path $ExtensionsDir "PPTools.extension"
New-Item -ItemType Directory -Force $ExtensionsDir | Out-Null

Write-Host "Downloading PP Tools from github.com/$GithubRepo ..."
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ZipPath = Join-Path $env:TEMP "pptools_install.zip"
$StagingDir = Join-Path $env:TEMP "pptools_install"
Invoke-WebRequest "https://github.com/$GithubRepo/archive/refs/heads/$Branch.zip" -OutFile $ZipPath

if (Test-Path $StagingDir) { Remove-Item -Recurse -Force $StagingDir }
Expand-Archive $ZipPath -DestinationPath $StagingDir -Force

# the GitHub zip wraps everything in a single "<repo>-<branch>" folder
$InnerRoot = Get-ChildItem $StagingDir -Directory | Select-Object -First 1
if ($null -eq $InnerRoot -or -not (Test-Path (Join-Path $InnerRoot.FullName "VERSION"))) {
    Write-Host "ERROR: downloaded package does not look like a PP Tools release." -ForegroundColor Red
    exit 1
}

Write-Host "Installing to $TargetDir ..."
if (Test-Path $TargetDir) { Remove-Item -Recurse -Force $TargetDir }
Copy-Item $InnerRoot.FullName $TargetDir -Recurse

Remove-Item $ZipPath -Force
Remove-Item -Recurse -Force $StagingDir

$Version = (Get-Content (Join-Path $TargetDir "VERSION") -TotalCount 1).Trim()
Write-Host ""
Write-Host "PP Tools $Version installed successfully." -ForegroundColor Green
Write-Host "Start Revit (or click pyRevit > Reload) and the 'PP Tools' tab will appear."
