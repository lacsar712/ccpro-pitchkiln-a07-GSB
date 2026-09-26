from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("floor/grid/", views.floor_grid_partial, name="floor_grid"),
    path("hearth/<int:pk>/drawer/", views.hearth_drawer, name="hearth_drawer"),
    path("hearth/<int:pk>/phase/", views.change_phase, name="change_phase"),
    path("hearth/<int:pk>/probe/", views.add_probe, name="add_probe"),
    path("hearth/<int:pk>/open-run/", views.open_run, name="open_run"),
    path("hearth/<int:pk>/close-run/", views.close_run, name="close_run"),
    path("hearth/<int:pk>/seal/", views.hang_seal, name="hang_seal"),
    path("hearth/<int:pk>/release-seal/", views.release_seal, name="release_seal"),
    path("seals/", views.seal_list, name="seal_list"),
    path("resin-lots/", views.resin_lot_feed, name="resin_lot_feed"),
]
