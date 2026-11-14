"""Safe, scoped Photo timeline cards; the caller first authorizes the object."""
import json
from django.core.paginator import Paginator
from .models import AuditEvent
from .photo_actions import correction_authority,credit_authority


def photo_timeline(request,photo):
    events=AuditEvent.objects.filter(group_id=photo.month.group_id,object_type='ScavengerPhotoSubmission',object_id=str(photo.pk),action__startswith='scavenger_photo.').select_related('actor').order_by('-created_at','-pk')
    page=Paginator(events,25).get_page(request.GET.get('activity_page'))
    staff=correction_authority(request.user,photo) or credit_authority(request.user,photo)
    labels={'created':'Submitted','edited':'Pending selection/link edited','corrected':'Staff correction','voided':'Voided','restored':'Restored','prompt_corrected':'Approved prompt corrected'}
    cards=[]
    for event in page:
        try:data=json.loads(event.summary)
        except (ValueError,TypeError):data={}
        suffix=event.action.rsplit('.',1)[-1]
        label=labels.get(suffix,'Photo state updated')
        if suffix=='reviewed':label='Approved' if data.get('decision')=='approved' else 'Rejected'
        if suffix=='reservation_changed' and data.get('after')=='withdrawn':label='Withdrawn'
        detail='Discord link changed; no scoring change.' if suffix=='corrected' else ''
        if suffix=='edited' and isinstance(data.get('before'),dict) and isinstance(data.get('after'),dict):
            before=data['before'].get('selection',{});after=data['after'].get('selection',{})
            if isinstance(before,dict) and isinstance(after,dict) and before.get('prompt_id')!=after.get('prompt_id'):
                detail=f"Prompt #{before.get('number','?')} → Prompt #{after.get('number','?')}."
        if suffix=='prompt_corrected':
            detail=f"Prompt #{data.get('old_number')} → #{data.get('new_number')}; {data.get('old_award')} → {data.get('new_award')} prompt points; net {data.get('impact')} points."
        if suffix in {'voided','restored'} and type(data.get('amount')) is int:
            detail=f"{data['amount']} frozen prompt points; dependent completion credit checked."
        cards.append({'label':label,'timestamp':event.created_at,'actor':event.actor.get_username() if staff and event.actor else '',
                      'detail':detail})
    return {'page':page,'cards':cards}
