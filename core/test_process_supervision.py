"""Process supervision tests, discovered by the maintained core test suite."""
import contextlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from deploy.supervise import commands, supervise


class CommandTests(unittest.TestCase):
    def test_defaults_preserve_existing_workers_and_safe_access_logs(self):
        roles = dict(commands({}))
        self.assertEqual(set(roles), {"gunicorn", "backup", "challenge", "hardcover"})
        self.assertIn("%(U)s", " ".join(roles["gunicorn"]))
        self.assertNotIn("%(r)s", " ".join(roles["gunicorn"]))

    def test_enabled_discord_roles_once_and_existing_intervals(self):
        roles = commands({"NORTHBOUND_DISCORD_EVENTS_ENABLED": "TRUE",
                          "NORTHBOUND_DISCORD_SCHEDULER_ENABLED": "yes"})
        self.assertEqual(len(roles), 6)
        self.assertEqual(len({n for n, _ in roles}), 6)
        for name, argv in roles:
            if name.startswith("discord-"):
                self.assertEqual(argv[-2:], ["--interval", "30"])
        # Other workers keep their existing command defaults (all 30 seconds).
        self.assertEqual(dict(roles)["challenge"][-1], "run_challenge_scheduler")
        self.assertEqual(dict(roles)["hardcover"][-1], "run_hardcover_sync_worker")

    def test_disabled_workers_leave_only_gunicorn(self):
        gates = {"NORTHBOUND_" + k: "0" for k in [
            "BACKUP_SCHEDULER_ENABLED", "CHALLENGE_SCHEDULER_ENABLED",
            "HARDCOVER_WRITEBACK_ENABLED", "DISCORD_EVENTS_ENABLED",
            "DISCORD_SCHEDULER_ENABLED"]}
        self.assertEqual([n for n, _ in commands(gates)], ["gunicorn"])

    def test_discord_gates_are_independent(self):
        roles = dict(commands({"NORTHBOUND_DISCORD_EVENTS_ENABLED": "on"}))
        self.assertIn("discord-events", roles)
        self.assertNotIn("discord-scheduler", roles)

    def test_postgresql_does_not_restart_on_expected_backup_exit(self):
        self.assertNotIn("backup", dict(commands({}, sqlite=False)))
        self.assertIn("backup", dict(commands({})))

    def test_partial_start_failure_cleans_up_without_logging_secrets(self):
        child = Mock(pid=123456, wait=Mock(return_value=0))
        output = io.StringIO()
        with patch("deploy.supervise.subprocess.Popen", side_effect=[child, OSError("synthetic-secret")]), \
                patch("deploy.supervise._signal_group") as send, \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            self.assertEqual(supervise([("gunicorn", ["fake"]), ("worker", ["fake"])]), 1)
        self.assertNotIn("synthetic-secret", output.getvalue())
        self.assertIn(unittest.mock.call(child, signal.SIGTERM), send.call_args_list)
        self.assertGreaterEqual(child.wait.call_count, 1)


CHILD = """
import os, pathlib, signal, sys, time
role, root, fail, stubborn = sys.argv[1:]
root = pathlib.Path(root)
def stop(signum, frame):
    (root/(role+'.stop')).write_text(str(signum))
    if stubborn != '1': sys.exit(0)
signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
(root/(role+'.pid')).write_text(str(os.getpid()))
while not (root/'go').exists(): time.sleep(.01)
if fail != '-': sys.exit(int(fail))
while True: time.sleep(.05)
"""


class ProcessTests(unittest.TestCase):
    def test_real_discord_workers_together_with_external_delivery_blocked(self):
        # Exercise both real command loops against one isolated heartbeat table.
        # This does not alter Development settings or create user preferences.
        with tempfile.TemporaryDirectory(prefix="northbound-supervisor-") as directory:
            root = Path(directory)
            env = os.environ.copy()
            for key in list(env):
                if key == "DATABASE_URL" or key.startswith("POSTGRES_"):
                    env.pop(key)
            env.update(DJANGO_SETTINGS_MODULE="reading_challenge.settings",
                       DJANGO_SECRET_KEY="synthetic-supervisor-test",
                       DJANGO_DEBUG="0", NORTHBOUND_DEVELOPMENT_BANNER="0",
                       NORTHBOUND_LIVE_SOURCE="0", PYTHONDONTWRITEBYTECODE="1",
                       NORTHBOUND_SQLITE_PATH=str(root/"heartbeat.sqlite3"),
                       NORTHBOUND_DISCORD_NOTIFICATIONS_ENABLED="0",
                       NORTHBOUND_DISCORD_ALLOW_DEVELOPMENT="0",
                       NORTHBOUND_DISCORD_EVENTS_ENABLED="1",
                       NORTHBOUND_DISCORD_SCHEDULER_ENABLED="1",
                       NORTHBOUND_BACKUP_SCHEDULER_ENABLED="0",
                       NORTHBOUND_CHALLENGE_SCHEDULER_ENABLED="0",
                       NORTHBOUND_HARDCOVER_WRITEBACK_ENABLED="0")
            setup = """
import django
django.setup()
from django.db import connection
from core.models import DiscordSchedulerHeartbeat
with connection.schema_editor() as schema:
    schema.create_model(DiscordSchedulerHeartbeat)
"""
            subprocess.run([sys.executable, "-B", "-c", setup], env=env,
                           check=True, capture_output=True, timeout=15)
            worker = """
import os, pathlib, sys
from unittest.mock import patch
root = pathlib.Path(sys.argv[2])
role = sys.argv[1]
def forbidden(*args, **kwargs):
    (root/'delivery-attempt').touch()
    raise AssertionError('External delivery is forbidden in this test')
with patch('socket.socket.connect', side_effect=forbidden), patch('socket.socket.connect_ex', side_effect=forbidden):
    import django
    django.setup()
    from django.core.management import call_command
    from core import discord_events, discord_scheduling
    original = discord_events.process_events if role == 'discord-events' else discord_scheduling.process_due_reports
    def observe(*args, **kwargs):
        result = original(*args, **kwargs)
        (root/(role+'.tick')).write_text('completed')
        return result
    target = 'core.discord_events.process_events' if role == 'discord-events' else 'core.discord_scheduling.process_due_reports'
    with patch('core.discord_notifications.send_webhook', side_effect=forbidden), patch(target, side_effect=observe):
        (root/(role+'.pid')).write_text(str(os.getpid()))
        call_command('run_discord_events' if role == 'discord-events' else 'run_discord_scheduler', interval=30)
"""
            specs = [("gunicorn", [sys.executable, "-B", "-c", CHILD,
                                    "gunicorn", directory, "-", "0"])]
            specs += [(role, [sys.executable, "-B", "-c", worker, role, directory])
                      for role in ("discord-events", "discord-scheduler")]
            runner = "from deploy.supervise import supervise; import sys; sys.exit(supervise(" + repr(specs) + ", shutdown_timeout=2))"
            process = subprocess.Popen([sys.executable, "-B", "-c", runner], env=env,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            pids = []
            try:
                until = time.monotonic() + 15
                while time.monotonic() < until and len(list(root.glob("*.tick"))) < 2:
                    if process.poll() is not None:
                        self.fail("A Discord command exited before its first safe iteration")
                    time.sleep(.05)
                self.assertEqual(len(list(root.glob("*.tick"))), 2)
                self.assertEqual(len(list(root.glob("*.pid"))), 3)
                pids = [int(p.read_text()) for p in root.glob("*.pid")]
                time.sleep(.2)
                self.assertIsNone(process.poll())
                for pid in pids:
                    os.kill(pid, 0)
                self.assertFalse((root/"delivery-attempt").exists())
                process.send_signal(signal.SIGTERM)
                output, error = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, error)
                for role in ("gunicorn", "discord-events", "discord-scheduler"):
                    self.assertEqual(output.count("started " + role + "."), 1)
                self.assertFalse((root/"delivery-attempt").exists())
                for pid in pids:
                    with self.assertRaises(ProcessLookupError):
                        os.kill(pid, 0)
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate(timeout=5)
                for p in root.glob("*.pid"):
                    try:
                        os.killpg(int(p.read_text()), signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def run_case(self, *, fail_role=None, fail_code=7, signum=signal.SIGTERM, stubborn=False):
        with tempfile.TemporaryDirectory(prefix="northbound-supervisor-") as directory:
            root = Path(directory)
            specs = [(role, [sys.executable, "-c", CHILD, role, directory,
                             str(fail_code) if role == fail_role else "-",
                             "1" if stubborn else "0"])
                     for role in ("gunicorn", "discord-events")]
            runner = "from deploy.supervise import supervise; import sys; sys.exit(supervise(" + repr(specs) + ", shutdown_timeout=.3))"
            process = subprocess.Popen([sys.executable, "-B", "-c", runner],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            pids = []
            try:
                until = time.monotonic() + 5
                while time.monotonic() < until and len(list(root.glob("*.pid"))) < 2:
                    if process.poll() is not None:
                        self.fail("Supervisor exited before children became ready")
                    time.sleep(.02)
                self.assertEqual(len(list(root.glob("*.pid"))), 2)
                pids = [int(p.read_text()) for p in root.glob("*.pid")]
                (root/"go").touch()
                if fail_role is None:
                    process.send_signal(signum)
                output, error = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 1 if fail_role else 0, error)
                self.assertEqual(output.count("started gunicorn."), 1)
                self.assertEqual(output.count("started discord-events."), 1)
                survivors = [role for role in ("gunicorn", "discord-events") if role != fail_role]
                for role in survivors:
                    self.assertEqual((root/(role+".stop")).read_text(), str(signal.SIGTERM if fail_role else signum))
                for pid in pids:
                    with self.assertRaises(ProcessLookupError):
                        os.kill(pid, 0)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)
                for pid in pids:
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_worker_failure_stops_web_and_reaps_children(self):
        self.run_case(fail_role="discord-events")

    def test_unexpected_successful_worker_exit_is_failure(self):
        self.run_case(fail_role="discord-events", fail_code=0)

    def test_gunicorn_failure_stops_worker(self):
        self.run_case(fail_role="gunicorn")

    def test_sigterm_is_forwarded_and_shutdown_is_successful(self):
        self.run_case()

    def test_sigint_is_forwarded_and_shutdown_is_successful(self):
        self.run_case(signum=signal.SIGINT)

    def test_stubborn_children_are_killed_within_deadline(self):
        self.run_case(stubborn=True)


if __name__ == "__main__":
    unittest.main()
