"""检修封条业务规则：拦截写操作与解除判定共用同一「未解除封条」查询。"""
from django.core.exceptions import ValidationError
from django.utils import timezone

SEAL_BLOCK_MESSAGE = "灶牌 {tag} 在修：检修封条未解除，任何写操作均被拦截。"


def active_seal_for(hearth):
    """该灶当前未解除的检修封条（无则 None）。

    写操作拦截、抽屉「在修」说明、解除按钮判定全部共用此查询。
    """
    return (
        hearth.maintenance_seals.filter(releasedAt__isnull=True)
        .order_by("-startedAt", "-id")
        .first()
    )


def assert_not_sealed(hearth) -> None:
    """有未解除封条时拦截写操作（开灶 / 改相位 / 登记探针 / 收灶）。"""
    if active_seal_for(hearth) is not None:
        raise ValidationError(SEAL_BLOCK_MESSAGE.format(tag=hearth.tag))


def hang_seal(hearth, *, startedAt, plannedReleaseDate, faultSummary, sealedBy):
    """挂检修封条。

    后端强制：仅冷灶、无未收灶值守、同灶无未解除封条；
    界面藏按钮不替代此处校验。
    """
    from apps.kiln.models import FireHearth, MaintenanceSeal

    if active_seal_for(hearth) is not None:
        raise ValidationError("该灶已有未解除的检修封条，不可重复挂条。")
    if hearth.phase != FireHearth.PHASE_COLD:
        raise ValidationError("仅冷灶可挂检修封条。")
    if hearth.open_run() is not None:
        raise ValidationError("该灶有未收灶的值守，须先收灶再挂封条。")
    return MaintenanceSeal.objects.create(
        hearth=hearth,
        startedAt=startedAt,
        plannedReleaseDate=plannedReleaseDate,
        faultSummary=faultSummary,
        sealedBy=sealedBy,
    )


def release_seal(seal, *, when=None):
    """解除封条：写入实解时刻；已解除的封条不可重复解除。"""
    if seal.releasedAt is not None:
        raise ValidationError("该检修封条已解除，实解时刻不可改写。")
    seal.releasedAt = when or timezone.now()
    seal.save(update_fields=["releasedAt"])
    return seal
