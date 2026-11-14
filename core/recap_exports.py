"""Export presentation DTOs from already-authorized recap data; no queries/scoring."""
from copy import deepcopy


def export_data(recap, slides):
    if not recap or not slides:
        return None
    perspective = recap['perspective']
    def section(value):
        result = {key: deepcopy(value.get(key, [] if key in ('cards', 'books', 'teams') else ''))
                  for key in ('challenge', 'group', 'subject', 'mode', 'from_date', 'to_date', 'cards', 'books', 'teams')}
        # Never export other Readers' contribution identities, even when visible on screen.
        return result
    clean = []
    for slide in slides:
        item = {key: deepcopy(slide[key]) for key in ('kind', 'title', 'mode', 'period', 'cards', 'books', 'caption') if key in slide}
        if slide['kind'] in ('opening', 'summary'):
            item['title'] = 'Northbound Wrapped'
        item['context'] = section(slide)
        if slide['kind'] == 'teams':
            item['teams'] = deepcopy(slide['rows'])
        elif slide['kind'] == 'contributions':
            item['cards'] = [{'label': 'Reader contribution', 'value': row['points']} for row in slide['rows']]
        clean.append(item)
    return {'perspective': perspective, 'period': recap['period'], 'slides': clean,
            'sections': [section(s) for s in recap['sections']], 'branding': deepcopy(recap.get('branding', {}))}
