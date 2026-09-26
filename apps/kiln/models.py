from django.conf import settings
from django.db import models


class ResinLot(models.Model):
    lotCode = models.CharField("来脂批号", max_length=64, unique=True)
    originPlace = models.CharField("来源地", max_length=120)
    arrivalKg = models.DecimalField("到货量(kg)", max_digits=10, decimal_places=2)
    receivedAt = models.DateTimeField("到货时间")

    class Meta:
        ordering = ["-receivedAt", "-id"]
        verbose_name = "来脂批"
        verbose_name_plural = "来脂批"

    def __str__(self):
        return f"{self.lotCode} · {self.originPlace}"


class FireHearth(models.Model):
    PHASE_COLD = "cold"
    PHASE_CHARGING = "charging"
    PHASE_RAMPING = "ramping"
    PHASE_HOLDING = "holding"
    PHASE_DRAWING = "drawing"
    PHASE_CHOICES = [
        (PHASE_COLD, "冷灶"),
        (PHASE_CHARGING, "装料"),
        (PHASE_RAMPING, "升温"),
        (PHASE_HOLDING, "保温"),
        (PHASE_DRAWING, "出胶"),
    ]

    lane = models.PositiveIntegerField("过道号")
    tag = models.CharField("灶牌", max_length=40, unique=True)
    resinGrade = models.CharField("松香品级标签", max_length=80)
    phase = models.CharField(
        "相位",
        max_length=20,
        choices=PHASE_CHOICES,
        default=PHASE_COLD,
    )

    class Meta:
        ordering = ["lane", "tag"]
        verbose_name = "灶台"
        verbose_name_plural = "灶台"

    def __str__(self):
        return f"L{self.lane}-{self.tag}"

    def open_run(self):
        return (
            self.runs.filter(closedAt__isnull=True)
            .select_related("resinLot")
            .order_by("-openedAt", "-id")
            .first()
        )

    def active_seal(self):
        """当前未解除的检修封条（无则 None）。"""
        return (
            self.seals.filter(releasedAt__isnull=True)
            .select_related("placedBy")
            .order_by("-startedAt", "-id")
            .first()
        )


class CookRun(models.Model):
    hearth = models.ForeignKey(
        FireHearth,
        on_delete=models.CASCADE,
        related_name="runs",
        verbose_name="灶台",
    )
    resinLot = models.ForeignKey(
        ResinLot,
        on_delete=models.PROTECT,
        related_name="runs",
        verbose_name="来脂批",
    )
    openedAt = models.DateTimeField("开灶时间")
    closedAt = models.DateTimeField("收灶时间", null=True, blank=True)
    targetSoftPointC = models.DecimalField(
        "目标软化点(℃)", max_digits=6, decimal_places=2
    )

    class Meta:
        ordering = ["-openedAt", "-id"]
        verbose_name = "熬制值守"
        verbose_name_plural = "熬制值守"

    def __str__(self):
        return f"{self.hearth.tag} @ {self.openedAt:%Y-%m-%d %H:%M}"

    @property
    def is_open(self):
        return self.closedAt is None


class SoftPointProbe(models.Model):
    run = models.ForeignKey(
        CookRun,
        on_delete=models.CASCADE,
        related_name="probes",
        verbose_name="值守",
    )
    sampledAt = models.DateTimeField("取样时间")
    softPointC = models.DecimalField("软化点(℃)", max_digits=6, decimal_places=2)
    samplerName = models.CharField("取样人", max_length=80)

    class Meta:
        ordering = ["-sampledAt", "-id"]
        verbose_name = "软化点探针"
        verbose_name_plural = "软化点探针"

    def __str__(self):
        return f"{self.softPointC}℃ by {self.samplerName}"


class MaintenanceSeal(models.Model):
    """检修封条：生效（未解除）期间拦截该灶一切写操作。"""

    hearth = models.ForeignKey(
        FireHearth,
        on_delete=models.CASCADE,
        related_name="seals",
        verbose_name="灶台",
    )
    startedAt = models.DateTimeField("开始时刻")
    plannedReleaseDate = models.DateField("计划解除日")
    faultSummary = models.CharField("故障摘要", max_length=200)
    placedBy = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="placed_seals",
        verbose_name="挂条人",
    )
    releasedAt = models.DateTimeField("实解时刻", null=True, blank=True)

    class Meta:
        ordering = ["-startedAt", "-id"]
        verbose_name = "检修封条"
        verbose_name_plural = "检修封条"
        constraints = [
            models.UniqueConstraint(
                fields=["hearth"],
                condition=models.Q(releasedAt__isnull=True),
                name="uniq_active_seal_per_hearth",
            )
        ]

    def __str__(self):
        state = "在修" if self.is_active else "已解除"
        return f"{self.hearth.tag} · {state} · {self.faultSummary}"

    @property
    def is_active(self):
        return self.releasedAt is None
