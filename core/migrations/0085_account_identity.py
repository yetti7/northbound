"""Preflight first: never repair identifiers or populate blank public names."""
import unicodedata
from django.db import migrations, models


def key(value):
    return unicodedata.normalize('NFKC', value or '').strip().casefold()


def preflight(apps, schema_editor):
    User = apps.get_model('auth', 'User')
    seen = {}
    conflicts = set()
    blank = []
    for user_id, username, email in User.objects.using(schema_editor.connection.alias).values_list('pk', 'username', 'email'):
        for value in (username, email):
            normalized = key(value)
            if not normalized:
                blank.append(user_id)
            if normalized in seen and seen[normalized] != user_id:
                conflicts.update((seen[normalized], user_id))
            seen[normalized] = user_id
    if conflicts or blank:
        raise RuntimeError('Account identity preflight failed; resolve identifiers before retrying. '
                           f'Conflicting account IDs: {sorted(conflicts)}; missing identifiers: {sorted(set(blank))}.')


def profiles(apps, schema_editor):
    User = apps.get_model('auth', 'User')
    Profile = apps.get_model('core', 'UserProfile')
    alias = schema_editor.connection.alias
    existing = Profile.objects.using(alias).values_list('user_id', flat=True)
    Profile.objects.using(alias).bulk_create([
        Profile(user_id=pk) for pk in User.objects.using(alias).exclude(pk__in=existing).values_list('pk', flat=True)
    ])


def guards(apps, schema_editor):
    # Native indexes guard all database writers for LOWER equivalences. Interactive
    # writes additionally serialize and validate NFKC+casefold in Python.
    schema_editor.execute('CREATE UNIQUE INDEX northbound_username_ci ON auth_user (LOWER(username))')
    schema_editor.execute("CREATE UNIQUE INDEX northbound_email_ci ON auth_user (LOWER(email)) WHERE email <> ''")
    if schema_editor.connection.vendor == 'sqlite':
        for operation in ('INSERT', 'UPDATE OF username, email'):
            name = 'northbound_identifiers_' + ('insert' if operation == 'INSERT' else 'update')
            schema_editor.execute(f'''CREATE TRIGGER {name} BEFORE {operation} ON auth_user
                WHEN EXISTS (SELECT 1 FROM auth_user u WHERE u.id <> NEW.id AND
                    ((NEW.email <> '' AND LOWER(u.username)=LOWER(NEW.email)) OR
                     (u.email <> '' AND LOWER(u.email)=LOWER(NEW.username))))
                BEGIN SELECT RAISE(ABORT, 'Ambiguous account identifier'); END''')
    elif schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute('''CREATE FUNCTION northbound_identifier_guard() RETURNS trigger AS $$
        BEGIN
            IF EXISTS (SELECT 1 FROM auth_user u WHERE u.id <> NEW.id AND
                ((NEW.email <> '' AND LOWER(u.username)=LOWER(NEW.email)) OR
                 (u.email <> '' AND LOWER(u.email)=LOWER(NEW.username)))) THEN
                RAISE EXCEPTION 'Ambiguous account identifier' USING ERRCODE='23505';
            END IF;
            RETURN NEW;
        END; $$ LANGUAGE plpgsql''')
        schema_editor.execute('''CREATE TRIGGER northbound_identifiers BEFORE INSERT OR UPDATE OF username,email
            ON auth_user FOR EACH ROW EXECUTE FUNCTION northbound_identifier_guard()''')


def unguard(apps, schema_editor):
    if schema_editor.connection.vendor == 'sqlite':
        schema_editor.execute('DROP TRIGGER IF EXISTS northbound_identifiers_insert')
        schema_editor.execute('DROP TRIGGER IF EXISTS northbound_identifiers_update')
    elif schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute('DROP TRIGGER IF EXISTS northbound_identifiers ON auth_user')
        schema_editor.execute('DROP FUNCTION IF EXISTS northbound_identifier_guard()')
    schema_editor.execute('DROP INDEX northbound_email_ci')
    schema_editor.execute('DROP INDEX northbound_username_ci')


class Migration(migrations.Migration):
    dependencies = [('core', '0084_platform_email_settings'), ('auth', '0012_alter_user_first_name_max_length')]
    operations = [
        migrations.RunPython(preflight, migrations.RunPython.noop),
        migrations.AddField(model_name='userprofile', name='display_name_confirmed_at',
                            field=models.DateTimeField(null=True, blank=True, editable=False)),
        migrations.RunPython(profiles, migrations.RunPython.noop),
        migrations.RunPython(guards, unguard),
    ]
