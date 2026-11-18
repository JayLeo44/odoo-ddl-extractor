<#
  Genera el ejecutable y el instalador de Windows.

    powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1
    powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1 -Python 3.12

  Requisitos:
    - Python (launcher "py") con la version indicada (default 3.10)
    - Inno Setup 6 para el instalador: winget install -e --id JRSoftware.InnoSetup
      (si no esta, se genera solo dist\ddl-extractor\ y un .zip portable)
#>
param(
    [string]$Python = "3.10"
)
$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $Root

$Venv = ".venv-build"
$Py = Join-Path $Venv "Scripts\python.exe"
if (-not (Test-Path $Py)) {
    Write-Host ">> Creando $Venv con Python $Python"
    py -$Python -m venv $Venv
}
& $Py -m pip install --upgrade pip -q
& $Py -m pip install -q -r requirements.txt -r requirements-gui.txt -r requirements-build.txt

Write-Host ">> PyInstaller"
& $Py -m PyInstaller --noconfirm --clean packaging\ddl_extractor.spec --distpath dist --workpath build\pyinstaller
if ($LASTEXITCODE -ne 0) { throw "PyInstaller fallo" }

$Version = (& $Py -c "import ddl_gui; print(ddl_gui.__version__)").Trim()

Write-Host ">> ZIP portable"
$Zip = "dist\DDL-Extractor-$Version-win64-portable.zip"
if (Test-Path $Zip) { Remove-Item $Zip }
Compress-Archive -Path "dist\ddl-extractor\*" -DestinationPath $Zip

$Iscc = Get-Command iscc.exe -ErrorAction SilentlyContinue
if (-not $Iscc) {
    foreach ($c in @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
                     "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
                     "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe")) {
        if (Test-Path $c) { $Iscc = $c; break }
    }
} else { $Iscc = $Iscc.Source }

if ($Iscc) {
    Write-Host ">> Inno Setup ($Iscc)"
    & $Iscc "/DAppVersion=$Version" packaging\windows\installer.iss
    if ($LASTEXITCODE -ne 0) { throw "ISCC fallo" }
} else {
    Write-Warning "Inno Setup no encontrado: solo se genero el ZIP portable. Instalar con: winget install -e --id JRSoftware.InnoSetup"
}

Write-Host ">> Listo:"
Get-ChildItem dist -File | Format-Table Name, @{n="MB";e={[math]::Round($_.Length/1MB,1)}}
