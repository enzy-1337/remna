# Запуск стека в Docker (Windows PowerShell).
#   .\start.ps1              — миграции + up -d --build
#   .\start.ps1 -MigrateOnly — только alembic upgrade head
#   .\start.ps1 -Down        — docker compose down
#   .\start.ps1 -NoBuild     — без пересборки образов
#   .\start.ps1 -Foreground  — логи в консоли (без -d)

param(
    [switch]$MigrateOnly,
    [switch]$Down,
    [switch]$NoBuild,
    [switch]$Foreground
)

Set-Location $PSScriptRoot

if (-not (Test-Path ".env")) {
    Write-Error "Нет файла .env — скопируйте .env.example в .env"
    exit 1
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Error "Docker не найден в PATH. Запустите Docker Desktop."
    exit 1
}

if ($Down) {
    Write-Host ">>> Остановка стека..."
    docker compose down
    exit $LASTEXITCODE
}

Write-Host ">>> Миграции БД (alembic upgrade head)..."
docker compose run --rm migrate
if ($LASTEXITCODE -ne 0) {
    Write-Error "Миграции не выполнились (код $LASTEXITCODE)"
    exit $LASTEXITCODE
}

if ($MigrateOnly) {
    Write-Host ">>> Готово: миграции применены."
    exit 0
}

$upArgs = @("compose", "up")
if (-not $Foreground) { $upArgs += "-d" }
if (-not $NoBuild) { $upArgs += "--build" }

Write-Host ">>> Запуск: docker $($upArgs -join ' ')"
docker @upArgs
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if (-not $Foreground) {
    Write-Host ""
    Write-Host "Сервисы запущены."
    Write-Host "  API: http://localhost:8000/health"
    Write-Host "  Логи: docker compose logs -f bot"
}
