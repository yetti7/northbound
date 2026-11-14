"""Read-only, scoped presentation of correction effects and existing audit evidence."""
import json
import re


IDENTITY_FIELDS = (
    ('title', 'Title'), ('author', 'Author'), ('book_format', 'Format'),
    ('catalog_book_id', 'Catalog work record'), ('catalog_edition_id', 'Selected edition record'),
    ('scoring_catalog_edition_id', 'Scoring edition record'),
    ('approved_pages', 'Approved base pages'), ('bonus_pages', 'Bonus pages'),
    ('final_scored_pages', 'Scored pages'), ('verification_method', 'Verification'),
    ('status', 'Status'), ('completed_on', 'Completion date'),
)


def operation_label(changes, workflow):
    if changes.get('association_mode') == 'only':
        return 'Link Only — preserve pages and awards'
    if 'catalog_edition_id' in changes or changes.get('association_mode') == 'update':
        return 'Update Book Information — recalculate applicable scores and credit'
    return {'book': 'Fix Book Information', 'pages': 'Fix Pages / Themes',
            'editor': 'Submission correction'}.get(workflow, 'Advanced Corrections')


def preview_effects(preview):
    before, after = preview['before'], preview['after']
    effects = []
    for table, label in [('PersonalTBRMatch', 'Personal TBR'), ('BotmMatch', 'BOTM')]:
        for state, name in [(before, 'Before'), (after, 'After')]:
            rows = [r for r in state[table] if r['status'] == 'confirmed' and r['is_qualifying']]
            effects.append({'label': f'{label} qualifying matches — {name}', 'value': len(rows)})
    for table, field, label in [('PersonalTBRMatch', 'personal_tbr_book_id', 'Personal TBR'),
                                ('BotmMatch', 'botm_book_id', 'BOTM')]:
        old = {(r['submission_id'], r[field]): (r['status'], r['is_qualifying']) for r in before[table]}
        new = {(r['submission_id'], r[field]): (r['status'], r['is_qualifying']) for r in after[table]}
        def describe(value):
            if value is None:
                return 'No match'
            return value[0].replace('_', ' ') + ('; qualifying' if value[1] else '; not qualifying')
        for key in sorted(set(old) | set(new)):
            if old.get(key) != new.get(key):
                effects.append({'label': f'{label}: submission #{key[0]}, selection #{key[1]}',
                                'value': f'{describe(old.get(key))} → {describe(new.get(key))}'})
    for table, label in [('PersonalTBRCompletionAward', 'TBR completion awards'),
                         ('BotmCompletionAward', 'BOTM completion awards')]:
        for state, name in [(before, 'Before'), (after, 'After')]:
            rows = [r for r in state[table] if r['is_qualifying']]
            effects.append({'label': f'{label} — {name}', 'value': sum(r['bonus_amount_snapshot'] for r in rows)})
    for state, name in [(before, 'Before'), (after, 'After')]:
        amounts = {}
        for row in state['ModifierProvenance']:
            if row['is_active']:
                amounts[row['source_type']] = amounts.get(row['source_type'], 0) + row['amount']
        effects.append({'label': f'Active Reader bonuses — {name}', 'value':
                        ', '.join(f'{key.replace("_", " ")}: {value} pages' for key, value in sorted(amounts.items())) or 'None'})
    for state, name in [(before, 'Before'), (after, 'After')]:
        prompt_state = state.get('prompt_reservations', {})
        for kind, label in [('bingo', 'Bingo'), ('scavenger', 'Scavenger Hunt')]:
            category_ids = {row['id'] for row in prompt_state.get('categories', []) if row['kind'] == kind}
            claims = [row for row in prompt_state.get('claims', []) if row['submission_id'] == prompt_state.get('submission_id') and row['is_current'] and row['category_id'] in category_ids]
            if claims:
                effects.append({'label': f'{label} qualification — {name}', 'value': ', '.join(row['state'].replace('_', ' ') for row in claims)})
            awards = [row for row in prompt_state.get('completions', []) if row['category_id'] in category_ids and row['is_active']]
            if awards:
                effects.append({'label': f'{label} completion bonus — {name}', 'value': sum(row['bonus_amount_snapshot'] for row in awards)})
    return effects


def preview_editions(preview):
    from .models import CatalogEdition
    fields = [('catalog_edition_id', 'Selected Hardcover edition'),
              ('scoring_catalog_edition_id', 'Scoring Hardcover edition')]
    ids = {preview[state]['submission'].get(field) for state in ('before', 'after') for field, _ in fields}
    editions = {e.pk: e for e in CatalogEdition.objects.filter(pk__in=ids - {None})}
    def label(pk):
        if pk is None:
            return 'None — manual pages'
        edition = editions.get(pk)
        return (f'Hardcover #{edition.provider_edition_id} · {edition.format_name or "Edition"}'
                if edition else 'Edition unavailable; reload before confirming')
    return [{'label': name, 'before': label(preview['before']['submission'].get(field)),
             'after': label(preview['after']['submission'].get(field))} for field, name in fields]


def history_entry(event):
    """Never expose reader_submissions, arbitrary audit JSON or free-form notes."""
    result = {'timestamp': event.created_at, 'actor': event.actor.username if event.actor else None, 'action': event.action_label,
              'rows': [], 'detail': 'No detailed before/after values were recorded.'}
    try:
        data = json.loads(event.summary)
    except (TypeError, ValueError):
        data = None
    if isinstance(data, dict):
        before, after = data.get('before', {}), data.get('after', {})
        if isinstance(before, dict) and isinstance(after, dict):
            before, after = before.get('submission', before), after.get('submission', after)
            if isinstance(before, dict) and isinstance(after, dict):
                result['rows'] = [{'label': label, 'before': before.get(key), 'after': after.get(key)}
                                  for key, label in IDENTITY_FIELDS
                                  if key in before or key in after]
        if data.get('association_mode') in {'only', 'update'}:
            result['detail'] = 'Link Only' if data['association_mode'] == 'only' else 'Update Book Information'
    elif event.action in {'submission.approved', 'submission.rejected', 'submission.pending'}:
        match = re.search(r'approved pages: (\d+|none)', event.summary)
        if match:
            result['detail'] = f'Recorded approved pages: {match.group(1)}.'
    return result
