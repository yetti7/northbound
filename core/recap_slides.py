"""Presentation-only conversion of already-authorized Chronicle cards to slides."""
READING = {'entries', 'pages', 'works'}
POINTS = {'base_pages', 'modifier_pages', 'total_pages', 'team_points'}


def _chunks(items, size):
    return [items[index:index + size] for index in range(0, len(items), size)]


SLIDE_ORDER = ('opening', 'overview', 'reading', 'points', 'achievements', 'books', 'teams', 'contributions', 'summary')


def team_bars(rows):
    maximum = max((row['points'] for row in rows), default=0)
    return [{**row, 'bar_width': round(max(0, row['points']) / maximum * 100, 2) if maximum > 0 else 0} for row in rows]


def build_wrapped_slides(recap):
    if not recap or not recap['sections']:
        return []
    modes = sorted({section['mode'] for section in recap['sections']})
    slides = [{'kind': 'opening', 'title': recap['heading'],
        'mode': modes[0] if len(modes) == 1 else 'Progress Snapshots & Final Wrapped',
        'perspective': {'reader':'My Reading','team':'Team Wide','challenge':'Challenge Wide'}[recap['perspective']], 'period': recap['period']}]
    for summary in _chunks(recap['summary'], 4):
        slides.append({'kind': 'overview', 'title': 'Across selected Challenges', 'cards': summary,
                       'period': recap['period'], 'mode': 'Authorized results'})
    for section in recap['sections']:
        context = {key: section[key] for key in ['subject','challenge','group','mode','timezone','from_date','to_date']}
        context['period'] = recap['period']
        reading = [c for c in section['cards'] if c['key'] in READING or c['key'].startswith('format_')]
        points = [c for c in section['cards'] if c['key'] in POINTS]
        achievements = [c for c in section['cards'] if c not in reading and c not in points]
        for kind, title, cards in [('reading','Reading highlights',reading),
                                  ('points','Credit where it is earned',points),
                                  ('achievements','Achievements along the way',achievements)]:
            for chunk in _chunks(cards, 4):
                slides.append({**context, 'kind': kind, 'title': title, 'cards': chunk})
        if section.get('books'):
            slides.append({**context, 'kind': 'books', 'title': 'Reading Highlights', 'books': section['books']})
        for chunk in _chunks(section['teams'], 6):
            slides.append({**context, 'kind':'teams', 'title':'Team results', 'rows':team_bars(chunk),
                'caption':'Current authorized results for this reporting period. No historical placement or tie-breaker is inferred.'})
        for chunk in _chunks(section['contributions'], 6):
            slides.append({**context, 'kind':'contributions', 'title':'Reader contributions', 'rows':chunk,
                'caption':'Visible Reader contributions only; these rows may not account for the full team total.'})
    slides.append({'kind': 'summary', 'title': recap['heading'], 'period': recap['period'],
                   'mode': 'Northbound Chronicle', 'sections': [
                       {**section, 'team_bars': team_bars(section['teams'])} for section in recap['sections']]})
    # Ordering is independent of component rendering. Move 'summary' in this tuple
    # to change its position without changing the card or reporting service.
    return sorted(slides, key=lambda slide: SLIDE_ORDER.index(slide['kind']))
