"""Ежедневный текстовый отчёт в админ-чат (тема REPORTS)."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings
from shared.database import get_session_factory
from shared.models.device import Device
from shared.models.plan import Plan
from shared.models.subscription import Subscription
from shared.models.transaction import Transaction
from shared.models.user import User
from shared.services.admin_log_topics import AdminLogTopic
from shared.services.admin_notify import notify_admin_plain

logger = logging.getLogger(__name__)


def _period_yesterday_local(settings: Settings) -> tuple[datetime, datetime, date]:
    """Вчера 00:00–24:00 в admin_report_timezone → границы UTC."""
    try:
        tz = ZoneInfo((settings.admin_report_timezone or "UTC").strip() or "UTC")
    except Exception:
        tz = timezone.utc
    now_local = datetime.now(tz)
    today_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    start_local = today_start - timedelta(days=1)
    end_local = today_start
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)
    report_day: date = start_local.date()
    return start_utc, end_utc, report_day


async def _scalar(session: AsyncSession, stmt) -> int:
    r = await session.execute(stmt)
    return int(r.scalar_one() or 0)


async def _scalar_decimal(session: AsyncSession, stmt) -> Decimal:
    r = await session.execute(stmt)
    v = r.scalar_one()
    if v is None:
        return Decimal("0")
    return Decimal(str(v))


async def build_daily_report_plain_text(settings: Settings) -> str:
    start_utc, end_utc, report_day = _period_yesterday_local(settings)
    day_s = report_day.strftime("%d.%m.%Y")
    now_utc = datetime.now(timezone.utc)

    factory = get_session_factory()
    ref_display: list[str] = []
    async with factory() as session:
        new_users = await _scalar(
            session,
            select(func.count()).select_from(User).where(
                User.created_at >= start_utc,
                User.created_at < end_utc,
            ),
        )

        trial_plan = await session.execute(select(Plan.id).where(Plan.name == "Триал").limit(1))
        trial_plan_id = trial_plan.scalar_one_or_none()

        new_trials = 0
        if trial_plan_id is not None:
            new_trials = await _scalar(
                session,
                select(func.count())
                .select_from(Subscription)
                .where(
                    Subscription.created_at >= start_utc,
                    Subscription.created_at < end_utc,
                    Subscription.plan_id == trial_plan_id,
                ),
            )

        paid_plan_cond = or_(Plan.price_rub > 0, Plan.name != "Триал")

        new_paid_subs = await _scalar(
            session,
            select(func.count())
            .select_from(Subscription)
            .join(Plan, Plan.id == Subscription.plan_id)
            .where(
                Subscription.created_at >= start_utc,
                Subscription.created_at < end_utc,
                paid_plan_cond,
            ),
        )

        conv_stmt = select(func.count()).select_from(User).where(
            User.trial_used.is_(True),
            exists(
                select(1)
                .select_from(Transaction)
                .where(
                    Transaction.user_id == User.id,
                    Transaction.type == "subscription",
                    Transaction.status == "completed",
                    Transaction.created_at >= start_utc,
                    Transaction.created_at < end_utc,
                )
            ),
        )
        conversions = await _scalar(session, conv_stmt)
        conv_pct = (100.0 * conversions / new_trials) if new_trials else 0.0

        topup_cnt = await _scalar(
            session,
            select(func.count()).select_from(Transaction).where(
                Transaction.type == "topup",
                Transaction.status == "completed",
                Transaction.created_at >= start_utc,
                Transaction.created_at < end_utc,
            ),
        )
        topup_sum = await _scalar_decimal(
            session,
            select(func.coalesce(func.sum(Transaction.amount), 0)).where(
                Transaction.type == "topup",
                Transaction.status == "completed",
                Transaction.created_at >= start_utc,
                Transaction.created_at < end_utc,
            ),
        )

        sub_pay_cnt = await _scalar(
            session,
            select(func.count()).select_from(Transaction).where(
                Transaction.type == "subscription",
                Transaction.status == "completed",
                Transaction.created_at >= start_utc,
                Transaction.created_at < end_utc,
            ),
        )
        sub_pay_sum = await _scalar_decimal(
            session,
            select(func.coalesce(func.sum(Transaction.amount), 0)).where(
                Transaction.type == "subscription",
                Transaction.status == "completed",
                Transaction.created_at >= start_utc,
                Transaction.created_at < end_utc,
            ),
        )

        active_trials = 0
        if trial_plan_id is not None:
            active_trials = await _scalar(
                session,
                select(func.count())
                .select_from(Subscription)
                .where(
                    Subscription.status == "trial",
                    Subscription.expires_at > now_utc,
                    Subscription.plan_id == trial_plan_id,
                ),
            )

        active_paid = await _scalar(
            session,
            select(func.count())
            .select_from(Subscription)
            .join(Plan, Plan.id == Subscription.plan_id)
            .where(
                Subscription.status == "active",
                Subscription.expires_at > now_utc,
                paid_plan_cond,
            ),
        )

        paid_active_users = await _scalar(
            session,
            select(func.count(func.distinct(Subscription.user_id)))
            .select_from(Subscription)
            .join(Plan, Plan.id == Subscription.plan_id)
            .where(
                Subscription.status == "active",
                Subscription.expires_at > now_utc,
                paid_plan_cond,
            ),
        )

        never_used = await _scalar(
            session,
            select(func.count(func.distinct(Subscription.user_id)))
            .select_from(Subscription)
            .join(Plan, Plan.id == Subscription.plan_id)
            .where(
                Subscription.status == "active",
                Subscription.expires_at > now_utc,
                paid_plan_cond,
                exists(
                    select(1).select_from(Device).where(Device.subscription_id == Subscription.id)
                ),
                ~exists(
                    select(1)
                    .select_from(Device)
                    .where(
                        Device.subscription_id == Subscription.id,
                        Device.last_used_at.is_not(None),
                    )
                ),
            ),
        )

        ref_rows = await session.execute(
            select(User.referred_by, func.count(User.id))
            .where(
                User.referred_by.is_not(None),
                User.created_at >= start_utc,
                User.created_at < end_utc,
            )
            .group_by(User.referred_by)
            .order_by(func.count(User.id).desc())
            .limit(5)
        )
        ref_list = ref_rows.all()

        if not ref_list:
            ref_display.append("— данных нет")
        else:
            for ref_id, cnt in ref_list:
                uname = f"#{ref_id}"
                if ref_id:
                    ru = await session.get(User, ref_id)
                    if ru and ru.username:
                        uname = f"#{ref_id} (@{ru.username})"
                ref_display.append(f"• {uname}: {cnt}")

    lines: list[str] = [
        f"📊 Отчёт за {day_s}",
        "",
        "🧭 Итог по периоду",
        f"• Новых пользователей: {new_users}",
        f"• Новых триалов: {new_trials}",
        f"• Конверсий триал → платная: {conversions} ({conv_pct:.1f}%)",
        f"• Новых платных (всего): {new_paid_subs}",
        f"• Поступления всего (только пополнения): {topup_sum:.2f} ₽",
        "",
        "💎 Подписки",
        f"• Активные триалы сейчас: {active_trials}",
        f"• Активные платные сейчас: {active_paid}",
        "",
        "💰 Финансы",
        f"• Оплаты подписок: {sub_pay_cnt} на сумму {sub_pay_sum:.2f} ₽",
        f"• Пополнения: {topup_cnt} на сумму {topup_sum:.2f} ₽",
        "Примечание: «Поступления всего» учитывают только пополнения; покупки подписок "
        "и реферальные бонусы исключены.",
        "",
        "🎟️ Поддержка",
        "• Новых тикетов: 0",
        "• Активных тикетов сейчас: 0",
        "",
        "👤 Активность пользователей",
        f"• Пользователей с активной платной подпиской: {paid_active_users}",
        f"• Пользователей, ни разу не подключившихся: {never_used}",
        "",
        "🤝 Топ по рефералам (за период)",
    ]
    lines.extend(ref_display)
    return "\n".join(lines)


async def send_daily_admin_report(settings: Settings) -> None:
    if not settings.admin_report_enabled:
        return
    try:
        text = await build_daily_report_plain_text(settings)
    except Exception:
        logger.exception("build_daily_report_plain_text failed")
        return
    ok = await notify_admin_plain(
        settings,
        text=text,
        topic=AdminLogTopic.REPORTS,
        event_type="daily_report",
    )
    if not ok:
        logger.warning("daily report send failed")
