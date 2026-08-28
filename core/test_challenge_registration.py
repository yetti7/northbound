from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    AuditEvent,
    ChallengeMonth,
    ChallengeSignupAnswer,
    ChallengeSignupQuestion,
    ChallengeStaffAssignment,
    Membership,
    MonthEnrollment,
    ReadingGroup,
    Team,
    TeamAssignment,
    UserProfile,
)


class ChallengeRegistrationMigrationTests(TransactionTestCase):
    migrate_from = [("core", "0037_userprofile_discord_username_is_public")]
    migrate_to = [("core", "0038_challenge_registration_questions")]

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps
        User = old_apps.get_model("auth", "User")
        ReadingGroupOld = old_apps.get_model("core", "ReadingGroup")
        MembershipOld = old_apps.get_model("core", "Membership")
        ChallengeMonthOld = old_apps.get_model("core", "ChallengeMonth")
        MonthEnrollmentOld = old_apps.get_model("core", "MonthEnrollment")
        user = User.objects.create(username="registration-migration-reader")
        group = ReadingGroupOld.objects.create(name="Registration Migration", slug="registration-migration")
        member = MembershipOld.objects.create(group=group, user=user, display_name="Reader")
        month = ChallengeMonthOld.objects.create(
            group=group,
            name="Existing Challenge",
            starts_on=date(2026, 8, 1),
            ends_on=date(2026, 8, 31),
            status="active",
        )
        self.month_pk = month.pk
        self.enrollment_pk = MonthEnrollmentOld.objects.create(month=month, participant=member).pk
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        migrated_apps = executor.loader.project_state(self.migrate_to).apps
        self.MigratedChallengeMonth = migrated_apps.get_model("core", "ChallengeMonth")
        self.MigratedMonthEnrollment = migrated_apps.get_model("core", "MonthEnrollment")

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_existing_participation_is_preserved_with_default_policy_and_no_fabricated_answers(self):
        month = self.MigratedChallengeMonth.objects.get(pk=self.month_pk)
        enrollment = self.MigratedMonthEnrollment.objects.get(pk=self.enrollment_pk)
        self.assertEqual(month.registration_answer_editing_policy, "timed")
        self.assertEqual(month.registration_answer_editing_hours, 24)
        self.assertEqual(enrollment.pk, self.enrollment_pk)
        self.assertFalse(enrollment.signup_answers.exists())
        self.assertFalse(month.signup_questions.exists())


class ChallengeRegistrationTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.platform_owner = User.objects.create_superuser("registration-root", "root@example.com", "password")
        self.owner_user = User.objects.create_user("registration-owner", password="password")
        self.moderator_user = User.objects.create_user("registration-moderator", password="password")
        self.host_user = User.objects.create_user("registration-host", password="password")
        self.reader_user = User.objects.create_user("registration-reader", password="password")
        self.other_user = User.objects.create_user("registration-other", password="password")
        self.floater_user = User.objects.create_user("registration-floater", password="password")
        self.leader_user = User.objects.create_user("registration-leader", password="password")
        self.group = ReadingGroup.objects.create(name="Registration Group", slug="registration-group")
        self.owner = Membership.objects.create(group=self.group, user=self.owner_user, role=Membership.Role.OWNER, display_name="Owner")
        self.moderator = Membership.objects.create(
            group=self.group,
            user=self.moderator_user,
            role=Membership.Role.MODERATOR,
            display_name="Moderator",
            permission_overrides={"manage_months": True},
        )
        self.host = Membership.objects.create(group=self.group, user=self.host_user, display_name="Host")
        self.reader = Membership.objects.create(group=self.group, user=self.reader_user, display_name="Reader")
        self.other = Membership.objects.create(group=self.group, user=self.other_user, display_name="Other")
        self.floater = Membership.objects.create(group=self.group, user=self.floater_user, display_name="Floater")
        self.leader = Membership.objects.create(group=self.group, user=self.leader_user, display_name="Leader")
        self.month = ChallengeMonth.objects.create(
            group=self.group,
            name="Registration Challenge",
            starts_on=date(2026, 8, 1),
            ends_on=date(2026, 8, 31),
            status=ChallengeMonth.Status.UPCOMING,
            registration_is_open=True,
        )
        ChallengeStaffAssignment.objects.create(
            month=self.month,
            membership=self.host,
            role=ChallengeStaffAssignment.Role.HOST,
            assigned_by=self.owner_user,
        )
        ChallengeStaffAssignment.objects.create(
            month=self.month,
            membership=self.floater,
            role=ChallengeStaffAssignment.Role.FLOATER,
            assigned_by=self.host_user,
        )
        self.team = Team.objects.create(month=self.month, name="Registration Team")
        UserProfile.objects.create(user=self.reader_user)

    def register_url(self):
        return reverse("challenge-register", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})

    def settings_url(self):
        return reverse("challenge-signup-settings", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})

    def detail_url(self, enrollment):
        return reverse(
            "challenge-registration-detail",
            kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "enrollment_pk": enrollment.pk},
        )

    def question(self, wording, question_type, required=False, choices=None, position=1):
        return ChallengeSignupQuestion.objects.create(
            month=self.month,
            wording=wording,
            question_type=question_type,
            is_required=required,
            choices=choices or [],
            position=position,
        )

    def settings_payload(self, questions, policy="timed", hours=24, initial=0):
        payload = {
            "settings-registration_answer_editing_policy": policy,
            "settings-registration_answer_editing_hours": hours,
            "questions-TOTAL_FORMS": len(questions),
            "questions-INITIAL_FORMS": initial,
            "questions-MIN_NUM_FORMS": 0,
            "questions-MAX_NUM_FORMS": 10,
        }
        for index, question in enumerate(questions):
            payload.update({
                f"questions-{index}-wording": question["wording"],
                f"questions-{index}-question_type": question["question_type"],
                f"questions-{index}-is_required": "on" if question.get("required") else "",
                f"questions-{index}-choices_text": question.get("choices", ""),
                f"questions-{index}-ORDER": question.get("order", index + 1),
                f"questions-{index}-DELETE": "on" if question.get("remove") else "",
            })
        return payload

    def test_zero_question_challenge_uses_confirmation_page_before_registration(self):
        self.month.enrollments.all().delete()
        self.client.force_login(self.reader_user)
        page = self.client.get(self.register_url())
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Submit Registration")
        self.assertFalse(MonthEnrollment.objects.filter(month=self.month, participant=self.reader).exists())
        self.assertRedirects(self.client.post(self.register_url()), self.month.get_absolute_url())
        self.assertTrue(MonthEnrollment.objects.get(month=self.month, participant=self.reader).is_active)

    def test_signup_configuration_compactly_lists_automatic_information_before_builders(self):
        self.client.force_login(self.owner_user)
        response = self.client.get(self.settings_url())
        content = response.content.decode()
        self.assertContains(response, "Information automatically provided")
        self.assertContains(response, "Reader name")
        self.assertContains(response, "Email")
        self.assertContains(response, "Discord username")
        self.assertContains(response, "Previous Challenges completed")
        self.assertContains(response, "Average Challenge pages for this Group")
        self.assertContains(response, "Previous/last Challenge pages")
        self.assertNotContains(response, "If a Reader has not added a Discord username")
        self.assertNotContains(response, "You do not need to create a custom Discord username question")
        information_position = content.index("Information automatically provided")
        self.assertLess(information_position, content.index("Reader answer editing"))
        self.assertLess(information_position, content.index("Custom questions"))

    def test_question_builder_hides_formset_controls_and_persists_move_and_remove_actions(self):
        self.month.enrollments.all().delete()
        self.question("First question", "short_text", position=1)
        self.question("Remove this question", "number", position=2)
        self.question("Choice question", "single_choice", choices=["One", "Two"], position=3)
        self.client.force_login(self.owner_user)
        page = self.client.get(self.settings_url())
        self.assertContains(page, 'type="hidden" name="questions-0-ORDER"')
        self.assertContains(page, 'type="hidden" name="questions-0-DELETE"')
        self.assertNotContains(page, "<label>Order:</label>", html=True)
        self.assertNotContains(page, "<label>Delete:</label>", html=True)
        self.assertContains(page, "Remove Question", count=4)
        self.assertContains(page, "data-move-up")
        self.assertContains(page, "data-move-down")
        self.assertContains(page, "type.value === 'single_choice' || type.value === 'multiple_choice'")
        response = self.client.post(self.settings_url(), self.settings_payload([
            {"wording": "First question", "question_type": "short_text", "order": 2},
            {"wording": "Remove this question", "question_type": "number", "order": 3, "remove": True},
            {"wording": "Choice question", "question_type": "single_choice", "choices": "One\nTwo", "order": 1},
        ], initial=3))
        self.assertRedirects(response, self.settings_url())
        self.assertEqual(
            list(self.month.signup_questions.values_list("wording", flat=True)),
            ["Choice question", "First question"],
        )

    def test_all_question_types_and_required_optional_answers_are_stored(self):
        self.month.enrollments.all().delete()
        short = self.question("Favorite trope?", "short_text", required=True, position=1)
        number = self.question("Anticipated pages?", "number", position=2)
        single = self.question("Preferred shift?", "single_choice", True, ["Day", "Night"], 3)
        multiple = self.question("Available days?", "multiple_choice", False, ["Friday", "Saturday", "Sunday"], 4)
        self.client.force_login(self.reader_user)
        missing = self.client.post(self.register_url(), {f"question_{single.pk}": "Day"})
        self.assertContains(missing, "This field is required")
        self.assertFalse(MonthEnrollment.objects.filter(month=self.month, participant=self.reader).exists())
        response = self.client.post(self.register_url(), {
            f"question_{short.pk}": "Found family",
            f"question_{number.pk}": "1250.5",
            f"question_{single.pk}": "Night",
            f"question_{multiple.pk}": ["Friday", "Sunday"],
        })
        self.assertRedirects(response, self.month.get_absolute_url())
        enrollment = MonthEnrollment.objects.get(month=self.month, participant=self.reader)
        answers = {answer.question_id: answer.value for answer in enrollment.signup_answers.all()}
        self.assertEqual(answers[short.pk], "Found family")
        self.assertEqual(answers[number.pk], "1250.5")
        self.assertEqual(answers[single.pk], "Night")
        self.assertEqual(answers[multiple.pk], ["Friday", "Sunday"])

    def test_choice_validation_and_ten_question_limit(self):
        self.month.enrollments.all().delete()
        self.client.force_login(self.owner_user)
        invalid = self.settings_payload([{
            "wording": "Choose one",
            "question_type": "single_choice",
            "choices": "Only one",
        }])
        response = self.client.post(self.settings_url(), invalid)
        self.assertContains(response, "at least two nonblank choices")
        self.assertFalse(self.month.signup_questions.exists())

        eleven = [
            {"wording": f"Question {index}", "question_type": "short_text"}
            for index in range(1, 12)
        ]
        response = self.client.post(self.settings_url(), self.settings_payload(eleven))
        self.assertContains(response, "at most 10 forms")
        self.assertFalse(self.month.signup_questions.exists())

    def test_registration_configuration_authority_excludes_ordinary_challenge_support_roles(self):
        MonthEnrollment.objects.create(month=self.month, participant=self.leader, origin=MonthEnrollment.Origin.STAFF)
        TeamAssignment.objects.create(month=self.month, participant=self.leader, team=self.team, assigned_by=self.host_user)
        ChallengeStaffAssignment.objects.create(
            month=self.month,
            membership=self.leader,
            team=self.team,
            role=ChallengeStaffAssignment.Role.TEAM_LEADER,
            assigned_by=self.host_user,
        )
        for viewer in (self.owner_user, self.moderator_user, self.host_user, self.platform_owner):
            with self.subTest(viewer=viewer.username):
                self.client.force_login(viewer)
                self.assertEqual(self.client.get(self.settings_url()).status_code, 200)
        for viewer in (self.other_user, self.floater_user, self.leader_user):
            with self.subTest(viewer=viewer.username):
                self.client.force_login(viewer)
                self.assertEqual(self.client.get(self.settings_url()).status_code, 403)

    def test_configuration_reorders_questions_and_locks_schema_and_policy_after_registration(self):
        self.month.enrollments.all().delete()
        self.client.force_login(self.owner_user)
        payload = self.settings_payload([
            {"wording": "Second", "question_type": "short_text", "order": 2},
            {"wording": "First", "question_type": "number", "order": 1},
        ], policy="timed", hours=48)
        self.assertRedirects(self.client.post(self.settings_url(), payload), self.settings_url())
        self.assertEqual(list(self.month.signup_questions.values_list("wording", flat=True)), ["First", "Second"])
        self.month.refresh_from_db()
        self.assertEqual(self.month.registration_answer_editing_hours, 48)
        MonthEnrollment.objects.create(month=self.month, participant=self.reader, origin=MonthEnrollment.Origin.SELF)
        locked = self.client.get(self.settings_url())
        self.assertContains(locked, "Registration configuration locked")
        self.assertNotContains(locked, "Save Registration Settings")
        self.client.post(self.settings_url(), self.settings_payload([], policy="none", hours=1))
        self.month.refresh_from_db()
        self.assertEqual(self.month.registration_answer_editing_policy, "timed")
        self.assertEqual(self.month.signup_questions.count(), 2)
        locked_question = self.month.signup_questions.first()
        locked_question.wording = "Rewrite attempt"
        with self.assertRaises(ValidationError):
            locked_question.save()
        with self.assertRaises(ValidationError):
            locked_question.delete()
        self.month.registration_answer_editing_policy = ChallengeMonth.RegistrationAnswerEditingPolicy.NONE
        with self.assertRaises(ValidationError):
            self.month.save(update_fields=["registration_answer_editing_policy"])

    def test_timed_policy_uses_variable_duration_and_original_registration_time(self):
        self.month.enrollments.all().delete()
        self.month.registration_answer_editing_policy = ChallengeMonth.RegistrationAnswerEditingPolicy.TIMED
        self.month.registration_answer_editing_hours = 48
        self.month.save(update_fields=["registration_answer_editing_policy", "registration_answer_editing_hours"])
        enrollment = MonthEnrollment.objects.create(month=self.month, participant=self.reader, origin=MonthEnrollment.Origin.SELF)
        original = timezone.now() - timedelta(hours=47)
        MonthEnrollment.objects.filter(pk=enrollment.pk).update(enrolled_at=original)
        enrollment.refresh_from_db()
        self.assertTrue(enrollment.can_reader_edit_registration_answers())
        self.assertEqual(enrollment.registration_answer_editing_deadline, original + timedelta(hours=48))
        MonthEnrollment.objects.filter(pk=enrollment.pk).update(enrolled_at=timezone.now() - timedelta(hours=49))
        enrollment.refresh_from_db()
        self.assertFalse(enrollment.can_reader_edit_registration_answers())

    def test_no_edit_and_until_close_policies_control_reader_edit_action(self):
        self.month.enrollments.all().delete()
        self.month.registration_answer_editing_policy = ChallengeMonth.RegistrationAnswerEditingPolicy.NONE
        self.month.save(update_fields=["registration_answer_editing_policy"])
        enrollment = MonthEnrollment.objects.create(month=self.month, participant=self.reader, origin=MonthEnrollment.Origin.SELF)
        self.client.force_login(self.reader_user)
        edit_url = reverse("challenge-registration-edit", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})
        self.assertRedirects(self.client.get(edit_url), self.month.get_absolute_url())
        MonthEnrollment.objects.filter(pk=enrollment.pk).delete()
        self.month.registration_answer_editing_policy = ChallengeMonth.RegistrationAnswerEditingPolicy.UNTIL_CLOSE
        self.month.registration_is_open = True
        self.month.save(update_fields=["registration_answer_editing_policy", "registration_is_open"])
        enrollment = MonthEnrollment.objects.create(month=self.month, participant=self.reader, origin=MonthEnrollment.Origin.SELF)
        self.assertEqual(self.client.get(edit_url).status_code, 200)
        self.month.registration_is_open = False
        self.month.save(update_fields=["registration_is_open"])
        self.assertRedirects(self.client.get(edit_url), self.month.get_absolute_url())
        self.assertEqual(MonthEnrollment.objects.get(pk=enrollment.pk).pk, enrollment.pk)

    def test_withdrawal_and_reregistration_preserve_answers_and_do_not_reset_timed_window(self):
        self.month.enrollments.all().delete()
        question = self.question("Planning note", "short_text", True)
        self.client.force_login(self.reader_user)
        self.client.post(self.register_url(), {f"question_{question.pk}": "Preserve me"})
        enrollment = MonthEnrollment.objects.get(month=self.month, participant=self.reader)
        original_pk = enrollment.pk
        original_time = timezone.now() - timedelta(hours=30)
        MonthEnrollment.objects.filter(pk=enrollment.pk).update(enrolled_at=original_time)
        self.client.post(reverse("challenge-withdraw", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}))
        page = self.client.get(self.register_url())
        self.assertContains(page, "Preserve me")
        self.assertContains(page, "responses are locked")
        self.client.post(self.register_url(), {f"question_{question.pk}": "Rewrite attempt"})
        enrollment.refresh_from_db()
        self.assertEqual(enrollment.pk, original_pk)
        self.assertEqual(enrollment.enrolled_at, original_time)
        self.assertTrue(enrollment.is_active)
        self.assertEqual(enrollment.signup_answers.get(question=question).value, "Preserve me")

    def test_registration_reuses_or_optionally_captures_discord_without_changing_privacy(self):
        self.month.enrollments.all().delete()
        profile = self.reader_user.northbound_profile
        profile.discord_username_is_public = False
        profile.save(update_fields=["discord_username_is_public"])
        self.client.force_login(self.reader_user)
        page = self.client.get(self.register_url())
        self.assertContains(page, "Discord Username (Optional)")
        self.client.post(self.register_url(), {"discord_username": "captured.reader"})
        profile.refresh_from_db()
        self.assertEqual(profile.discord_username, "captured.reader")
        self.assertFalse(profile.discord_username_is_public)

        self.month.enrollments.all().delete()
        page = self.client.get(self.register_url())
        self.assertNotContains(page, "Discord Username (Optional)")

    def test_registration_answers_are_private_with_bounded_staff_read_only_visibility(self):
        question = self.question("Private plan", "short_text", position=1)
        enrollment = MonthEnrollment.objects.create(month=self.month, participant=self.reader, origin=MonthEnrollment.Origin.SELF)
        ChallengeSignupAnswer.objects.create(enrollment=enrollment, question=question, value="Sensitive planning value")
        MonthEnrollment.objects.create(month=self.month, participant=self.leader, origin=MonthEnrollment.Origin.STAFF)
        TeamAssignment.objects.create(month=self.month, participant=self.leader, team=self.team, assigned_by=self.host_user)
        ChallengeStaffAssignment.objects.create(
            month=self.month,
            membership=self.leader,
            team=self.team,
            role=ChallengeStaffAssignment.Role.TEAM_LEADER,
            assigned_by=self.host_user,
        )
        url = self.detail_url(enrollment)
        for viewer in (self.owner_user, self.moderator_user, self.host_user, self.platform_owner):
            with self.subTest(viewer=viewer.username):
                self.client.force_login(viewer)
                self.assertContains(self.client.get(url), "Sensitive planning value")
        for viewer in (self.other_user, self.floater_user, self.leader_user):
            with self.subTest(viewer=viewer.username):
                self.client.force_login(viewer)
                self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(self.owner_user)
        self.client.post(url, {f"question_{question.pk}": "Staff rewrite"})
        self.assertEqual(enrollment.signup_answers.get(question=question).value, "Sensitive planning value")

    def test_platform_owner_can_correct_answers_with_audit_without_changing_relationships(self):
        question = self.question("Correction target", "short_text", True)
        enrollment = MonthEnrollment.objects.create(month=self.month, participant=self.reader, origin=MonthEnrollment.Origin.SELF, is_active=False, inactive_reason=MonthEnrollment.InactiveReason.WITHDRAWN)
        answer = ChallengeSignupAnswer.objects.create(enrollment=enrollment, question=question, value="Original private value")
        original_enrolled_at = enrollment.enrolled_at
        self.client.force_login(self.platform_owner)
        response = self.client.post(self.detail_url(enrollment), {f"question_{question.pk}": "Corrected private value"})
        self.assertRedirects(response, self.detail_url(enrollment))
        answer.refresh_from_db()
        enrollment.refresh_from_db()
        self.assertEqual(answer.value, "Corrected private value")
        self.assertFalse(enrollment.is_active)
        self.assertEqual(enrollment.enrolled_at, original_enrolled_at)
        self.assertFalse(Membership.objects.filter(user=self.platform_owner).exists())
        self.assertFalse(ChallengeStaffAssignment.objects.filter(membership__user=self.platform_owner).exists())
        event = AuditEvent.objects.get(action="registration.answers_admin_corrected", object_id=str(enrollment.pk))
        self.assertNotIn("Corrected private value", event.summary)
        self.assertNotIn("Original private value", event.summary)

    def test_question_model_rejects_invalid_choice_schema(self):
        with self.assertRaises(ValidationError):
            self.question("Invalid", "multiple_choice", choices=["Only one"])
