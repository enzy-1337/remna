"""PNG QR-код для ссылки подписки (VPN)."""

from __future__ import annotations

import io

import segno


def subscription_url_qr_png(url: str, *, scale: int = 6) -> bytes:
    """Возвращает PNG (bytes) с QR для текста/URL."""
    u = (url or "").strip()
    if not u:
        raise ValueError("empty subscription url")
    q = segno.make(u, error="m")
    buf = io.BytesIO()
    q.save(buf, kind="png", scale=scale, border=2)
    return buf.getvalue()
