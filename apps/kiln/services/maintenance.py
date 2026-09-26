"""检修封条业务规则。

挂条拦截、写操作拦截、解除判定共用同一个「未解除封条」查询：
`active_seal_for`。视图层与服务层都经由这里判定，保证按钮被绕过
（直接 POST）时后端仍然拦截。
"""
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.kiln.models import FireHearth, MaintenanceSeal


def active_seal_for(hearth):
    """返回灶台当前未解除的检修封条（无则 None）。拦截与解除判定共用。"""
    return hearth.active_seal()


def active_seal_count():
    """全厂未解除封条数（班次条角标用）。"""
    return MaintenanceSeal.objects.filter(releasedAt__isnull=True).count()


def assert_not_sealed(hearth):
    """未解除期间任何写操作（开灶/改相位/登记探针/收灶）一律拦截。"""
    seal = active_seal_for(hearth)
    if seal is not None:
        raise ValidationError(
            "灶台在修：{summary}（计划解除 {until:%Y-%m-%d}），解除封条前禁止写操作。".format(
                summary=seal.faultSummary,
                until=seal.plannedReleaseDate,
            )
        )


def seal_placement_block_reason(hearth):
    """挂条前置判定：可挂返回 None，否则返回中文原因。

    仅冷灶且无未收灶值守可挂条；同灶已有未解除封条不可再挂。
    """
    if active_seal_for(hearth) is not None:
        return "该灶已有未解除的检修封条，不可重复挂条。"
    if hearth.phase != FireHearth.PHASE_COLD:
        return "仅冷灶可挂检修封条。"
    if hearth.open_run() is not None:
        return "该灶存在未收灶值守，须先收灶再挂条。"
    return None


def place_maintenance_seal(
    *, hearth, placed_by, started_at, planned_release_date, fault_summary
):
    """挂条统一入口：无论按钮是否展示，后端都重新校验前置条件。"""
    reason = seal_placement_block_reason(hearth)
    if reason is not None:
        raise ValidationError(reason)
    return MaintenanceSeal.objects.create(
        hearth=hearth,
        placedBy=placed_by,
        startedAt=started_at,
        plannedReleaseDate=planned_release_date,
        faultSummary=fault_summary,
    )


def release_maintenance_seal(*, hearth, released_at=None):
    """解除封条：写入实解时刻。判定与写操作拦截共用 active_seal_for。"""
    seal = active_seal_for(hearth)
    if seal is None:
        raise ValidationError("该灶没有未解除的检修封条。")
    seal.releasedAt = released_at or timezone.now()
    seal.save(update_fields=["releasedAt"])
    return seal
