from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .admin import AuditEventAdmin, RecoveryOperationAdmin
from .maintenance import prune_audit_history
from .models import (
    AuditEvent, BookSubmission, ChallengeMonth, ChallengeStaffAssignment,
    HardcoverConnection, Membership, MonthEnrollment, ReadingGroup,
    RecoveryOperation, Team, TeamAssignment,
)
from .recovery import (
    RecoveryImpactItem, RecoveryImpactPreview, RecoveryRequest,
    execute_recovery_operation, safe_recovery_data, stored_backup_advisory,
    validate_reason, validate_recovery_request, validate_typed_confirmation,
)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class RecoveryFrameworkTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_superuser("platform-owner", password="owner-password-482!")
        self.group = ReadingGroup.objects.create(name="Recovery Group", slug="recovery-group")
        self.month = ChallengeMonth.objects.create(
            group=self.group, name="Recovery Challenge",
            starts_on=timezone.localdate(), ends_on=timezone.localdate() + timedelta(days=30),
        )
        self.team = Team.objects.create(month=self.month, name="Recovery Team")

    def request_for(self, user, *, tier=RecoveryOperation.Tier.DESTRUCTIVE, **overrides):
        values = {
            "actor": user, "tier": tier, "action": "framework.test", "target_type": "ReadingGroup",
            "target_id": str(self.group.pk), "target_label": self.group.name,
            "reason": "Repair test data.", "required_confirmation": "RECOVER",
            "supplied_confirmation": "RECOVER", "confirmation_method": "typed phrase",
            "group": self.group,
            "impact": RecoveryImpactPreview(self.group.name, (RecoveryImpactItem("Records", 1),), ("Test warning",)),
            "before_state": {"name": self.group.name},
        }
        values.update(overrides)
        return RecoveryRequest(**values)

    def test_only_platform_owner_can_access_recovery_pages_without_creating_identity(self):
        User = get_user_model()
        normal_roles = []
        for label, role in (("group-owner", Membership.Role.OWNER), ("moderator", Membership.Role.MODERATOR), ("reader", Membership.Role.MEMBER)):
            user = User.objects.create_user(label, password="test-password")
            membership = Membership.objects.create(group=self.group, user=user, role=role, display_name=label)
            normal_roles.append((label, user, membership))

        for label in ("host", "floater", "team-leader"):
            user = User.objects.create_user(label, password="test-password")
            membership = Membership.objects.create(group=self.group, user=user, display_name=label)
            if label == "team-leader":
                MonthEnrollment.objects.create(month=self.month, participant=membership)
                TeamAssignment.objects.create(month=self.month, team=self.team, participant=membership)
                ChallengeStaffAssignment.objects.create(month=self.month, membership=membership, team=self.team, role=ChallengeStaffAssignment.Role.TEAM_LEADER)
            else:
                ChallengeStaffAssignment.objects.create(month=self.month, membership=membership, role=label)
            normal_roles.append((label, user, membership))

        baseline = (Membership.objects.count(), ChallengeStaffAssignment.objects.count(), MonthEnrollment.objects.count(), TeamAssignment.objects.count(), BookSubmission.objects.count())
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse("advanced-recovery")).status_code, 200)
        self.assertEqual(self.client.get(reverse("recovery-history")).status_code, 200)
        recovery_page = self.client.get(reverse("advanced-recovery"))
        self.assertContains(recovery_page, "Challenges &amp; Participation", html=False)
        self.assertContains(recovery_page, "Submission Recovery")
        self.assertContains(recovery_page, "Theme Recovery")
        self.assertContains(recovery_page, "Modifier Recovery")
        self.assertContains(recovery_page, "BOTM Recovery")
        self.assertContains(recovery_page, "Personal TBR Recovery")
        self.assertContains(recovery_page, "Game Reward Recovery")
        self.assertContains(recovery_page, "Checkpoint Recovery")
        self.assertContains(recovery_page, "Hardcover Credential Recovery")
        self.assertContains(recovery_page, 'class="card recovery-unsupported-panel"')
        self.assertContains(recovery_page, "No generic recovery CRUD")
        self.assertEqual(baseline, (Membership.objects.count(), ChallengeStaffAssignment.objects.count(), MonthEnrollment.objects.count(), TeamAssignment.objects.count(), BookSubmission.objects.count()))

        for label, user, _ in normal_roles:
            with self.subTest(role=label):
                self.client.force_login(user)
                self.assertEqual(self.client.get(reverse("advanced-recovery")).status_code, 403)
                self.assertEqual(self.client.get(reverse("recovery-history")).status_code, 403)

    def test_framework_creates_immutable_redacted_success_ledger(self):
        request = self.request_for(
            self.owner, tier=RecoveryOperation.Tier.EMERGENCY,
            current_password="owner-password-482!",
            before_state={"password": "never-store", "nested": {"encrypted_token": "cipher", "safe": "kept"}},
        )
        operation = execute_recovery_operation(request, lambda: {"token_hint": "1234", "status": "repaired"})
        self.assertEqual(operation.result, RecoveryOperation.Result.SUCCEEDED)
        self.assertEqual(operation.reason, "Repair test data.")
        self.assertEqual(operation.impact["items"], [{"label": "Records", "count": 1}])
        self.assertEqual(operation.before_state["password"], "[REDACTED]")
        self.assertEqual(operation.before_state["nested"], {"encrypted_token": "[REDACTED]", "safe": "kept"})
        self.assertEqual(operation.after_state, {"token_hint": "[REDACTED]", "status": "repaired"})
        self.assertNotIn("owner-password-482!", str(RecoveryOperation.objects.values().get(pk=operation.pk)))
        operation.reason = "Changed"
        with self.assertRaisesMessage(ValidationError, "immutable"):
            operation.save()
        with self.assertRaisesMessage(ValidationError, "cannot be deleted"):
            operation.delete()

    def test_ledger_survives_target_deletion_and_history_post_cannot_mutate_it(self):
        operation = execute_recovery_operation(self.request_for(self.owner), lambda: {})
        self.group.delete()
        operation.refresh_from_db()
        self.assertIsNone(operation.group)
        self.assertIsNone(operation.challenge)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.post(reverse("recovery-history"), {"reason": "Changed"}).status_code, 200)
        operation.refresh_from_db()
        self.assertEqual(operation.reason, "Repair test data.")

    def test_reason_confirmation_and_password_validation(self):
        with self.assertRaises(ValidationError):
            validate_reason("   ", required=True)
        self.assertEqual(validate_reason("  valid reason  ", required=True), "valid reason")
        self.assertEqual(validate_typed_confirmation(required="DELETE", supplied="DELETE"), "DELETE")
        with self.assertRaises(ValidationError):
            validate_typed_confirmation(required="DELETE", supplied="delete")
        with self.assertRaises(ValidationError):
            validate_recovery_request(self.request_for(self.owner, tier=RecoveryOperation.Tier.EMERGENCY, current_password="wrong"))
        self.assertFalse(RecoveryOperation.objects.exists())
        inactive_owner = get_user_model().objects.create_superuser("inactive-owner", password="test-password", is_active=False)
        with self.assertRaises(PermissionDenied):
            validate_recovery_request(self.request_for(inactive_owner))

    def test_transaction_failure_rolls_back_domain_change_and_records_failed_result(self):
        def failing_mutation():
            self.group.name = "Partially Changed"
            self.group.save(update_fields=["name"])
            raise RuntimeError("deliberate failure")

        with self.assertRaisesMessage(RuntimeError, "deliberate failure"):
            execute_recovery_operation(self.request_for(self.owner), failing_mutation)
        self.group.refresh_from_db()
        self.assertEqual(self.group.name, "Recovery Group")
        operation = RecoveryOperation.objects.get()
        self.assertEqual(operation.result, RecoveryOperation.Result.FAILED)
        self.assertEqual(operation.after_state, {})

    def test_audit_pruning_does_not_remove_recovery_ledger(self):
        AuditEvent.objects.create(actor=self.owner, action="old.event", object_type="Test")
        AuditEvent.objects.update(created_at=timezone.now() - timedelta(days=800))
        operation = execute_recovery_operation(self.request_for(self.owner), lambda: {})
        prune_audit_history(years=1, actor=self.owner)
        self.assertTrue(RecoveryOperation.objects.filter(pk=operation.pk).exists())
        self.assertFalse(AuditEvent.objects.filter(action="old.event").exists())

    def test_safe_snapshot_redaction(self):
        cleaned = safe_recovery_data({"api_token": "abc", "registration": "safe", "registration_answers": "private", "note": "password=visible"})
        self.assertEqual(cleaned["api_token"], "[REDACTED]")
        self.assertEqual(cleaned["registration"], "safe")
        self.assertEqual(cleaned["registration_answers"], "[REDACTED]")
        self.assertNotIn("visible", cleaned["note"])
        with self.assertRaises(ValidationError):
            RecoveryImpactItem("Bad", -1)

    @patch("core.recovery.list_stored_backups")
    def test_backup_advisory_recent_stale_and_missing_without_blocking(self, stored):
        now = timezone.now()
        stored.return_value = []
        self.assertEqual(stored_backup_advisory(now=now), {"exists": False, "newest_at": None, "is_recent": False, "window_hours": 24})
        stored.return_value = [SimpleNamespace(stat=lambda: SimpleNamespace(st_mtime=(now - timedelta(hours=2)).timestamp()))]
        self.assertTrue(stored_backup_advisory(now=now)["is_recent"])
        stored.return_value = [SimpleNamespace(stat=lambda: SimpleNamespace(st_mtime=(now - timedelta(hours=25)).timestamp()))]
        self.assertFalse(stored_backup_advisory(now=now)["is_recent"])
        validate_recovery_request(self.request_for(self.owner))

    def test_admin_contains_domain_credentials_and_protected_ledgers(self):
        for model in (ReadingGroup, Membership, ChallengeMonth, ChallengeStaffAssignment, Team, TeamAssignment, MonthEnrollment, BookSubmission, HardcoverConnection):
            self.assertFalse(admin.site.is_registered(model), model.__name__)
        self.assertTrue(admin.site.is_registered(AuditEvent))
        self.assertTrue(admin.site.is_registered(RecoveryOperation))
        request = RequestFactory().get("/admin/")
        request.user = self.owner
        for model_admin in (AuditEventAdmin(AuditEvent, admin.site), RecoveryOperationAdmin(RecoveryOperation, admin.site)):
            self.assertFalse(model_admin.has_add_permission(request))
            self.assertFalse(model_admin.has_change_permission(request))
            self.assertFalse(model_admin.has_delete_permission(request))
