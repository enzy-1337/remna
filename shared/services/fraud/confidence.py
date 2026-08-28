"""Детерминированный weighted-rule confidence-score по детекторам (не ML — считается
и объясняется по слагаемым, слагаемые попадают в FraudIncident.evidence["confidence_breakdown"]
для прозрачности в алерте). См. план, раздел 3.
"""

from __future__ import annotations


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def hwid_collision_confidence(
    *,
    other_account_established: bool,
    is_repeat_offender: bool,
    prior_incidents_dismissed: int,
    family_linked: bool,
) -> tuple[float, dict]:
    """
    other_account_established — у исходного (первого) аккаунта этот HWID был активен
        не только что, а хотя бы сутки назад (устоявшееся владение, а не шум одновременной
        привязки).
    is_repeat_offender — этот HWID уже засветился на 3+ разных аккаунтах за всю историю.
    prior_incidents_dismissed — сколько раз админ уже пропускал коллизии HWID для ЭТОГО
        (нового) аккаунта — снижает уверенность для конкретных ложных срабатываний.
    family_linked — оба аккаунта в одной семейной подписке (FamilyMember/resolve_account_user_id).
    """
    breakdown: dict[str, float] = {"base": 0.55}
    score = breakdown["base"]

    if other_account_established:
        breakdown["other_account_established"] = 0.20
        score += 0.20
    if is_repeat_offender:
        breakdown["repeat_offender_hwid"] = 0.25
        score += 0.25
    if prior_incidents_dismissed:
        penalty = -0.15 * min(prior_incidents_dismissed, 2)
        breakdown["prior_dismissals_penalty"] = penalty
        score += penalty

    if family_linked:
        breakdown["family_linked_cap"] = True
        score = min(score, 0.2)

    score = clamp01(score)
    breakdown["final"] = score
    return score, breakdown


def ip_hop_confidence(*, distinct_ip_count: int, suspicious_threshold: int) -> tuple[float, dict]:
    """Мягкий порог (жёсткое правило 10 IP/15с обходит confidence вообще, см. incident_service).
    Линейно растёт от 0.5 на самом пороге до 0.95 при "+5 IP сверху" — без ML, лишь бы объяснимо.
    """
    over = max(0, distinct_ip_count - suspicious_threshold)
    score = clamp01(0.5 + 0.09 * over)
    breakdown = {
        "distinct_ip_count": distinct_ip_count,
        "suspicious_threshold": suspicious_threshold,
        "over_threshold": over,
        "final": score,
    }
    return score, breakdown
