"""Публичные страницы возврата после оплаты."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["public-pages"])


def _page(title: str, message: str, accent: str) -> HTMLResponse:
    html = f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title}</title>
  <style>
    :root {{ --bg:#0b1020; --card:#131c33; --stroke:#2a3b66; --txt:#e9edf7; --muted:#a7b4d0; --accent:{accent}; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; min-height:100vh; display:grid; place-items:center; background:linear-gradient(150deg, var(--bg), #101936); color:var(--txt); font-family: Inter, Segoe UI, Arial, sans-serif; }}
    .card {{ width:min(92vw, 560px); background:var(--card); border:1px solid var(--stroke); border-radius:16px; padding:24px; text-align:center; }}
    h1 {{ margin:0 0 10px; font-size:26px; color:var(--accent); }}
    p {{ margin:0; color:var(--muted); line-height:1.45; }}
  </style>
</head>
<body>
  <main class="card">
    <h1>{title}</h1>
    <p>{message}</p>
  </main>
</body>
</html>
"""
    return HTMLResponse(html)


@router.get("/payment/success")
async def payment_success() -> HTMLResponse:
    return _page(
        "Оплата прошла успешно",
        "Платеж подтвержден. Вернитесь в Telegram-бот, чтобы продолжить работу с подпиской.",
        "#60d394",
    )


@router.get("/payment/fail")
async def payment_fail() -> HTMLResponse:
    return _page(
        "Оплата не завершена",
        "Платеж не был подтвержден. Попробуйте снова или выберите другой способ оплаты в боте.",
        "#ff6b6b",
    )
