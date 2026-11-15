from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from .models import PersonalTBRBook
from . import personal_tbr_covers as covers


@login_required
def cover_settings(request,group_slug,month_pk,pk):
    book=get_object_or_404(PersonalTBRBook.objects.select_related(*covers.RELATED),pk=pk,
        personal_tbr__enrollment__month_id=month_pk,personal_tbr__enrollment__month__group__slug=group_slug)
    if not covers.can_manage(request.user,book):
        raise PermissionDenied('Only the eligible Reader may manage this Personal TBR cover.')
    if request.method not in {'GET','POST'}:
        return HttpResponse('Use GET or POST.',status=405)
    form=covers.CoverForm(request.POST or None,request.FILES or None,initial={'version':covers.cover_version(book,request.user)})
    if request.method=='POST' and form.is_valid():
        try:
            covers.update_cover(actor=request.user,book=book,version=form.cleaned_data['version'],
                upload=form.cleaned_data.get('artwork'),remove=request.POST.get('action')=='remove')
        except ValidationError as exc:
            form.add_error(None,exc)
        except (DatabaseError,OSError):
            form.add_error(None,'The cover could not be saved. Existing artwork was preserved. Reload before trying again.')
        else:
            messages.success(request,'Custom cover removed; fallback artwork restored.' if request.POST.get('action')=='remove' else 'Custom cover saved. Your selected book and TBR lock are unchanged.')
            return redirect('personal-tbr-detail',group_slug=group_slug,month_pk=month_pk)
    return render(request,'core/personal_tbr_cover.html',{'book':book,'month':book.personal_tbr.enrollment.month,'form':form})
