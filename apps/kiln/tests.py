from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import CookRun, FireHearth, MaintenanceSeal, ResinLot
from .services.floor_rules import change_hearth_phase
from .services.maintenance import (
    active_seal_for,
    place_maintenance_seal,
    release_maintenance_seal,
    seal_placement_block_reason,
)


def _make_lot(code="脂-测试-001"):
    return ResinLot.objects.create(
        lotCode=code,
        originPlace="松脂坳东沟",
        arrivalKg=Decimal("100.00"),
        receivedAt=timezone.now(),
    )


def _make_hearth(tag="测-甲", phase=FireHearth.PHASE_COLD):
    return FireHearth.objects.create(lane=9, tag=tag, resinGrade="特级脂", phase=phase)


def _seal_kwargs(hearth, user):
    return dict(
        hearth=hearth,
        placed_by=user,
        started_at=timezone.now() - timedelta(hours=1),
        planned_release_date=(timezone.now() + timedelta(days=2)).date(),
        fault_summary="炉膛耐火砖开裂",
    )


def _seal_post():
    return {
        "startedAt": timezone.now().strftime("%Y-%m-%dT%H:%M"),
        "plannedReleaseDate": (timezone.now() + timedelta(days=1)).strftime("%Y-%m-%d"),
        "faultSummary": "直接 POST 挂条",
    }


class MaintenanceSealRuleTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.staff = User.objects.create_user("boss", password="x", is_staff=True)
        self.worker = User.objects.create_user("w1", password="x")

    def test_place_on_cold_idle_hearth_ok(self):
        h = _make_hearth()
        seal = place_maintenance_seal(**_seal_kwargs(h, self.worker))
        self.assertTrue(seal.is_active)
        self.assertEqual(active_seal_for(h).pk, seal.pk)

    def test_place_on_non_cold_hearth_rejected(self):
        h = _make_hearth(phase=FireHearth.PHASE_RAMPING)
        self.assertIsNotNone(seal_placement_block_reason(h))
        with self.assertRaises(ValidationError):
            place_maintenance_seal(**_seal_kwargs(h, self.worker))
        self.assertIsNone(active_seal_for(h))

    def test_place_on_hearth_with_open_run_rejected(self):
        h = _make_hearth()
        CookRun.objects.create(
            hearth=h,
            resinLot=_make_lot(),
            openedAt=timezone.now(),
            targetSoftPointC=Decimal("88.00"),
        )
        self.assertIsNotNone(seal_placement_block_reason(h))
        with self.assertRaises(ValidationError):
            place_maintenance_seal(**_seal_kwargs(h, self.worker))
        self.assertIsNone(active_seal_for(h))

    def test_duplicate_active_seal_rejected_and_db_constraint(self):
        h = _make_hearth()
        place_maintenance_seal(**_seal_kwargs(h, self.worker))
        with self.assertRaises(ValidationError):
            place_maintenance_seal(**_seal_kwargs(h, self.worker))
        # 数据库部分唯一约束兜底：绕开服务层直插也失败
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                MaintenanceSeal.objects.create(
                    hearth=h,
                    placedBy=self.worker,
                    startedAt=timezone.now(),
                    plannedReleaseDate=timezone.now().date(),
                    faultSummary="绕开服务层直插",
                )

    def test_sealed_hearth_blocks_phase_change_service(self):
        h = _make_hearth()
        place_maintenance_seal(**_seal_kwargs(h, self.worker))
        with self.assertRaises(ValidationError):
            change_hearth_phase(h, FireHearth.PHASE_CHARGING)
        h.refresh_from_db()
        self.assertEqual(h.phase, FireHearth.PHASE_COLD)

    def test_release_writes_released_at_and_unblocks(self):
        h = _make_hearth()
        place_maintenance_seal(**_seal_kwargs(h, self.worker))
        release_maintenance_seal(hearth=h)
        self.assertIsNone(active_seal_for(h))
        self.assertIsNotNone(h.seals.first().releasedAt)
        # 解除后写操作恢复
        change_hearth_phase(h, FireHearth.PHASE_CHARGING)
        h.refresh_from_db()
        self.assertEqual(h.phase, FireHearth.PHASE_CHARGING)


class MaintenanceSealViewTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.staff = User.objects.create_user("boss", password="x", is_staff=True)
        self.worker = User.objects.create_user("w1", password="x")
        self.lot = _make_lot()

    def _sealed_hearth(self, tag="测-封"):
        h = _make_hearth(tag=tag)
        place_maintenance_seal(**_seal_kwargs(h, self.worker))
        return h

    def test_place_seal_view_ok_on_cold_idle_hearth(self):
        self.client.force_login(self.worker)
        h = _make_hearth(tag="测-冷")
        resp = self.client.post(reverse("place_seal", args=[h.pk]), _seal_post())
        self.assertEqual(resp.status_code, 302)
        self.assertIsNotNone(active_seal_for(h))

    def test_place_seal_view_on_non_cold_hearth_fails(self):
        """按钮藏了，直接 POST 给非冷灶挂条仍失败。"""
        self.client.force_login(self.worker)
        h = _make_hearth(tag="测-热", phase=FireHearth.PHASE_HOLDING)
        resp = self.client.post(reverse("place_seal", args=[h.pk]), _seal_post())
        self.assertEqual(resp.status_code, 302)
        self.assertIsNone(active_seal_for(h))

    def test_place_seal_view_on_hearth_with_open_run_fails(self):
        """按钮藏了，直接 POST 给有未收灶值守的灶挂条仍失败。"""
        self.client.force_login(self.worker)
        h = _make_hearth(tag="测-忙")
        CookRun.objects.create(
            hearth=h,
            resinLot=self.lot,
            openedAt=timezone.now(),
            targetSoftPointC=Decimal("88.00"),
        )
        resp = self.client.post(reverse("place_seal", args=[h.pk]), _seal_post())
        self.assertEqual(resp.status_code, 302)
        self.assertIsNone(active_seal_for(h))

    def test_place_seal_view_duplicate_fails(self):
        self.client.force_login(self.worker)
        h = self._sealed_hearth()
        resp = self.client.post(reverse("place_seal", args=[h.pk]), _seal_post())
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(h.seals.filter(releasedAt__isnull=True).count(), 1)

    def test_write_ops_blocked_while_sealed(self):
        """在修期间：开灶 / 改相位 / 登记探针 / 收灶 全部拦下。"""
        self.client.force_login(self.worker)
        h = self._sealed_hearth()

        # 开灶
        self.client.post(
            reverse("open_run", args=[h.pk]),
            {
                "resinLot": self.lot.pk,
                "openedAt": timezone.now().strftime("%Y-%m-%dT%H:%M"),
                "targetSoftPointC": "88.00",
            },
        )
        self.assertIsNone(h.open_run())

        # 改相位
        self.client.post(reverse("change_phase", args=[h.pk]), {"phase": "charging"})
        h.refresh_from_db()
        self.assertEqual(h.phase, FireHearth.PHASE_COLD)

        # 构造数据异常场景（在修灶上存在未收值守），探针与收灶仍须拦截
        run = CookRun.objects.create(
            hearth=h,
            resinLot=self.lot,
            openedAt=timezone.now(),
            targetSoftPointC=Decimal("88.00"),
        )
        self.client.post(
            reverse("add_probe", args=[h.pk]),
            {
                "sampledAt": timezone.now().strftime("%Y-%m-%dT%H:%M"),
                "softPointC": "90.00",
                "samplerName": "值守周磊",
            },
        )
        self.assertEqual(run.probes.count(), 0)

        self.client.post(reverse("close_run", args=[h.pk]))
        run.refresh_from_db()
        self.assertIsNone(run.closedAt)

    def test_release_requires_staff(self):
        h = self._sealed_hearth()
        self.client.force_login(self.worker)
        self.client.post(reverse("release_seal", args=[h.pk]))
        self.assertIsNotNone(active_seal_for(h))

        self.client.force_login(self.staff)
        self.client.post(reverse("release_seal", args=[h.pk]))
        self.assertIsNone(active_seal_for(h))
        self.assertIsNotNone(h.seals.first().releasedAt)

    def test_board_tile_and_drawer_show_seal_marker(self):
        self.client.force_login(self.worker)
        h = self._sealed_hearth()
        resp = self.client.get(reverse("home"))
        self.assertContains(resp, "在修")

        resp = self.client.get(
            reverse("hearth_drawer", args=[h.pk]), HTTP_HX_REQUEST="true"
        )
        self.assertContains(resp, "在修")
        self.assertContains(resp, "炉膛耐火砖开裂")
        # 非主管看不到解除按钮，只看到提示
        self.assertContains(resp, "仅主管可解除封条")

        self.client.force_login(self.staff)
        resp = self.client.get(
            reverse("hearth_drawer", args=[h.pk]), HTTP_HX_REQUEST="true"
        )
        self.assertContains(resp, "解除封条")
