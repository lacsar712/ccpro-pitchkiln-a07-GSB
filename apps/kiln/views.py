from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Prefetch
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_http_methods, require_POST

from .forms import (
    MaintenanceSealForm,
    OpenCookRunForm,
    PhaseChangeForm,
    ResinLotForm,
    SoftPointProbeForm,
)
from .models import CookRun, FireHearth, MaintenanceSeal, ResinLot
from .services import maintenance
from .services.floor_rules import change_hearth_phase


def _wants_htmx(request):
    return request.headers.get("HX-Request") == "true"


def _first_error(exc):
    """ValidationError → 第一条中文消息（兼容 field/non-field 两种）。"""
    if hasattr(exc, "message_dict"):
        for errs in exc.message_dict.values():
            if errs:
                return errs[0]
    return exc.messages[0] if exc.messages else str(exc)


def _hearths_for_board():
    return FireHearth.objects.prefetch_related(
        Prefetch(
            "runs",
            queryset=CookRun.objects.filter(closedAt__isnull=True)
            .select_related("resinLot")
            .prefetch_related("probes"),
            to_attr="open_runs_cache",
        ),
        Prefetch(
            "maintenance_seals",
            queryset=MaintenanceSeal.objects.filter(releasedAt__isnull=True),
            to_attr="active_seals_cache",
        ),
    ).order_by("lane", "tag")


def _board_context():
    hearths = list(_hearths_for_board())
    lanes = {}
    for h in hearths:
        lanes.setdefault(h.lane, []).append(h)
    phase_legend = [
        (key, label, sum(1 for h in hearths if h.phase == key))
        for key, label in FireHearth.PHASE_CHOICES
    ]
    return {
        "hearths": hearths,
        "lanes": sorted(lanes.items()),
        "phase_legend": phase_legend,
    }


def _drawer_context(hearth):
    open_run = hearth.open_run()
    probes = []
    if open_run:
        probes = list(open_run.probes.order_by("-sampledAt", "-id"))
    active_seal = maintenance.active_seal_for(hearth)
    can_hang = (
        active_seal is None
        and open_run is None
        and hearth.phase == FireHearth.PHASE_COLD
    )
    return {
        "hearth": hearth,
        "open_run": open_run,
        "probes": probes,
        "active_seal": active_seal,
        "seal_form": MaintenanceSealForm() if can_hang else None,
        "phase_form": PhaseChangeForm(hearth=hearth),
        "probe_form": SoftPointProbeForm() if open_run else None,
        "open_run_form": OpenCookRunForm(hearth=hearth) if open_run is None else None,
    }


def _write_response(request, hearth):
    """写操作后的统一返回：HTMX 重绘抽屉并刷新看板，否则回首页抽屉。"""
    if _wants_htmx(request):
        hearth.refresh_from_db()
        resp = render(request, "floor/_drawer.html", _drawer_context(hearth))
        resp["HX-Trigger"] = "floor-refresh"
        return resp
    return redirect(f"/?hearth={hearth.pk}")


def _blocked_response(request, hearth, exc):
    """封条拦截：中文说明「在修」并回写抽屉。"""
    messages.error(request, _first_error(exc))
    return _write_response(request, hearth)


@login_required
def home(request):
    ctx = _board_context()
    drawer_pk = request.GET.get("hearth")
    if drawer_pk:
        try:
            hearth = FireHearth.objects.get(pk=drawer_pk)
            ctx.update(_drawer_context(hearth))
            ctx["drawer_open"] = True
        except (FireHearth.DoesNotExist, ValueError):
            ctx["drawer_open"] = False
    else:
        ctx["drawer_open"] = False
    return render(request, "floor/board.html", ctx)


@login_required
def floor_grid_partial(request):
    html = render_to_string("floor/_grid.html", _board_context(), request=request)
    return HttpResponse(html)


@login_required
def hearth_drawer(request, pk):
    hearth = get_object_or_404(FireHearth, pk=pk)
    ctx = _drawer_context(hearth)
    if _wants_htmx(request):
        return render(request, "floor/_drawer.html", ctx)
    return redirect(f"/?hearth={pk}")


@login_required
@require_POST
def change_phase(request, pk):
    hearth = get_object_or_404(FireHearth, pk=pk)
    form = PhaseChangeForm(request.POST, hearth=hearth)
    if form.is_valid():
        try:
            change_hearth_phase(hearth, form.cleaned_data["phase"])
            messages.success(request, f"灶牌 {hearth.tag} 相位已更新")
        except ValidationError as exc:
            messages.error(request, _first_error(exc))
    else:
        err = form.errors.get("phase")
        messages.error(request, err[0] if err else "相位切换失败")

    return _write_response(request, hearth)


@login_required
@require_POST
def add_probe(request, pk):
    hearth = get_object_or_404(FireHearth, pk=pk)
    try:
        maintenance.assert_not_sealed(hearth)
    except ValidationError as exc:
        return _blocked_response(request, hearth, exc)

    open_run = hearth.open_run()
    if open_run is None:
        messages.error(request, "没有进行中的值守，无法登记探针")
        return redirect(f"/?hearth={pk}")

    form = SoftPointProbeForm(request.POST)
    if form.is_valid():
        probe = form.save(commit=False)
        probe.run = open_run
        probe.save()
        messages.success(request, f"已登记探针 {probe.softPointC}℃")
    else:
        messages.error(request, "探针登记失败，请检查输入")

    return _write_response(request, hearth)


@login_required
@require_POST
def open_run(request, pk):
    hearth = get_object_or_404(FireHearth, pk=pk)
    try:
        maintenance.assert_not_sealed(hearth)
    except ValidationError as exc:
        return _blocked_response(request, hearth, exc)

    form = OpenCookRunForm(request.POST, hearth=hearth)
    if form.is_valid():
        run = form.save(commit=False)
        run.hearth = hearth
        run.save()
        if hearth.phase == FireHearth.PHASE_COLD:
            hearth.phase = FireHearth.PHASE_CHARGING
            hearth.save(update_fields=["phase"])
        messages.success(request, "新值守已开灶")
    else:
        for errs in form.errors.values():
            for e in errs:
                messages.error(request, e)
            break

    return _write_response(request, hearth)


@login_required
@require_POST
def close_run(request, pk):
    hearth = get_object_or_404(FireHearth, pk=pk)
    try:
        maintenance.assert_not_sealed(hearth)
    except ValidationError as exc:
        return _blocked_response(request, hearth, exc)

    open_run = hearth.open_run()
    if open_run is None:
        messages.error(request, "没有进行中的值守可收灶")
    else:
        open_run.closedAt = timezone.now()
        open_run.save(update_fields=["closedAt"])
        hearth.phase = FireHearth.PHASE_COLD
        hearth.save(update_fields=["phase"])
        messages.success(request, "值守已收灶，灶台回冷灶")

    return _write_response(request, hearth)


@login_required
@require_POST
def hang_seal(request, pk):
    """挂检修封条：按钮藏了后端仍强制校验冷灶 / 无未收灶值守 / 无未解除封条。"""
    hearth = get_object_or_404(FireHearth, pk=pk)
    form = MaintenanceSealForm(request.POST)
    if form.is_valid():
        try:
            maintenance.hang_seal(
                hearth,
                startedAt=form.cleaned_data["startedAt"],
                plannedReleaseDate=form.cleaned_data["plannedReleaseDate"],
                faultSummary=form.cleaned_data["faultSummary"],
                sealedBy=request.user,
            )
            messages.success(request, f"灶牌 {hearth.tag} 已挂检修封条，写操作全部拦截")
        except ValidationError as exc:
            messages.error(request, _first_error(exc))
    else:
        for errs in form.errors.values():
            for e in errs:
                messages.error(request, e)
            break

    return _write_response(request, hearth)


@login_required
@require_POST
def release_seal(request, pk):
    """主管解除封条：写入实解时刻。判定与写操作拦截共用 active_seal_for。"""
    hearth = get_object_or_404(FireHearth, pk=pk)
    if not request.user.is_staff:
        messages.error(request, "仅主管可解除检修封条")
        return _write_response(request, hearth)

    seal = maintenance.active_seal_for(hearth)
    if seal is None:
        messages.error(request, "该灶没有未解除的检修封条")
    else:
        try:
            maintenance.release_seal(seal)
            messages.success(request, f"灶牌 {hearth.tag} 检修封条已解除")
        except ValidationError as exc:
            messages.error(request, _first_error(exc))

    if _wants_htmx(request):
        return _write_response(request, hearth)
    nxt = request.POST.get("next", "")
    if url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
        return redirect(nxt)
    return _write_response(request, hearth)


@login_required
def seal_list(request):
    """检修封条总览：未解除的在前，历史已解除的列后。"""
    seals = MaintenanceSeal.objects.select_related("hearth", "sealedBy")
    return render(
        request,
        "floor/seals.html",
        {
            "active_seals": seals.filter(releasedAt__isnull=True),
            "released_seals": seals.filter(releasedAt__isnull=False)[:40],
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def resin_lot_feed(request):
    if request.method == "POST":
        form = ResinLotForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "来脂批已登记")
            return redirect("resin_lot_feed")
    else:
        form = ResinLotForm(
            initial={
                "receivedAt": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
            }
        )

    lots = ResinLot.objects.all()[:40]
    return render(request, "resin/feed.html", {"lots": lots, "form": form})
