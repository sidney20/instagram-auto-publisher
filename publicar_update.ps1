# Publica uma nova versao no GitHub (release = o que o botao "Buscar atualizacao" baixa).
#
# Passos: 1) mude APP_VERSION em config.py  2) rode este script
#
#   powershell -ExecutionPolicy Bypass -File publicar_update.ps1

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$repo = "sidney20/instagram-auto-publisher"

# 1. Versao atual
$cfg = Get-Content (Join-Path $root "config.py") -Raw -Encoding UTF8
if ($cfg -notmatch 'APP_VERSION\s*=\s*"([0-9][0-9A-Za-z\.\-]*)') {
    Write-Host "ERRO: APP_VERSION nao encontrada em config.py" -ForegroundColor Red
    exit 1
}
$ver = $Matches[1]
Write-Host "=== Publicando v$ver ===" -ForegroundColor Cyan

# 2. Zip limpo
& (Join-Path $root "gerar_release.ps1")
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERRO: falha ao gerar o zip" -ForegroundColor Red
    exit 1
}

# 3. Git: commit + push
Push-Location $root
try {
    git rev-parse --is-inside-work-tree 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        git init -b main 2>$null | Out-Null
        git remote add origin "https://github.com/$repo.git"
        Write-Host "Repositorio inicializado."
    }
    if (-not (git config user.name)) {
        git config user.name "sidney20"
        git config user.email "sidney20@users.noreply.github.com"
    }

    git add -A

    # Seguranca: nunca commitar sessoes/dados/log
    $leaks = git diff --cached --name-only | Where-Object {
        $_ -match 'browser_profile|(^|/)logs/|(^|/)debug/|\.venv|(^|/)dist/|data/(config|state)'
    }
    if ($leaks) {
        Write-Host "ERRO: arquivos sensiveis detectados no commit:" -ForegroundColor Red
        $leaks | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
        exit 1
    }

    git diff --cached --quiet 2>$null
    if ($LASTEXITCODE -ne 0) {
        git commit -m "v$ver" | Out-Null
        Write-Host "Commit v$ver feito."
    } else {
        Write-Host "Sem alteracoes novas para commitar."
    }

    git push origin main 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERRO: push falhou (confira git remote / autenticacao)" -ForegroundColor Red
        exit 1
    }
    Write-Host "Push OK."
}
finally { Pop-Location }

# 4. Release (o que o cliente baixa)
gh release create "v$ver" (Join-Path $root "dist\instagram_auto_publisher.zip") `
    --repo $repo `
    --title "v$ver" `
    --notes "Atualizacao automatica v$ver" `
    --latest
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERRO: release v$ver falhou (tag ja existe? Suba APP_VERSION em config.py)" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "PRONTO! v$ver publicada." -ForegroundColor Green
Write-Host "Quem tem o programa: clique em 'Buscar atualizacao'." -ForegroundColor Green
