<#
.SYNOPSIS
  Build the BabyCare AI Android APK locally (no paid services, no cloud build).

.DESCRIPTION
  1. Regenerates mobile/android with `npx expo prebuild` for the chosen environment (unless -SkipPrebuild).
  2. Mirrors mobile/ to a short real path (default C:\bc\m) because the native CMake/ninja step breaks on Windows'
     260-character path limit when the project lives in a deep folder. A junction does not help: Gradle resolves it.
  3. Runs Gradle there and copies the APK to android-builds/.

  The release variant bundles the JavaScript so the APK runs on its own. Without a release keystore it is signed with
  the standard Android debug keystore, which is fine for testing on your own phone and is NOT a Play Store release.
  To sign for real, generate a keystore outside the repository and set
  BABYCARE_UPLOAD_STORE_FILE / BABYCARE_UPLOAD_STORE_PASSWORD / BABYCARE_UPLOAD_KEY_ALIAS / BABYCARE_UPLOAD_KEY_PASSWORD
  before running (see the "Android Application Architecture" wiki page). Never commit a keystore.

.EXAMPLE
  # emulator build against a backend on this PC (port 8000)
  .\scripts\build_android.ps1 -Env local

.EXAMPLE
  # phone build against the free Render API and the Firebase site
  .\scripts\build_android.ps1 -Env production -ApiUrl https://your-api.onrender.com -WebOrigin https://your-project.web.app
#>
param(
  [ValidateSet('local', 'staging', 'production')] [string]$Env = 'local',
  [string]$ApiUrl = '',
  [string]$WebOrigin = '',
  [ValidateSet('release', 'debug')] [string]$Variant = 'release',
  [string]$ShortPath = 'C:\bc\m',
  [string]$OutName = 'BabyCare-AI-debug.apk',
  [switch]$SkipPrebuild
)
# Native tools (npx, gradle) write warnings to stderr; Windows PowerShell would treat those as fatal under 'Stop'.
# Every native step below checks $LASTEXITCODE instead.
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
$mobile = Join-Path $root 'mobile'
$out = Join-Path $root 'android-builds'

# Toolchain: portable JDK 17 and the Android SDK that Android Studio installed.
if (-not $env:ANDROID_HOME) { $env:ANDROID_HOME = "$env:LOCALAPPDATA\Android\Sdk" }
$jdk = "$env:USERPROFILE\.jdks\jdk-17.0.20.1+1"
if (Test-Path $jdk) { $env:JAVA_HOME = $jdk }
if (-not (Test-Path "$env:JAVA_HOME\bin\java.exe")) { throw "JAVA_HOME ($env:JAVA_HOME) has no java.exe; install JDK 17 and set JAVA_HOME" }
if (-not (Test-Path "$env:ANDROID_HOME\platform-tools\adb.exe")) { throw "Android SDK not found at $env:ANDROID_HOME" }

$env:EXPO_PUBLIC_ENV = $Env
if ($ApiUrl) { Set-Item -Path "env:EXPO_PUBLIC_API_URL_$($Env.ToUpper())" -Value $ApiUrl }
if ($WebOrigin) { $env:EXPO_PUBLIC_WEB_ORIGIN = $WebOrigin }
if ($Env -eq 'staging' -and -not $ApiUrl -and -not (Get-Item "env:EXPO_PUBLIC_API_URL_$($Env.ToUpper())" -ErrorAction SilentlyContinue)) {
  throw "A $Env build needs -ApiUrl (or EXPO_PUBLIC_API_URL_$($Env.ToUpper())); it never falls back to localhost."
}

Push-Location $mobile
try {
  if (-not (Test-Path 'node_modules')) { npm ci; if ($LASTEXITCODE) { throw 'npm ci failed' } }
  if (-not $SkipPrebuild) {
    npx expo prebuild --platform android --no-install
    if ($LASTEXITCODE) { throw 'expo prebuild failed' }
    # Phones + x86_64 emulator only; halves the native build time.
    $gp = 'android\gradle.properties'
    (Get-Content $gp) -replace '^reactNativeArchitectures=.*', 'reactNativeArchitectures=arm64-v8a,x86_64' | Set-Content $gp -Encoding utf8
  }
  # Forward slashes: backslashes are escape characters in .properties files.
  "sdk.dir=$($env:ANDROID_HOME -replace '\\', '/')" | Set-Content 'android\local.properties' -Encoding ascii
} finally { Pop-Location }

# Mirror to the short path (keeps the native build cache there between runs).
New-Item -ItemType Directory -Force $ShortPath | Out-Null
robocopy $mobile $ShortPath /E /MT:16 /NFL /NDL /NJH /NJS /XD "$mobile\android\app\build" "$mobile\android\app\.cxx" "$mobile\android\build" "$mobile\android\.gradle" "$mobile\.expo" | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy failed with code $LASTEXITCODE" }

Push-Location (Join-Path $ShortPath 'android')
try {
  $task = if ($Variant -eq 'release') { 'assembleRelease' } else { 'assembleDebug' }
  .\gradlew.bat $task --no-daemon
  if ($LASTEXITCODE) { throw "gradle $task failed" }
  $apk = Join-Path $ShortPath "android\app\build\outputs\apk\$Variant\app-$Variant.apk"
  New-Item -ItemType Directory -Force $out | Out-Null
  Copy-Item $apk (Join-Path $out $OutName) -Force
  $size = [math]::Round((Get-Item (Join-Path $out $OutName)).Length / 1MB, 1)
  Write-Host "APK ready: android-builds\$OutName ($size MB, env=$Env, variant=$Variant)"
  Write-Host "Install:   adb install -r android-builds\$OutName"
} finally { Pop-Location }
