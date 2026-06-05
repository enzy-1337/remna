"""Скачивание фото из отзывов на Wildberries, Ozon, Яндекс.Маркет."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = 20.0
_MAX_PHOTOS = 50

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}


@dataclass
class MarketplaceResult:
    platform: str
    product_id: str
    product_name: str = ""
    photo_urls: list[str] = field(default_factory=list)
    error: str | None = None


# ─────────────────────────── Wildberries ────────────────────────────

_WB_ARTICLE_RE = re.compile(r"wildberries\.ru/catalog/(\d+)/", re.IGNORECASE)
_WB_SHORT_RE = re.compile(r"wb\.ru/catalog/(\d+)/", re.IGNORECASE)


def _wb_extract_id(url: str) -> str | None:
    for pat in (_WB_ARTICLE_RE, _WB_SHORT_RE):
        m = pat.search(url)
        if m:
            return m.group(1)
    return None


def _wb_basket_host(nm: int) -> str:
    vol = nm // 100_000
    part = nm // 1_000
    if vol <= 143:
        basket = "01"
    elif vol <= 287:
        basket = "02"
    elif vol <= 431:
        basket = "03"
    elif vol <= 719:
        basket = "04"
    elif vol <= 1007:
        basket = "05"
    elif vol <= 1061:
        basket = "06"
    elif vol <= 1115:
        basket = "07"
    elif vol <= 1169:
        basket = "08"
    elif vol <= 1313:
        basket = "09"
    elif vol <= 1601:
        basket = "10"
    elif vol <= 1655:
        basket = "11"
    elif vol <= 1919:
        basket = "12"
    elif vol <= 2045:
        basket = "13"
    elif vol <= 2189:
        basket = "14"
    elif vol <= 2405:
        basket = "15"
    elif vol <= 2621:
        basket = "16"
    elif vol <= 2837:
        basket = "17"
    else:
        basket = "18"
    return f"https://basket-{basket}.wbbasket.ru/vol{vol}/part{part}/{nm}"


async def _wb_fetch_review_photos(nm_id: int) -> list[str]:
    """Забрать фото из отзывов WB через публичный JSON-API."""
    photo_urls: list[str] = []
    page = 1
    async with httpx.AsyncClient(headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True) as client:
        while len(photo_urls) < _MAX_PHOTOS:
            url = (
                f"https://feedbacks.wildberries.ru/api/v1/feedbacks/products/nm"
                f"?nmId={nm_id}&take=30&skip={30 * (page - 1)}&order=dateDesc"
            )
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                logger.warning("WB feedbacks API error: %s", exc)
                break
            feedbacks = (data or {}).get("feedbacks") or []
            if not feedbacks:
                break
            for fb in feedbacks:
                photos = fb.get("photo") or []
                for ph in photos:
                    if isinstance(ph, dict):
                        full = ph.get("fullSize") or ph.get("minSize") or ""
                    else:
                        full = str(ph or "")
                    if full:
                        photo_urls.append(full)
                    if len(photo_urls) >= _MAX_PHOTOS:
                        break
                if len(photo_urls) >= _MAX_PHOTOS:
                    break
            page += 1
            if len(feedbacks) < 30:
                break
    return photo_urls


async def scrape_wb(url: str) -> MarketplaceResult:
    nm_str = _wb_extract_id(url)
    if not nm_str:
        return MarketplaceResult(platform="wildberries", product_id="", error="Не удалось извлечь ID товара из ссылки WB")
    nm = int(nm_str)
    photos = await _wb_fetch_review_photos(nm)
    return MarketplaceResult(
        platform="wildberries",
        product_id=nm_str,
        product_name=f"WB #{nm_str}",
        photo_urls=photos,
    )


# ─────────────────────────── Ozon ───────────────────────────────────

_OZON_ID_RE = re.compile(r"ozon\.ru/product/[^/]+-(\d+)/", re.IGNORECASE)
_OZON_SHORT_RE = re.compile(r"ozon\.ru/t/\w+", re.IGNORECASE)


def _ozon_extract_id(url: str) -> str | None:
    m = _OZON_ID_RE.search(url)
    return m.group(1) if m else None


def _ozon_parse_photos_from_widgets(widgets: dict) -> list[str]:
    """Извлечь URL фото из widgetStates Ozon."""
    import json as _json
    photo_urls: list[str] = []
    for _key, val in widgets.items():
        if isinstance(val, str):
            try:
                val = _json.loads(val)
            except Exception:
                continue
        if not isinstance(val, dict):
            continue
        # Перебираем все возможные форматы структуры отзывов
        reviews = (
            val.get("reviews")
            or val.get("items")
            or val.get("feedbacks")
            or []
        )
        for rv in reviews:
            if not isinstance(rv, dict):
                continue
            # Формат 1: media как список словарей
            for media in rv.get("media") or []:
                if not isinstance(media, dict):
                    if isinstance(media, str) and media.startswith("http"):
                        photo_urls.append(media)
                    continue
                img = (
                    media.get("url")
                    or media.get("src")
                    or media.get("originalUrl")
                    or ""
                )
                if img:
                    photo_urls.append(img)
            # Формат 2: photos как список
            for ph in rv.get("photos") or []:
                if isinstance(ph, dict):
                    img = ph.get("url") or ph.get("src") or ""
                elif isinstance(ph, str):
                    img = ph
                else:
                    img = ""
                if img:
                    photo_urls.append(img)
            # Формат 3: content.media
            content = rv.get("content") or {}
            if isinstance(content, dict):
                for media in content.get("media") or []:
                    if not isinstance(media, dict):
                        continue
                    img = media.get("url") or media.get("src") or ""
                    if img:
                        photo_urls.append(img)
    return photo_urls


async def _ozon_fetch_review_photos(item_id: int) -> tuple[str, list[str]]:
    """Забрать фото из отзывов Ozon через внутренний API."""
    photo_urls: list[str] = []
    product_name = f"Ozon #{item_id}"
    page = 1
    async with httpx.AsyncClient(headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True) as client:
        while len(photo_urls) < _MAX_PHOTOS:
            data = None
            # Попытка 1: POST composer API (основной)
            try:
                resp = await client.post(
                    "https://www.ozon.ru/api/composer-api.bx/page/json/v2",
                    json={"url": f"/product/{item_id}/reviews/?page={page}"},
                    headers={**_HEADERS, "Content-Type": "application/json"},
                )
                if resp.status_code < 400:
                    data = resp.json()
            except Exception as exc:
                logger.debug("Ozon composer POST failed: %s", exc)

            # Попытка 2: GET entrypoint API
            if data is None:
                try:
                    resp = await client.get(
                        "https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2",
                        params={"url": f"/product/{item_id}/reviews/?page={page}"},
                        headers=_HEADERS,
                    )
                    if resp.status_code < 400:
                        data = resp.json()
                except Exception as exc:
                    logger.warning("Ozon entrypoint GET failed: %s", exc)
                    break

            if data is None:
                break

            widgets = _deep_get(data, "widgetStates") or {}
            new_photos = _ozon_parse_photos_from_widgets(widgets)
            if not new_photos:
                break
            photo_urls.extend(new_photos)
            page += 1
    return product_name, photo_urls


def _deep_get(d: Any, key: str) -> Any:
    if isinstance(d, dict):
        if key in d:
            return d[key]
        for v in d.values():
            result = _deep_get(v, key)
            if result is not None:
                return result
    return None


async def scrape_ozon(url: str) -> MarketplaceResult:
    # Резолвим короткие ссылки типа ozon.ru/t/XXXXX
    if _OZON_SHORT_RE.search(url):
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as client:
                r = await client.get(url, headers=_HEADERS)
                url = str(r.url)
                logger.info("Ozon short URL resolved to: %s", url)
        except Exception as exc:
            logger.warning("Failed to resolve Ozon short URL: %s", exc)

    item_str = _ozon_extract_id(url)
    if not item_str:
        return MarketplaceResult(platform="ozon", product_id="", error="Не удалось извлечь ID товара из ссылки Ozon")
    item_id = int(item_str)
    name, photos = await _ozon_fetch_review_photos(item_id)
    return MarketplaceResult(
        platform="ozon",
        product_id=item_str,
        product_name=name,
        photo_urls=photos,
    )


# ─────────────────────────── Яндекс.Маркет ──────────────────────────

_YM_SKU_RE = re.compile(r"market\.yandex\.ru/product(?:--[^/]+)?/(\d+)", re.IGNORECASE)
_YM_OFFER_RE = re.compile(r"sku=(\d+)", re.IGNORECASE)


def _ym_extract_id(url: str) -> str | None:
    m = _YM_SKU_RE.search(url)
    if m:
        return m.group(1)
    m = _YM_OFFER_RE.search(url)
    return m.group(1) if m else None


async def _ym_fetch_review_photos(model_id: int) -> tuple[str, list[str]]:
    """Забрать фото из отзывов Яндекс.Маркет через GraphQL/REST API."""
    photo_urls: list[str] = []
    product_name = f"ЯМ #{model_id}"
    page = 1
    async with httpx.AsyncClient(headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True) as client:
        while len(photo_urls) < _MAX_PHOTOS:
            try:
                resp = await client.get(
                    f"https://api.content.market.yandex.ru/v1/model/{model_id}/reviews.json",
                    params={
                        "geo_id": 213,
                        "page": page,
                        "count": 30,
                        "sort": "DATE_DESC",
                    },
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception:
                # Fallback: попробуем через фронтовый API
                try:
                    resp = await client.get(
                        f"https://market.yandex.ru/api/cataloger/v1/modelOpinions",
                        params={"modelId": model_id, "page": page, "pageSize": 20, "sortBy": "DATE_DESC"},
                        headers={**_HEADERS, "X-Requested-With": "XMLHttpRequest"},
                    )
                    resp.raise_for_status()
                    data = resp.json()
                except Exception as exc2:
                    logger.warning("YandexMarket reviews API error: %s", exc2)
                    break

            opinions = (
                (data or {}).get("opinions")
                or (data or {}).get("items")
                or (data or {}).get("modelOpinions")
                or []
            )
            if not opinions:
                break
            for op in opinions:
                if not isinstance(op, dict):
                    continue
                for photo in op.get("photos") or op.get("images") or []:
                    if isinstance(photo, dict):
                        img = photo.get("url") or photo.get("src") or photo.get("original") or ""
                    else:
                        img = str(photo or "")
                    if img:
                        photo_urls.append(img)
                    if len(photo_urls) >= _MAX_PHOTOS:
                        break
                if len(photo_urls) >= _MAX_PHOTOS:
                    break
            page += 1
            if len(opinions) < 10:
                break
    return product_name, photo_urls


async def scrape_yandex_market(url: str) -> MarketplaceResult:
    model_str = _ym_extract_id(url)
    if not model_str:
        return MarketplaceResult(platform="yandex_market", product_id="", error="Не удалось извлечь ID товара из ссылки Яндекс.Маркет")
    model_id = int(model_str)
    name, photos = await _ym_fetch_review_photos(model_id)
    return MarketplaceResult(
        platform="yandex_market",
        product_id=model_str,
        product_name=name,
        photo_urls=photos,
    )


# ─────────────────────────── Роутер ─────────────────────────────────

def detect_marketplace(url: str) -> str | None:
    url_lower = (url or "").lower()
    if "wildberries.ru" in url_lower or "wb.ru" in url_lower:
        return "wildberries"
    if "ozon.ru" in url_lower:
        return "ozon"
    if "market.yandex.ru" in url_lower:
        return "yandex_market"
    return None


async def scrape_marketplace_reviews(url: str) -> MarketplaceResult:
    """Определить маркетплейс по URL и загрузить фото из отзывов."""
    platform = detect_marketplace(url)
    if platform == "wildberries":
        return await scrape_wb(url)
    if platform == "ozon":
        return await scrape_ozon(url)
    if platform == "yandex_market":
        return await scrape_yandex_market(url)
    return MarketplaceResult(
        platform="unknown",
        product_id="",
        error="Ссылка не распознана. Поддерживаются: Wildberries, Ozon, Яндекс.Маркет",
    )
