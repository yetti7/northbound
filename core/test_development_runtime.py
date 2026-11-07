"""Development isolation controls; no Docker daemon or provider access required."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser
from django.core.management import call_command
from django.template.loader import render_to_string
from django.test import SimpleTestCase, RequestFactory, override_settings

from .context_processors import platform_configuration
from .integrations import hardcover_library as library
from .integrations.hardcover import HardcoverConnectionError
from .hardcover_sync import process_hardcover_sync_batch, reader_sync_capability

ROOT = Path(__file__).resolve().parent.parent


class DevelopmentRuntimeTests(SimpleTestCase):
    def runtime_settings(self, **changes):
        env=os.environ.copy()
        for name in ('DJANGO_SESSION_COOKIE_NAME','DJANGO_CSRF_COOKIE_NAME','NORTHBOUND_DEVELOPMENT_BANNER',
                     'NORTHBOUND_HARDCOVER_WRITEBACK_ENABLED','DATABASE_URL','POSTGRES_HOST'):
            env.pop(name,None)
        env.update(DJANGO_SETTINGS_MODULE='reading_challenge.settings',DJANGO_DEBUG='0',
                   DJANGO_SECRET_KEY='synthetic-development-settings-test',**changes)
        script="from django.conf import settings; import json; print(json.dumps({k:getattr(settings,k) for k in ['SESSION_COOKIE_NAME','CSRF_COOKIE_NAME','NORTHBOUND_DEVELOPMENT_BANNER','NORTHBOUND_HARDCOVER_WRITEBACK_ENABLED']}))"
        return json.loads(subprocess.check_output([sys.executable,'-c',script],cwd=ROOT,env=env,text=True))

    def test_defaults_preserve_production_behavior(self):
        data=self.runtime_settings()
        self.assertEqual(data,{'SESSION_COOKIE_NAME':'sessionid','CSRF_COOKIE_NAME':'csrftoken',
            'NORTHBOUND_DEVELOPMENT_BANNER':False,'NORTHBOUND_HARDCOVER_WRITEBACK_ENABLED':True})

    def test_development_cookie_names_banner_and_writeback_configuration(self):
        data=self.runtime_settings(DJANGO_SESSION_COOKIE_NAME='northbound_dev_sessionid',
            DJANGO_CSRF_COOKIE_NAME='northbound_dev_csrftoken',NORTHBOUND_DEVELOPMENT_BANNER='1',
            NORTHBOUND_HARDCOVER_WRITEBACK_ENABLED='0')
        self.assertEqual(data['SESSION_COOKIE_NAME'],'northbound_dev_sessionid')
        self.assertEqual(data['CSRF_COOKIE_NAME'],'northbound_dev_csrftoken')
        self.assertTrue(data['NORTHBOUND_DEVELOPMENT_BANNER']);self.assertFalse(data['NORTHBOUND_HARDCOVER_WRITEBACK_ENABLED'])

    def startup(self, disabled):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);log=root/'commands'
            python=root/'python';python.write_text('#!/bin/sh\nprintf "python %s\\n" "$*" >> "$STARTUP_TEST_LOG"\n');python.chmod(0o700)
            gunicorn=root/'gunicorn';gunicorn.write_text('#!/bin/sh\nprintf "gunicorn %s\\n" "$*" >> "$STARTUP_TEST_LOG"\nsleep 0.1\n');gunicorn.chmod(0o700)
            env=os.environ.copy();env['PATH']=str(root)+os.pathsep+env['PATH'];env['STARTUP_TEST_LOG']=str(log)
            for name in ('NORTHBOUND_BACKUP_SCHEDULER_ENABLED','NORTHBOUND_CHALLENGE_SCHEDULER_ENABLED','NORTHBOUND_HARDCOVER_WRITEBACK_ENABLED'):
                env.pop(name,None)
                if disabled:env[name]='0'
            subprocess.run(['/bin/sh',str(ROOT/'deploy/start.sh')],cwd=ROOT,env=env,check=True,timeout=5,capture_output=True)
            return log.read_text()

    def test_startup_defaults_keep_all_three_background_processes(self):
        log=self.startup(False)
        for name in ('run_backup_scheduler','run_challenge_scheduler','run_hardcover_sync_worker'):
            self.assertIn(name,log)
        self.assertIn('migrate --noinput',log);self.assertIn('gunicorn ',log)

    def test_disabled_background_processes_keep_web_and_data_initialization(self):
        log=self.startup(True)
        for name in ('run_backup_scheduler','run_challenge_scheduler','run_hardcover_sync_worker'):
            self.assertNotIn(name,log)
        for name in ('restore_pending_backup','migrate --noinput','safeguard_restored_hardcover_sync','gunicorn '):
            self.assertIn(name,log)

    @override_settings(NORTHBOUND_HARDCOVER_WRITEBACK_ENABLED=False)
    def test_all_library_mutations_are_blocked_before_network(self):
        calls=((library.create_read_user_book,{'book_id':1,'read_status_id':3}),
               (library.update_user_book_to_read,{'user_book_id':1,'read_status_id':3}),
               (library.create_read_occurrence,{'user_book_id':1,'finished_at':date(2026,11,5)}),
               (library.update_read_occurrence,{'read_id':1,'finished_at':date(2026,11,5)}))
        with patch.object(library,'execute_graphql') as network:
            for function,args in calls:
                with self.assertRaises(HardcoverConnectionError) as error:function('synthetic',**args)
                self.assertEqual(error.exception.classification,'writeback_disabled')
            network.assert_not_called()

    @override_settings(NORTHBOUND_HARDCOVER_WRITEBACK_ENABLED=False)
    def test_worker_and_processor_do_not_claim_or_replay_jobs(self):
        with patch('core.hardcover_sync._claim_one') as claim:
            self.assertEqual(process_hardcover_sync_batch(),[]);claim.assert_not_called()
        with patch('core.management.commands.run_hardcover_sync_worker.call_command') as safeguard:
            output=io.StringIO();call_command('run_hardcover_sync_worker',once=True,stdout=output)
            self.assertIn('disabled',output.getvalue());safeguard.assert_not_called()
        self.assertFalse(reader_sync_capability(None)[0])

    @override_settings(NORTHBOUND_HARDCOVER_WRITEBACK_ENABLED=False)
    def test_catalog_read_operations_remain_available(self):
        with patch.object(library,'execute_graphql',return_value={'user_book_reads_by_pk':{'id':1}}) as network:
            self.assertEqual(library.read_occurrence('synthetic',1),{'id':1});network.assert_called_once()

    def test_banner_is_opt_in_and_visible_on_base_layout(self):
        request=RequestFactory().get('/');request.resolver_match=None
        values=SimpleNamespace(display_name='Northbound',allow_public_registration=True,allow_user_group_creation=True)
        with patch('core.context_processors.get_platform_settings',return_value=values):
            for enabled in (False,True):
                with self.settings(NORTHBOUND_DEVELOPMENT_BANNER=enabled):
                    context=platform_configuration(request);context['user']=AnonymousUser()
                    markup=render_to_string('base.html',context)
                    self.assertEqual('DEVELOPMENT' in markup,enabled)
