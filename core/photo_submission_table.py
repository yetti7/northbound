"""Database-paginated IDs for the existing Platform All Submissions table."""
from django.core.paginator import Paginator
from django.core.exceptions import PermissionDenied
from .prompt_reservations import category_available
from django.db.models import Q,Value,CharField
from .models import BookSubmission,ScavengerPhotoSubmission


def submission_page(month,actor,*,query,status,size,number):
    if not actor.is_authenticated or not actor.is_active or not actor.is_superuser:
        raise PermissionDenied('Platform All Submissions authority is required.')
    operational=category_available(month,'scavenger')
    books=BookSubmission.objects.filter(month=month)
    photos=ScavengerPhotoSubmission.objects.filter(month=month)
    if status=='voided':books=books.filter(is_removed=True);photos=photos.filter(status='voided')
    elif status=='withdrawn':books=books.none();photos=photos.filter(status='withdrawn')
    elif status!='all':books=books.filter(is_removed=False,status=status);photos=photos.filter(status=status)
    if query:
        books=books.filter(Q(participant__user__username__icontains=query)|Q(title__icontains=query))
        photos=photos.filter(Q(participant__user__username__icontains=query)|Q(prompt_claim__selection_evidence__description__icontains=query)|Q(prompt_claim__prompt__description__icontains=query))
    b=books.order_by().annotate(_row_kind=Value('book',output_field=CharField())).values('pk','submitted_at','_row_kind')
    p=photos.order_by().annotate(_row_kind=Value('photo',output_field=CharField())).values('pk','submitted_at','_row_kind')
    page=Paginator(b.union(p).order_by('-submitted_at','-pk','_row_kind'),size).get_page(number)
    ids=list(page.object_list)
    book_rows=BookSubmission.objects.filter(pk__in=[row['pk'] for row in ids if row['_row_kind']=='book']).select_related('month__group','participant__user__northbound_profile','catalog_book','catalog_edition').in_bulk()
    photo_rows=ScavengerPhotoSubmission.objects.filter(pk__in=[row['pk'] for row in ids if row['_row_kind']=='photo']).select_related('month__group','participant__user__northbound_profile','prompt_claim__prompt').defer('discord_message_url').in_bulk()
    for photo in photo_rows.values():
        photo.is_photo=True;photo.is_removed=photo.status=='voided'
        photo.can_correct=operational and month.status in {'active','finalizing'} and photo.status in {'pending','approved','rejected'}
        photo.can_void=operational and month.status=='active' and photo.status=='approved'
        photo.can_restore=operational and month.status=='active' and photo.status=='voided'
    page.object_list=[(book_rows if row['_row_kind']=='book' else photo_rows)[row['pk']] for row in ids]
    return page
