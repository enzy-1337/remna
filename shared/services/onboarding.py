"""Подсказки «как подключиться» до первого подключения устройства (сайт и Telegram Mini App).

Панель отдаёт Xray-конфиги с проверкой HWID — рекомендуемый клиент Happ (iOS/Android/Windows/macOS).
Ссылки на приложения настраиваются в .env (ONBOARDING_APP_*_URL), импорт подписки — диплинк happ://add/.
"""

from __future__ import annotations

from shared.config import Settings

PLATFORMS = (
    ("ios", "iPhone / iPad", "onboarding_app_ios_url"),
    ("android", "Android", "onboarding_app_android_url"),
    ("windows", "Windows", "onboarding_app_windows_url"),
    ("macos", "macOS", "onboarding_app_macos_url"),
)


def onboarding_payload(settings: Settings, subscription_url: str) -> dict:
    apps = [
        {"os": key, "label": label, "url": (getattr(settings, attr) or "").strip()}
        for key, label, attr in PLATFORMS
        if (getattr(settings, attr) or "").strip()
    ]
    sub = (subscription_url or "").strip()
    return {
        "app_name": (settings.onboarding_app_name or "Happ").strip(),
        "apps": apps,
        "subscription_url": sub,
        # Happ: happ://add/<ссылка подписки> — открывает приложение и сразу добавляет подписку
        "import_url": f"happ://add/{sub}" if sub else "",
        "instructions_phone": (settings.instruction_telegraph_phone_url or settings.instruction_android_url or "").strip(),
        "instructions_pc": (settings.instruction_telegraph_pc_url or settings.instruction_macos_url or "").strip(),
    }
