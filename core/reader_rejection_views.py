from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import redirect
from django.views.decorators.http import require_POST
from .reader_rejections import acknowledge
from django.core.exceptions import PermissionDenied


@login_required
@require_POST
def acknowledge_notice(request,pk):
    try:acknowledge(request.user,pk)
    except PermissionDenied:raise Http404 from None
    return redirect('needs-attention')
