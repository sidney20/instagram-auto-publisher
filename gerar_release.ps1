# Gera dist\instagram_auto_publisher.zip limpo para enviar ao amigo.
# Uso: click-direito -> "Executar com o PowerShell" ou:
#   powershell -ExecutionPolicy Bypass -File gerar_release.ps1

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$dist = Join-Path $root "dist"
$stage = Join-Path $dist "instagram_auto_publisher"
$zip = Join-Path $dist "instagram_auto_publisher.zip"

New-Item -ItemType Directory -Force -Path $dist | Out-Null
if (Test-Path $stage) {
    # Remove-Item falha em paths longos (cache do Chrome); robocopy /MIR aguenta
    $empty = Join-Path $env:TEMP ("iap_empty_" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $empty | Out-Null
    robocopy $empty $stage /MIR /NFL /NDL /NJH /NJS /NC /NS /NP | Out-Null
    Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item $empty -Recurse -Force -ErrorAction SilentlyContinue
}
New-Item -ItemType Directory -Force -Path $stage | Out-Null

# Copia o codigo sem: venv, sessoes do navegador, historico, configs, logs, caches
$excludeDirs = @(
    ".venv", "__pycache__", ".pytest_cache", ".git", "dist",
    "logs", "debug",
    "browser_profile",
    "browser_profile_perfil1", "browser_profile_perfil2"
)
$excludeFiles = @(
    "config.json", "state_perfil1.json", "state_perfil2.json",
    "*.pyc", "*.tmp", "gerar_release.ps1", "publicar_update.ps1"
)

robocopy $root $stage /E `
    /XD @excludeDirs `
    /XF @excludeFiles `
    /NFL /NDL /NJH /NJS /NC /NS /NP | Out-Null

if ($LASTEXITCODE -ge 8) {
    Write-Host "ERRO no robocopy (codigo $LASTEXITCODE)" -ForegroundColor Red
    exit 1
}

if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path $stage -DestinationPath $zip -Force

# Validacoes de seguranca
$bad = @()
foreach ($f in @("browser_profile", "browser_profile_perfil1", "browser_profile_perfil2", "data\config.json", "data\state_perfil1.json", "data\state_perfil2.json")) {
    if (Test-Path (Join-Path $stage $f)) { $bad += $f }
}

$size = [math]::Round((Get-Item $zip).Length / 1MB, 1)
Write-Host ""
if ($bad.Count -gt 0) {
    Write-Host "ATENCOES: conteudo sensiveis no zip:" -ForegroundColor Red
    $bad | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
    exit 1
}
Write-Host "OK: $zip ($size MB)" -ForegroundColor Green
Write-Host "Arquivos: " -NoNewline
Write-Host (Get-ChildItem $stage -Recurse -File | Measure-Object).Count
exit 0
