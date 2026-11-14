"""Private Reader-only Photo history. Never gated by operational activation."""
from django.core.paginator import Paginator
from .models import ScavengerPhotoSubmission
from .photo_submissions import can_edit_pending_photo
from .prompt_reservations import category_available


def reader_photo_history(request, *, month=None):
    query = ScavengerPhotoSubmission.objects.filter(participant__user=request.user).select_related(
        'participant__user', 'month__group', 'prompt_claim__prompt', 'prompt_claim__category')
    if month:
        query = query.filter(month=month)
    page = Paginator(query.order_by('-submitted_at','-pk'),25).get_page(request.GET.get('photo_page'))
    availability = {}
    editability = {}
    cards = []
    for photo in page.object_list:
        if photo.month_id not in availability:
            availability[photo.month_id] = category_available(photo.month, 'scavenger')
        claim = getattr(photo, 'prompt_claim', None)
        if photo.status == 'pending' and photo.month_id not in editability:
            editability[photo.month_id] = availability[photo.month_id] and can_edit_pending_photo(request.user,photo)
        cards.append({'photo':photo, 'number':claim.selection_evidence.get('number',claim.prompt.number) if claim else '',
            'description':claim.selection_evidence.get('description',claim.prompt.description) if claim else 'Prompt history unavailable',
            'editable': photo.status == 'pending' and editability.get(photo.month_id,False)})
    params=request.GET.copy();params.pop('photo_page',None)
    return {'photo_cards':cards,'photo_page':page,'photo_query':params.urlencode()}
