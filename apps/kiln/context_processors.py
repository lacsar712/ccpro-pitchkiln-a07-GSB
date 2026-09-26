from .services.maintenance import active_seal_count


def maintenance_seals(request):
    """班次条检修封条角标：未解除封条数。"""
    if not request.user.is_authenticated:
        return {}
    return {"active_seal_count": active_seal_count()}
