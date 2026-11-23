# iSync kurulum dosyasını üretir:  dist\iSync-Setup-<sürüm>.exe
# Kullanım (proje klasöründe):  powershell -ExecutionPolicy Bypass -File packaging\build.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "[build] .venv oluşturuluyor..."
    py -3 -m venv .venv
    & $py -m pip install --upgrade pip -q
}
Write-Host "[build] Bağımlılıklar kontrol ediliyor..."
& $py -m pip install -q -r requirements.txt pyinstaller
if ($LASTEXITCODE -ne 0) { throw "pip install başarısız" }

$version = (& $py -c "import isync; print(isync.__version__)").Trim()
Write-Host "[build] iSync $version"

& $py "$root\packaging\make_icon.py"

$work = Join-Path $root "build"
$appDist = Join-Path $work "app"
Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue

# Python 3.15 'abi3t' uyumluluk DLL'i (cryptography gibi paketler python3t.dll ister); PyInstaller kendisi eklemiyor
$extraBin = @()
$compat = (& $py -c "import sys,os; p=os.path.join(sys.base_prefix,'abi3t-compat','python3t.dll'); print(p if os.path.exists(p) else '')").Trim()
if ($compat) { $extraBin += @('--add-binary', "$compat;.") }

Write-Host "[build] 1/2 Uygulama paketleniyor (iSync.exe)..."
& $py -m PyInstaller --noconfirm --clean --log-level WARN `
    --windowed --name iSync --icon "$root\packaging\iSync.ico" `
    --distpath $appDist --workpath (Join-Path $work "w-app") --specpath $work `
    --add-data "$root\isync\static;isync\static" `
    --collect-submodules pymobiledevice3 --collect-data pymobiledevice3 --recursive-copy-metadata pymobiledevice3 `
    --collect-submodules uvicorn --collect-all pillow_heif `
    --hidden-import isync.server --hidden-import tkinter --hidden-import tkinter.filedialog `
    --hidden-import tkinter.messagebox `
    --exclude-module IPython --exclude-module jedi --exclude-module parso --exclude-module av `
    --exclude-module matplotlib --exclude-module pytest @extraBin `
    "$root\packaging\isync_app.py"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller (uygulama) başarısız" }

$appDir = Join-Path $appDist "iSync"
Set-Content -Path (Join-Path $appDir "version.txt") -Value $version -Encoding ascii -NoNewline

# Öz-test: paket, Python'u ve proje ortamını görmeyen bir ortamda tüm parçalarını doğrulasın
Write-Host "[build] Öz-test (Python'suz ortam)..."
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = Join-Path $appDir "iSync.exe"; $psi.Arguments = "--selftest"; $psi.UseShellExecute = $false
$psi.WorkingDirectory = $appDir
$psi.EnvironmentVariables["PATH"] = "$env:SystemRoot\System32;$env:SystemRoot"
foreach ($v in "VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME") { if ($psi.EnvironmentVariables.ContainsKey($v)) { $psi.EnvironmentVariables.Remove($v) } }
$st = [System.Diagnostics.Process]::Start($psi); $st.WaitForExit(120000) | Out-Null
$report = Get-Content (Join-Path $env:USERPROFILE ".isync\selftest.json") -Raw -Encoding UTF8
Write-Host $report
if ($st.ExitCode -ne 0) { throw "Öz-test başarısız (kod $($st.ExitCode)); yukarıdaki rapora bakın" }

Write-Host "[build] Paket sıkıştırılıyor..."
$payload = Join-Path $work "payload.zip"
& $py -c "import shutil,sys; shutil.make_archive(sys.argv[1][:-4], 'zip', sys.argv[2])" $payload $appDir
if ($LASTEXITCODE -ne 0) { throw "zip başarısız" }

Write-Host "[build] 2/2 Kurulum programı paketleniyor..."
$setupName = "iSync-Setup-$version"
& $py -m PyInstaller --noconfirm --clean --log-level WARN `
    --onefile --windowed --name $setupName --icon "$root\packaging\iSync.ico" `
    --distpath (Join-Path $root "dist") --workpath (Join-Path $work "w-setup") --specpath $work `
    --add-data "$payload;." --add-data "$root\packaging\iSync.ico;." --add-data "$root\packaging\iSync.png;." `
    "$root\packaging\installer.py"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller (kurulum) başarısız" }

# Ara derleme klasörünü sil: içindeki yarım exe'ler tek başına çalışmaz (DLL hatası verir), karışıklık olmasın
Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue

$out = Join-Path $root "dist\$setupName.exe"
$mb = [math]::Round((Get-Item $out).Length / 1MB, 1)
Write-Host "[build] Hazır: $out ($mb MB)"
