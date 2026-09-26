from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import CookRun, FireHearth, MaintenanceSeal, ResinLot
from .services import maintenance
from .services.floor_rules import change_hearth_phase


def _make_lot(code="脂-试-001"):
    return ResinLot.objects.create(
        lotCode=code,
        originPlace="松脂坳东沟",
        arrivalKg=Decimal("100.00"),
        receivedAt=timezone.now(),
    )


def _make_hearth(tag, phase=FireHearth.PHASE_COLD, lane=1):
    return FireHearth.objects.create(
        lane=lane, tag=tag, resinGrade="一级脂", phase=phase
    )


def _open_run(hearth, lot):
    return CookRun.objects.create(
        hearth=hearth,
        resinLot=lot,
        openedAt=timezone.now(),
        targetSoftPointC=Decimal("88.00"),
    )


def _hang(hearth, user, **kw):
    defaults = {
        "startedAt": timezone.now(),
        "plannedReleaseDate": timezone.localdate() + timedelta(days=2),
        "faultSummary": "炉膛耐火砖开裂",
        "sealedBy": user,
    }
    defaults.update(kw)
    return maintenance.hang_seal(hearth, **defaults)


class HangSealRuleTests(TestCase):
    """挂条规则：按钮藏了后端仍强制校验。"""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.user = User.objects.create_user("worker", password="pw")
        cls.lot = _make_lot()

    def test_hang_on_cold_idle_hearth_ok(self):
        hearth = _make_hearth("冷灶-甲")
        seal = _hang(hearth, self.user)
        self.assertIsNone(seal.releasedAt)
        self.assertEqual(seal.sealedBy, self.user)
        self.assertEqual(maintenance.active_seal_for(hearth), seal)

    def test_hang_on_non_cold_hearth_rejected(self):
        for phase in (
            FireHearth.PHASE_CHARGING,
            FireHearth.PHASE_RAMPING,
            FireHearth.PHASE_HOLDING,
            FireHearth.PHASE_DRAWING,
        ):
            hearth = _make_hearth(f"热灶-{phase}", phase=phase)
            with self.assertRaises(ValidationError):
                _hang(hearth, self.user)
        self.assertEqual(MaintenanceSeal.objects.count(), 0)

    def test_hang_with_open_run_rejected_even_if_cold(self):
        hearth = _make_hearth("冷灶-乙")
        _open_run(hearth, self.lot)  # 数据异常：冷灶却有未收灶值守
        with self.assertRaises(ValidationError):
            _hang(hearth, self.user)

    def test_second_active_seal_rejected(self):
        hearth = _make_hearth("冷灶-丙")
        _hang(hearth, self.user)
        with self.assertRaises(ValidationError):
            _hang(hearth, self.user)

    def test_db_constraint_blocks_second_active_seal(self):
        hearth = _make_hearth("冷灶-丁")
        _hang(hearth, self.user)
        with self.assertRaises(IntegrityError):
            MaintenanceSeal.objects.create(
                hearth=hearth,
                startedAt=timezone.now(),
                plannedReleaseDate=timezone.localdate(),
                faultSummary="绕过服务层直插",
                sealedBy=self.user,
            )

    def test_rehang_allowed_after_release(self):
        hearth = _make_hearth("冷灶-戊")
        seal = _hang(hearth, self.user)
        maintenance.release_seal(seal)
        again = _hang(hearth, self.user, faultSummary="二次故障")
        self.assertTrue(again.is_active)
        self.assertEqual(maintenance.active_seal_for(hearth), again)


class HangSealViewTests(TestCase):
    """POST 入口：藏按钮不等于放行。"""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.user = User.objects.create_user("worker", password="pw")
        cls.lot = _make_lot("脂-试-002")

    def setUp(self):
        self.client.login(username="worker", password="pw")

    def _payload(self):
        return {
            "startedAt": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
            "plannedReleaseDate": (
                timezone.localdate() + timedelta(days=2)
            ).strftime("%Y-%m-%d"),
            "faultSummary": "炉膛裂缝",
        }

    def test_post_hang_on_cold_hearth(self):
        hearth = _make_hearth("冷灶-POST")
        resp = self.client.post(reverse("hang_seal", args=[hearth.pk]), self._payload())
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(MaintenanceSeal.objects.count(), 1)
        seal = MaintenanceSeal.objects.get()
        self.assertEqual(seal.sealedBy, self.user)

    def test_post_hang_on_hot_hearth_fails(self):
        hearth = _make_hearth("热灶-POST", phase=FireHearth.PHASE_HOLDING)
        resp = self.client.post(
            reverse("hang_seal", args=[hearth.pk]), self._payload(), follow=True
        )
        self.assertContains(resp, "仅冷灶可挂检修封条")
        self.assertEqual(MaintenanceSeal.objects.count(), 0)

    def test_post_hang_with_open_run_fails(self):
        hearth = _make_hearth("冷灶-POST2")
        _open_run(hearth, self.lot)
        resp = self.client.post(
            reverse("hang_seal", args=[hearth.pk]), self._payload(), follow=True
        )
        self.assertContains(resp, "未收灶")
        self.assertEqual(MaintenanceSeal.objects.count(), 0)

    def test_post_second_seal_fails(self):
        hearth = _make_hearth("冷灶-POST3")
        _hang(hearth, self.user)
        resp = self.client.post(
            reverse("hang_seal", args=[hearth.pk]), self._payload(), follow=True
        )
        self.assertContains(resp, "不可重复挂条")
        self.assertEqual(MaintenanceSeal.objects.count(), 1)


class SealedHearthBlockTests(TestCase):
    """封条生效时：开灶 / 改相位 / 登记探针 / 收灶全部拦下。"""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.user = User.objects.create_user("worker", password="pw")
        cls.lot = _make_lot("脂-试-003")

    def setUp(self):
        self.client.login(username="worker", password="pw")
        self.hearth = _make_hearth("冷灶-封")
        self.seal = _hang(self.hearth, self.user)

    def test_service_blocks_phase_change(self):
        with self.assertRaises(ValidationError):
            change_hearth_phase(self.hearth, FireHearth.PHASE_CHARGING)
        self.hearth.refresh_from_db()
        self.assertEqual(self.hearth.phase, FireHearth.PHASE_COLD)

    def test_post_phase_change_blocked(self):
        resp = self.client.post(
            reverse("change_phase", args=[self.hearth.pk]),
            {"phase": FireHearth.PHASE_CHARGING},
            follow=True,
        )
        self.assertContains(resp, "在修")
        self.hearth.refresh_from_db()
        self.assertEqual(self.hearth.phase, FireHearth.PHASE_COLD)

    def test_post_open_run_blocked(self):
        resp = self.client.post(
            reverse("open_run", args=[self.hearth.pk]),
            {
                "resinLot": self.lot.pk,
                "openedAt": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
                "targetSoftPointC": "88.00",
            },
            follow=True,
        )
        self.assertContains(resp, "在修")
        self.assertEqual(CookRun.objects.count(), 0)

    def test_post_probe_blocked(self):
        run = _open_run(self.hearth, self.lot)  # 数据异常：封条期间存在未收灶值守
        resp = self.client.post(
            reverse("add_probe", args=[self.hearth.pk]),
            {
                "sampledAt": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
                "softPointC": "90.00",
                "samplerName": "周磊",
            },
            follow=True,
        )
        self.assertContains(resp, "在修")
        self.assertEqual(run.probes.count(), 0)

    def test_post_close_run_blocked(self):
        run = _open_run(self.hearth, self.lot)  # 数据异常：封条期间存在未收灶值守
        resp = self.client.post(
            reverse("close_run", args=[self.hearth.pk]), follow=True
        )
        self.assertContains(resp, "在修")
        run.refresh_from_db()
        self.assertIsNone(run.closedAt)

    def test_drawer_shows_repair_notice_not_forms(self):
        resp = self.client.get(
            reverse("hearth_drawer", args=[self.hearth.pk]), HTTP_HX_REQUEST="true"
        )
        self.assertContains(resp, "在修")
        self.assertContains(resp, "炉膛耐火砖开裂")
        self.assertNotContains(resp, "开新值守")
        self.assertNotContains(resp, "切换相位")

    def test_tile_marks_under_repair(self):
        resp = self.client.get(reverse("home"))
        self.assertContains(resp, "在修")
        self.assertContains(resp, "tile-seal")


class ReleaseSealTests(TestCase):
    """解除：仅主管，写入实解时刻；判定与拦截共用。"""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.staff = User.objects.create_user("boss", password="pw", is_staff=True)
        cls.worker = User.objects.create_user("worker", password="pw")
        cls.lot = _make_lot("脂-试-004")

    def setUp(self):
        self.hearth = _make_hearth("冷灶-解")
        self.seal = _hang(self.hearth, self.worker)

    def test_worker_cannot_release(self):
        self.client.login(username="worker", password="pw")
        resp = self.client.post(
            reverse("release_seal", args=[self.hearth.pk]), follow=True
        )
        self.assertContains(resp, "仅主管可解除")
        self.seal.refresh_from_db()
        self.assertIsNone(self.seal.releasedAt)

    def test_staff_release_writes_released_at(self):
        self.client.login(username="boss", password="pw")
        before = timezone.now()
        resp = self.client.post(
            reverse("release_seal", args=[self.hearth.pk]), follow=True
        )
        self.assertContains(resp, "已解除")
        self.seal.refresh_from_db()
        self.assertIsNotNone(self.seal.releasedAt)
        self.assertGreaterEqual(self.seal.releasedAt, before)
        self.assertIsNone(maintenance.active_seal_for(self.hearth))

    def test_double_release_rejected(self):
        maintenance.release_seal(self.seal)
        with self.assertRaises(ValidationError):
            maintenance.release_seal(self.seal)

    def test_writes_work_again_after_release(self):
        maintenance.release_seal(self.seal)
        change_hearth_phase(self.hearth, FireHearth.PHASE_CHARGING)
        self.hearth.refresh_from_db()
        self.assertEqual(self.hearth.phase, FireHearth.PHASE_CHARGING)

    def test_release_from_seal_list_redirects_back(self):
        self.client.login(username="boss", password="pw")
        resp = self.client.post(
            reverse("release_seal", args=[self.hearth.pk]),
            {"next": reverse("seal_list")},
        )
        self.assertRedirects(resp, reverse("seal_list"))


class SealListViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.user = User.objects.create_user("worker", password="pw")

    def test_list_shows_active_and_history(self):
        hearth = _make_hearth("冷灶-列表")
        seal = _hang(hearth, self.user)
        old = _make_hearth("冷灶-历史")
        old_seal = _hang(old, self.user, faultSummary="旧故障")
        maintenance.release_seal(old_seal)

        self.client.login(username="worker", password="pw")
        resp = self.client.get(reverse("seal_list"))
        self.assertContains(resp, "在修")
        self.assertContains(resp, "炉膛耐火砖开裂")
        self.assertContains(resp, "已解除")
        self.assertContains(resp, "旧故障")
        self.assertTrue(seal.is_active)
