"""Публичные страницы возврата после оплаты."""

from __future__ import annotations

import html

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["public-pages"])


def _esc(s: str) -> str:
    return html.escape(s)


def _page(title: str, message: str, *, variant: str) -> HTMLResponse:
    """variant: success | error"""
    icon = "fa-circle-check text-success" if variant == "success" else "fa-circle-xmark text-error"
    alert = "alert-success" if variant == "success" else "alert-error"
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
<body class="min-h-screen bg-base-200 bg-gradient-to-br from-base-200 via-base-200 to-secondary/10 flex items-center justify-center p-6 text-base-content antialiased">
  <div class="card bg-base-100 w-full max-w-lg border border-base-content/10 shadow-2xl">
    <div class="card-body items-center text-center gap-4">
      <i class="fa-solid {icon} text-5xl" aria-hidden="true"></i>
      <h1 class="text-2xl font-bold">{_esc(title)}</h1>
      <p class="text-base-content/70 leading-relaxed">{_esc(message)}</p>
      <div class="alert {alert} text-sm"><i class="fa-brands fa-telegram mr-2" aria-hidden="true"></i>Вернитесь в Telegram-бот</div>
    </div>
  </div>
</body>
</html>
"""
    return HTMLResponse(page)


@router.get("/payment/success")
async def payment_success() -> HTMLResponse:
    return _page(
        "Оплата прошла успешно",
        "Платеж подтвержден. Вернитесь в Telegram-бот, чтобы продолжить работу с подпиской.",
        variant="success",
    )


@router.get("/payment/fail")
async def payment_fail() -> HTMLResponse:
    return _page(
        "Оплата не завершена",
        "Платеж не был подтвержден. Попробуйте снова или выберите другой способ оплаты в боте.",
        variant="error",
    )
