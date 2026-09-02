from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django import forms
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone
from django.contrib.auth import get_user_model

from .forms import ChallengeRegistrationSettingsForm
from .models import ChallengeMonth, ChallengeStaffAssignment, HardcoverConnection, Membership, ReadingGroup
from .integrations.secrets import encrypt_token
from .widgets import MidnightDateTimeInput


class DateTimeDefaultsTests(SimpleTestCase):
    class ExampleForm(forms.Form):
        when = forms.DateTimeField(required=False, widget=MidnightDateTimeInput(), input_formats=["%Y-%m-%dT%H:%M"])

    def test_blank_date_has_midnight_and_remains_optional(self):
        self.assertIn('value="00:00"', str(self.ExampleForm()["when"]))
        form = self.ExampleForm({"when_0": "", "when_1": "00:00"})
        self.assertTrue(form.is_valid())
        self.assertIsNone(form.cleaned_data["when"])

    def test_new_date_and_selected_time_are_timezone_aware(self):
        with timezone.override("America/New_York"):
            for time, hour in (("00:00", 0), ("17:45", 17)):
                form = self.ExampleForm({"when_0": "2026-09-02", "when_1": time})
                self.assertTrue(form.is_valid(), form.errors)
                self.assertEqual(form.cleaned_data["when"].hour, hour)
                self.assertTrue(timezone.is_aware(form.cleaned_data["when"]))

    def test_edit_preserves_local_time_and_invalid_post_value(self):
        with timezone.override("America/New_York"):
            form = self.ExampleForm(initial={"when": datetime(2026, 9, 2, 21, 45, tzinfo=ZoneInfo("UTC"))})
            self.assertIn('value="17:45"', str(form["when"]))
            bound = self.ExampleForm({"when_0": "2026-09-02", "when_1": "17:45"})
            self.assertIn('value="17:45"', str(bound["when"]))
            invalid = self.ExampleForm({"when_0": "2026-09-02", "when_1": "invalid"})
            self.assertFalse(invalid.is_valid())
            self.assertIn('value="invalid"', str(invalid["when"]))

    def test_dst_ambiguity_is_still_rejected(self):
        with timezone.override("America/New_York"):
            form = self.ExampleForm({"when_0": "2026-11-01", "when_1": "01:30"})
            self.assertFalse(form.is_valid())


class EditingDurationTests(SimpleTestCase):
    def test_only_timed_policy_requires_valid_duration(self):
        for policy, _ in ChallengeMonth.RegistrationAnswerEditingPolicy.choices:
            for hours in ("", "0", "721", "invalid", "48"):
                with self.subTest(policy=policy, hours=hours):
                    form = ChallengeRegistrationSettingsForm({"registration_answer_editing_policy": policy, "registration_answer_editing_hours": hours})
                    self.assertEqual(form.is_valid(), policy != "timed" or hours == "48", form.errors)
                    if policy != "timed":
                        self.assertEqual(form.cleaned_data["registration_answer_editing_hours"], 24)


class GroupCatalogPolishTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("polish-owner")
        self.group = ReadingGroup.objects.create(name="Polish", slug="polish")
        Membership.objects.create(group=self.group, user=self.user, role=Membership.Role.OWNER, display_name="Owner")
        self.month = ChallengeMonth.objects.create(group=self.group, name="Polish", status="draft")
        ChallengeStaffAssignment.objects.create(month=self.month, membership=Membership.objects.get(user=self.user), role=ChallengeStaffAssignment.Role.HOST)
        HardcoverConnection.objects.create(group=self.group, encrypted_token=encrypt_token("group-test-token"), is_valid=True)
        self.client.force_login(self.user)
        self.url = reverse("botm-catalog", args=[self.group.slug, self.month.pk])

    def test_smart_search_uses_group_credential(self):
        for value in ("Book title", "Author", "9781234567890"):
            with patch("core.views.search_books", return_value=([], False)) as search:
                response = self.client.post(self.url, {"action": "smart", "input": value})
                self.assertEqual(response.json()["lookup_type"], "search")
                search.assert_called_once_with("group-test-token", value)

    def test_book_and_edition_links_use_shared_resolver(self):
        for result, kind in (({"book_id": "1", "edition_required": True}, "book"), ({"edition_id": "2"}, "edition")):
            with patch("core.views.lookup_hardcover_url", return_value=(result, False)) as lookup:
                response = self.client.post(self.url, {"action": "smart", "input": "https://hardcover.app/books/example"})
                self.assertEqual(response.json()["lookup_type"], kind)
                lookup.assert_called_once_with("group-test-token", "https://hardcover.app/books/example")

    def test_unsupported_link_never_becomes_catalog_search(self):
        with patch("core.views.search_books") as search:
            response = self.client.post(self.url, {"action": "smart", "input": "https://example.com/books/x"})
            self.assertEqual(response.status_code, 400)
            search.assert_not_called()

    def test_token_success_payload_and_least_privilege_copy(self):
        with patch("core.views.test_catalog_connection", return_value=True):
            response = self.client.post(reverse("hardcover-test-token"), {"api_token": "test-token"})
        self.assertEqual(response.json()["title"], "Hardcover Catalog Connected")
        self.assertEqual(response.json()["message"], "Northbound can search the Hardcover catalog for this group.")
        page = self.client.get(reverse("group-edit", args=[self.group.slug]))
        self.assertContains(page, "read:catalog:data")
        self.assertContains(page, "read:catalog:search")


class ImageTagPolicyTests(SimpleTestCase):
    def test_actual_workflow_script_only_promotes_main_and_stable_tags(self):
        import os
        from pathlib import Path
        import subprocess
        import tempfile
        from django.conf import settings

        workflow = (Path(settings.BASE_DIR) / ".github/workflows/container.yml").read_text()
        step = workflow.split("id: image-tags", 1)[1].split("      - name:", 1)[0]
        script = "\n".join(line[10:] for line in step.split("run: |\n", 1)[1].splitlines())
        cases = [("branch", "main", True), ("branch", "dev", False)]
        cases += [("tag", tag, expected) for tag, expected in (
            ("v1.0.1", True), ("v0.0.0", True), ("v12.34.56", True),
            ("v1.0.1-rc.1", False), ("v1.0.1-beta", False), ("v1.0.1+build", False),
            ("v01.0.1", False), ("v1.0", False), ("latest", False),
        )]
        for ref_type, ref_name, expected in cases:
            with self.subTest(ref=ref_name), tempfile.NamedTemporaryFile() as output:
                subprocess.run(["bash", "-e", "-c", script], check=True, env={**os.environ, "REF_TYPE": ref_type, "REF_NAME": ref_name, "GITHUB_OUTPUT": output.name})
                self.assertEqual(Path(output.name).read_text().strip(), f"latest={str(expected).lower()}")
        self.assertIn("flavor: latest=false", workflow)
        self.assertIn("type=ref,event=tag", workflow)
        self.assertIn("type=sha", workflow)
        self.assertIn("tags: ${{ steps.meta.outputs.tags }}", workflow)
