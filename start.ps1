# Запуск всего стека: PostgreSQL, Redis, бот, API (Docker Compose).
# Использование:
#   .\start.ps1              — поднять в фоне (-d) с пересборкой (миграции — сервис migrate при up)
#   .\start.ps1 -Migrate     — только миграции: docker compose run --rm migrate, затем up
#   .\start.ps1 -Foreground  — логи в консоли (без -d)
#   .\start.ps1 -NoBuild     — без --build (быстрее при повторном запуске)
#   .\start.ps1 -Down        — остановить и удалить контейнеры

param(
    [switch]$Migrate,
    [switch]$Foreground,
    [switch]$NoBuild,
    [switch]$Down
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
if ([string]::IsNullOrEmpty($Root)) { $Root = Get-Location }
Set-Location $Root

$envFile = Join-Path $Root ".env"
if (-not (Test-Path $envFile)) {
    Write-Host "Ошибка: нет файла .env" -ForegroundColor Red
    Write-Host "Скопируйте .env.example в .env и заполните переменные."
    exit 1
}

function Test-Docker {
    docker version 1>$null 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Docker не найден или не запущен. Установите Docker Desktop и запустите его." -ForegroundColor Red
        exit 1
    }
}

Test-Docker

if ($Down) {
    Write-Host ">>> Остановка стека..." -ForegroundColor Cyan
    docker compose down
    exit $LASTEXITCODE
}

if ($Migrate) {
    Write-Host ">>> Миграции БД (docker compose run --rm migrate)..." -ForegroundColor Cyan
    docker compose run --rm migrate
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

$composeArgs = @("compose", "up")
if (-not $Foreground) { $composeArgs += "-d" }
if (-not $NoBuild) { $composeArgs += "--build" }

Write-Host ">>> Запуск: docker $($composeArgs -join ' ')" -ForegroundColor Cyan
docker @composeArgs
$code = $LASTEXITCODE

if ($code -eq 0 -and -not $Foreground) {
    Write-Host ""
    Write-Host "Сервисы запущены." -ForegroundColor Green
    Write-Host "  API health: http://localhost:8000/health"
    Write-Host "  Логи бота:  docker compose logs -f bot"
    Write-Host "  Логи API:   docker compose logs -f api"
}

exit $code
