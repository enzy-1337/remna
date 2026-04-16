"""Публичные страницы возврата после оплаты."""

from __future__ import annotations

import html

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["public-pages"])


def _esc(s: str) -> str:
    return html.escape(s)


def _page(title: str, message: str, *, variant: str, badge: str, footer: str) -> HTMLResponse:
    """variant: success | error | neutral"""
    if variant == "success":
        icon = "fa-circle-check text-success"
        accent = "from-success/25 via-primary/20 to-secondary/25"
        alert = "alert-success"
    elif variant == "error":
        icon = "fa-circle-xmark text-error"
        accent = "from-error/25 via-primary/20 to-secondary/25"
        alert = "alert-error"
    else:
        icon = "fa-wave-square text-primary"
        accent = "from-primary/25 via-secondary/20 to-accent/25"
        alert = "alert-info"
    page = f"""<!DOCTYPE html>
<html lang="ru" data-theme="night">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{_esc(title)}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css" crossorigin="anonymous" referrerpolicy="no-referrer" />
  <link href="https://cdn.jsdelivr.net/npm/daisyui@4.12.14/dist/full.min.css" rel="stylesheet" type="text/css" />
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="min-h-screen overflow-hidden bg-[#0a0715] text-base-content antialiased">
  <div class="pointer-events-none fixed inset-0">
    <div class="absolute inset-0 bg-[radial-gradient(circle_at_top,rgba(146,112,255,0.22),transparent_38%),radial-gradient(circle_at_bottom_right,rgba(67,97,238,0.18),transparent_30%),linear-gradient(145deg,#0a0715,#120d26_45%,#1f133f)]"></div>
    <div class="absolute left-[8%] top-[12%] h-40 w-40 rounded-full bg-white/6 blur-3xl"></div>
    <div class="absolute right-[10%] top-[18%] h-56 w-56 rounded-full bg-secondary/20 blur-3xl"></div>
    <div class="absolute bottom-[10%] left-[12%] h-52 w-52 rounded-full bg-primary/20 blur-3xl"></div>
  </div>
  <main class="relative flex min-h-screen items-center justify-center p-6">
    <div class="card w-full max-w-xl overflow-hidden border border-white/10 bg-base-100/90 shadow-[0_30px_80px_-30px_rgba(0,0,0,0.75)] backdrop-blur">
      <div class="h-1.5 w-full bg-gradient-to-r {accent}"></div>
      <div class="card-body items-center gap-5 px-7 py-8 text-center">
        <div class="badge badge-outline badge-lg border-white/15 bg-base-300/50 px-4 py-3 font-medium">{_esc(badge)}</div>
        <i class="fa-solid {icon} text-5xl" aria-hidden="true"></i>
        <h1 class="text-2xl font-bold sm:text-3xl">{_esc(title)}</h1>
        <p class="max-w-lg text-sm leading-relaxed text-base-content/70 sm:text-base">{_esc(message)}</p>
        <div class="alert {alert} border border-white/10 bg-base-200/60 text-sm">{_esc(footer)}</div>
      </div>
    </div>
  </main>
</body>
</html>
"""
    return HTMLResponse(page)


def render_public_stub_page() -> HTMLResponse:
    return _page(
        "Flux Network Web Admin",
        "Сервис доступен, но публичной страницы здесь нет. Этот адрес используется как точка входа для служебных и административных сценариев.",
        variant="neutral",
        badge="WEBA",
        footer="Откройте только нужный вам адрес или вернитесь туда, откуда пришли.",
    )


def render_not_found_page(path: str) -> HTMLResponse:
    shown = path if path.startswith("/") else f"/{path}"
    return _page(
        "Страница не найдена",
        f"Адрес {shown} не существует или был перемещен. Проверьте путь и попробуйте снова.",
        variant="error",
        badge="404",
        footer="По этому адресу ничего нет.",
    )


@router.get("/")
async def public_stub(_request: Request) -> HTMLResponse:
    return render_public_stub_page()


@router.get("/payment/success")
async def payment_success() -> HTMLResponse:
    return _page(
        "Оплата прошла успешно",
        "Платеж подтвержден. Вернитесь в Telegram-бот, чтобы продолжить работу с подпиской.",
        variant="success",
        badge="SUCCESS",
        footer="Вернитесь в Telegram-бот.",
    )


@router.get("/payment/fail")
async def payment_fail() -> HTMLResponse:
    return _page(
        "Оплата не завершена",
        "Платеж не был подтвержден. Попробуйте снова или выберите другой способ оплаты в боте.",
        variant="error",
        badge="ERROR",
        footer="Вернитесь в Telegram-бот и попробуйте еще раз.",
    )
