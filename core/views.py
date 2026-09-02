from django.contrib import messages
from django.conf import settings
from django.core import signing
from django.core.exceptions import ValidationError
from django.contrib.auth import get_user_model, login
from django.contrib.auth.views import LoginView, PasswordChangeView
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db import connection
from django.db.models import Count, F, OuterRef, Prefetch, Q, Subquery
from django.db.models.deletion import ProtectedError
from django.forms import inlineformset_factory
from django.http import FileResponse, Http404, HttpResponse, HttpResponseForbidden, JsonResponse, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.template.defaultfilters import filesizeformat
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_GET
import secrets
import hashlib
import csv
import json
import sqlite3
import zipfile
import os
import signal
import threading
from uuid import uuid4
from pathlib import Path
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .forms import AccountProfileForm, BookSubmissionForm, BotmBookForm, BotmReactivateForm, ChallengeAnnouncementForm, ChallengeBotmSettingsForm, ChallengeCreateForm, ChallengeFloaterAssignmentForm, ChallengeGamesSettingsForm, ChallengeGeneralSettingsForm, ChallengeHostAssignmentForm, ChallengeMonthForm, ChallengeRegistrationForm, ChallengeRegistrationSettingsForm, ChallengeScheduleForm, ChallengeSignupQuestionFormSet, ChallengeTbrSettingsForm, ChallengeTeamLeaderAssignmentForm, CompetitionVisibilityForm, FirstRunSetupForm, GameForm, GameRewardApplyForm, GameRewardVoidForm, GroupAccessCodeForm, GroupCreateForm, GroupEditForm, GroupJoinForm, HardcoverConnectionForm, HardcoverOAuthApplicationForm, MemberCreateForm, MembershipPermissionsForm, MembershipRoleForm, MonthEnrollmentForm, MonthParticipantEditForm, MonthThemeForm, PersonalTbrRegistrationBookFormSet, PlatformAccountIdentityForm, PlatformBackupSettingsForm, PlatformOwnerAcceptanceForm, PlatformOwnerInvitationForm, PlatformSettingsForm, ProgressCheckpointFormSet, PublicRegistrationForm, ReaderHardcoverConnectionForm, ReaderHardcoverSyncPreferenceForm, RootAuthenticationForm, SubmissionReviewForm, TeamAssignmentForm, TeamForm, ThemeClaimReviewForm
from .integrations.hardcover import HardcoverConnectionError, HardcoverLinkError, list_book_editions, lookup_edition, lookup_hardcover_url, resolve_scoring_edition, search_books, test_catalog_connection
from .integrations.secrets import TokenDecryptionError, decrypt_token, encrypt_token
from .models import AuditEvent, BookSubmission, BotmBook, BotmCompletionAward, BotmMatch, CatalogEdition, ChallengeMonth, ChallengeSignupAnswer, ChallengeSignupQuestion, ChallengeStaffAssignment, Game, GameRewardApplication, HardcoverConnection, HardcoverOAuthApplication, Membership, ModifierProvenance, MonthEnrollment, MonthTheme, PersonalTBR, PersonalTBRBook, PersonalTBRCompletionAward, PersonalTBRMatch, PlatformBackupSettings, PlatformOwnerInvitation, ProgressCheckpoint, ProgressCheckpointResult, ReaderHardcoverConnection, ReaderHardcoverSyncPreference, ReadingGroup, RecoveryOperation, Team, TeamAssignment, ThemeClaim, UserProfile, audit_action_label, hash_platform_owner_invitation_token, safe_audit_summary
from .permissions import can_configure_competition_visibility, can_manage_challenge_announcements, can_manage_challenge_hosts, can_manage_group, can_manage_group_announcements, can_manage_months, can_manage_participants, can_manage_permissions, can_operate_challenge, can_review_challenge, can_review_submission, can_transition_challenge, can_view_access_code, can_view_challenge, can_view_reader_scores, can_view_team_standings, challenge_review_scope, membership_for, scope_reviewable_submissions, visible_challenges_for
from .backups import automatic_backup_directory, create_stored_backup, list_automatic_backups, list_stored_backups, next_scheduled_backup, pending_restore_path, stage_restore, stage_stored_restore, stored_backup_path
from .platform_config import get_platform_settings, get_platform_timezone
from .maintenance import AUDIT_RETENTION_YEARS, audit_prune_preview, cleanup_disposable_cache, disposable_cache_usage, optimize_sqlite_database, prune_audit_history, storage_overview
from .maintenance_lock import MaintenanceBusyError
from .system_status import build_system_status
from .review_attention import needs_attention_summary
from .participation import activate_participation, assign_participant_to_team, deactivate_participation, end_team_assignment
from .reader_planning import historical_reader_planning_data
from .scoring import apply_submission_review
from .score_aggregation import challenge_score_totals
from .game_rewards import apply_game_reward, game_reward_application_unavailable_reason, preview_game_reward, void_game_reward
from .botm_configuration import add_botm_book, delete_unused_botm_book, reactivate_botm_book, retire_botm_book, update_botm_book
from .botm_matching import adjudicate_match, synchronize_challenge, synchronize_submission
from .botm_presentation import build_botm_reader_presentation
from .reader_hardcover import ReaderHardcoverUnavailable, save_reader_hardcover_connection, test_reader_hardcover_connection
from .reader_hardcover import get_reader_hardcover_token
from .hardcover_oauth import (
    OAUTH_ISSUER, OAUTH_SCOPES, STATE_TTL_SECONDS, HardcoverOAuthError, authorization_url,
    canonical_oauth_urls, exchange_authorization_code, generate_pkce,
    oauth_application_status, revoke_oauth_token, save_oauth_application,
)
from .hardcover_sync import make_existing_reader_events_due, reader_sync_capability, reader_sync_health
from .health import health_response
from .personal_tbr import confirm_personal_tbr, replace_draft_personal_tbr
from .personal_tbr_matching import adjudicate_match as adjudicate_personal_tbr_match
from .personal_tbr_matching import synchronize_submission as synchronize_personal_tbr_submission
from .personal_tbr_presentation import build_personal_tbr_reader_presentation
from .recovery import stored_backup_advisory
from .recovery import RecoveryRequest
from .recovery_domain import (
    challenge_purge_impact, correct_enrollment_origin, correct_membership_role, correct_staffing_role,
    delete_unused_team, purge_challenge, reassign_team_assignment,
    set_enrollment_active, set_group_active, set_membership_active,
    set_staffing_active, set_team_archived, set_team_assignment_active,
    set_user_active, transfer_group_ownership,
)
from .recovery_scoring import (
    correct_unused_theme, purge_malformed_provenance, purge_submission,
    rebuild_provenance, recover_theme_claim, set_submission_removed,
    set_theme_active, submission_purge_impact, submission_recovery_label,
    void_provenance,
)
from .recovery_modifiers import (
    botm_match_label, correct_botm_book, game_application_label,
    purge_botm_match, purge_tbr_match, rebuild_locked_tbr,
    recover_botm_match, recover_game_application, recover_tbr_match,
    recreate_game_application, repair_tbr_entry, set_botm_book_retired,
    set_game_active, tbr_label, tbr_match_label,
)
from .recovery_checkpoints import (
    checkpoint_configuration_snapshot, checkpoint_recovery_label,
    checkpoint_reset_impact, checkpoint_result_summary,
    release_checkpoint_evaluation, reset_checkpoint_evaluation,
)
from .recovery_credentials import (
    clear_group_hardcover_connection, clear_reader_hardcover_connection,
    group_credential_label, reader_credential_label,
    sanitized_credential_status,
)
from .recovery_forms import (
    BotmMatchRecoveryForm, EnrollmentOriginRecoveryForm, GameReplacementRecoveryForm,
    LockedTbrListBookFormSet, MembershipRoleRecoveryForm, OwnershipTransferForm,
    RecoveryBookIdentityForm, RecoveryConfirmationForm, SafeDeleteConfirmationForm,
    StaffingRoleRecoveryForm, TbrMatchRecoveryForm, TeamReassignmentRecoveryForm,
    ThemeClaimRecoveryForm, UnusedThemeRecoveryForm,
)


CONFIGURABLE_MONTH_STATUSES = {
    ChallengeMonth.Status.DRAFT,
    ChallengeMonth.Status.UPCOMING,
    ChallengeMonth.Status.ACTIVE,
}
REVIEWABLE_MONTH_STATUSES = {ChallengeMonth.Status.ACTIVE, ChallengeMonth.Status.FINALIZING}


def month_is_configurable(month):
    return month.status in CONFIGURABLE_MONTH_STATUSES


def can_configure_challenge_registration(user, month):
    return can_manage_months(user, month.group) or can_operate_challenge(user, month)


def can_view_challenge_registration_answers(user, month):
    return can_manage_months(user, month.group) or can_operate_challenge(user, month)


def reject_locked_month(request, month, action="change this Challenge"):
    if month_is_configurable(month):
        return False
    messages.error(request, f"{month.get_status_display()} Challenges are read-only. You cannot {action}.")
    return True


def lifecycle_transition_targets(month):
    order = month.lifecycle_order()
    current_index = order.index(month.status)
    if month.status == ChallengeMonth.Status.ARCHIVED:
        return []
    targets = []
    if current_index > 0:
        targets.append({"value": order[current_index - 1], "label": dict(ChallengeMonth.Status.choices)[order[current_index - 1]], "backward": True})
    if current_index < len(order) - 1:
        targets.append({"value": order[current_index + 1], "label": dict(ChallengeMonth.Status.choices)[order[current_index + 1]], "backward": False})
    return targets


@login_required
def needs_attention(request):
    summary = needs_attention_summary(request.user)
    return render(request, "core/needs_attention.html", summary)


class RootLoginView(LoginView):
    authentication_form = RootAuthenticationForm
    template_name = "registration/config_login.html"

    def get_success_url(self):
        return reverse("config-dashboard")

    def form_valid(self, form):
        response = super().form_valid(form)
        AuditEvent.objects.create(
            actor=self.request.user,
            action="platform.root_login",
            object_type="User",
            object_id=str(self.request.user.pk),
            summary="Platform owner signed into platform administration.",
        )
        return response


class NorthboundPasswordChangeView(PasswordChangeView):
    template_name = "registration/password_change.html"
    success_url = "/account/"

    def form_valid(self, form):
        response = super().form_valid(form)
        profile, _ = UserProfile.objects.get_or_create(user=self.request.user)
        if profile.must_change_password:
            profile.must_change_password = False
            profile.save(update_fields=["must_change_password"])
            AuditEvent.objects.create(
                actor=self.request.user,
                action="account.temporary_password_replaced",
                object_type="User",
                object_id=str(self.request.user.pk),
                summary="User replaced a platform-owner-issued temporary password.",
            )
        messages.success(self.request, "Your password was changed.")
        return response


@login_required
def account(request):
    form = AccountProfileForm(request.POST or None, request.FILES or None, instance=request.user)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Your account details were updated.")
        return redirect("account")
    connection = ReaderHardcoverConnection.objects.filter(user=request.user).first()
    oauth_application = HardcoverOAuthApplication.objects.first()
    oauth_urls = canonical_oauth_urls()
    oauth_status = oauth_application_status(oauth_application, oauth_urls)
    sync_preference = ReaderHardcoverSyncPreference.objects.filter(user=request.user).first()
    sync_preference = sync_preference or ReaderHardcoverSyncPreference(user=request.user)
    sync_write_available, sync_unavailable_reason = reader_sync_capability(connection)
    return render(request, "core/account.html", {
        "form": form,
        "reader_hardcover_form": ReaderHardcoverConnectionForm(),
        "reader_hardcover_connection": connection,
        "reader_hardcover_oauth_available": oauth_status["key"] == "configured",
        "reader_hardcover_oauth_status": oauth_status,
        "reader_hardcover_sync_form": ReaderHardcoverSyncPreferenceForm(
            instance=sync_preference,
            write_available=sync_write_available,
            unavailable_reason=sync_unavailable_reason,
        ),
        "reader_hardcover_sync_write_available": sync_write_available,
        "reader_hardcover_sync_unavailable_reason": sync_unavailable_reason,
        "reader_hardcover_sync_health": reader_sync_health(request.user),
    })


READER_OAUTH_SESSION_KEY = "reader_hardcover_oauth_flows"


@login_required
def reader_hardcover_oauth_start(request):
    if request.method != "POST":
        return redirect("account")
    application = HardcoverOAuthApplication.objects.first()
    urls = canonical_oauth_urls()
    if oauth_application_status(application, urls)["key"] != "configured":
        messages.error(request, "Reader OAuth is unavailable until the installation configuration is corrected.")
        return redirect("account")
    state = secrets.token_urlsafe(48)
    state_hash = hashlib.sha256(state.encode("ascii")).hexdigest()
    verifier, challenge = generate_pkce()
    now = int(timezone.now().timestamp())
    from .models import HardcoverOAuthStateUse
    HardcoverOAuthStateUse.objects.filter(created_at__lt=timezone.now() - timedelta(days=1)).delete()
    flows = request.session.get(READER_OAUTH_SESSION_KEY, {})
    flows = {
        key: value for key, value in flows.items()
        if isinstance(value, dict) and now - int(value.get("created_at", 0)) <= STATE_TTL_SECONDS
    }
    flows[state_hash] = {
        "user_id": request.user.pk,
        "created_at": now,
        "code_verifier": encrypt_token(verifier),
        "application_fingerprint": hashlib.sha256((application.client_id + application.encrypted_client_secret).encode()).hexdigest(),
        "redirect_uri": urls.redirect_uri,
        "return_to": reverse("account"),
    }
    request.session[READER_OAUTH_SESSION_KEY] = dict(list(flows.items())[-5:])
    return redirect(authorization_url(
        application=application,
        redirect_uri=urls.redirect_uri,
        state=state,
        code_challenge=challenge,
    ))


@login_required
@sensitive_variables("code", "state", "flow", "token_set")
def reader_hardcover_oauth_callback(request):
    state = request.GET.get("state", "")
    code = request.GET.get("code", "")
    if request.GET.get("iss") != OAUTH_ISSUER:
        messages.error(request, "That Hardcover authorization came from an unexpected issuer. Start again from My Account.")
        return redirect("account")
    state_hash = hashlib.sha256(state.encode("utf-8")).hexdigest() if state else ""
    flows = request.session.get(READER_OAUTH_SESSION_KEY, {})
    flow = flows.pop(state_hash, None)
    request.session[READER_OAUTH_SESSION_KEY] = flows
    now = int(timezone.now().timestamp())
    if not flow or flow.get("user_id") != request.user.pk or now - int(flow.get("created_at", 0)) > STATE_TTL_SECONDS:
        messages.error(request, "That Hardcover authorization could not be verified. Start again from My Account.")
        return redirect("account")
    from .models import HardcoverOAuthStateUse
    _, claimed = HardcoverOAuthStateUse.objects.get_or_create(state_hash=state_hash)
    if not claimed:
        messages.error(request, "That Hardcover authorization could not be verified. Start again from My Account.")
        return redirect("account")
    # Never accept a return destination from a serialized session flow.
    flow["return_to"] = reverse("account")
    if request.GET.get("error"):
        messages.error(request, "Hardcover did not authorize the connection. Your previous connection was not changed.")
        return redirect(flow["return_to"])
    if not code:
        messages.error(request, "Hardcover did not return an authorization code. Your previous connection was not changed.")
        return redirect(flow["return_to"])
    application = HardcoverOAuthApplication.objects.first()
    urls = canonical_oauth_urls()
    if oauth_application_status(application, urls)["key"] != "configured" or flow.get("redirect_uri") != urls.redirect_uri or flow.get("application_fingerprint") != hashlib.sha256((application.client_id + application.encrypted_client_secret).encode()).hexdigest():
        messages.error(request, "The installation OAuth configuration changed during authorization. Start again.")
        return redirect(flow["return_to"])
    try:
        token_set = exchange_authorization_code(
            application=application,
            code=code,
            code_verifier=decrypt_token(flow["code_verifier"]),
            redirect_uri=urls.redirect_uri,
        )
        test_catalog_connection(token_set.access_token)
    except (HardcoverOAuthError, HardcoverConnectionError, TokenDecryptionError):
        messages.error(request, "Hardcover could not validate the new OAuth connection. Your previous connection was not changed.")
        return redirect(flow["return_to"])
    with transaction.atomic():
        if not HardcoverOAuthStateUse.objects.filter(state_hash=state_hash, completed_or_cancelled=False).update(completed_or_cancelled=True):
            messages.error(request, "That Hardcover authorization was cancelled. Start again from My Account.")
            return redirect("account")
        previous = ReaderHardcoverConnection.objects.select_for_update().filter(user=request.user).first()
        connection, created = ReaderHardcoverConnection.objects.update_or_create(
            user=request.user,
            defaults={
                "connection_method": ReaderHardcoverConnection.ConnectionMethod.OAUTH,
                "encrypted_token": encrypt_token(token_set.access_token),
                "encrypted_refresh_token": encrypt_token(token_set.refresh_token) if token_set.refresh_token else "",
                "token_hint": "",
                "access_expires_at": token_set.expires_at,
                "granted_scopes": list(token_set.scopes),
                "refreshed_at": None,
                "tested_at": timezone.now(),
                "is_valid": True,
                "reconnect_required": False,
                "last_error": "",
            },
        )
        AuditEvent.objects.create(
            actor=request.user,
            action="reader_hardcover.connected" if created else "reader_hardcover.replaced",
            object_type="ReaderHardcoverConnection",
            object_id=str(connection.pk),
            summary="Connected a personal Hardcover account with OAuth." if created else "Replaced the personal Hardcover connection with OAuth.",
        )
    messages.success(request, "Your personal Hardcover account was connected with OAuth.")
    make_existing_reader_events_due(request.user)
    return redirect(flow["return_to"])


@login_required
@sensitive_post_parameters("api_token")
@sensitive_variables("token")
def reader_hardcover_connect(request):
    if request.method != "POST":
        return redirect("account")
    form = ReaderHardcoverConnectionForm(request.POST)
    connection = ReaderHardcoverConnection.objects.filter(user=request.user).first()
    if form.is_valid():
        token = form.cleaned_data["api_token"]
        try:
            saved_connection, created = save_reader_hardcover_connection(request.user, token)
        except HardcoverConnectionError:
            form.add_error("api_token", "Hardcover could not validate that token. Check it and try again.")
        else:
            AuditEvent.objects.create(
                actor=request.user,
                action="reader_hardcover.connected" if created else "reader_hardcover.replaced",
                object_type="ReaderHardcoverConnection",
                object_id=str(saved_connection.pk),
                summary="Connected a personal Hardcover account." if created else "Replaced the personal Hardcover credential.",
            )
            messages.success(request, "Your personal Hardcover account was connected successfully.")
            make_existing_reader_events_due(request.user)
            return redirect("account")
    sync_preference = ReaderHardcoverSyncPreference.objects.filter(user=request.user).first()
    sync_preference = sync_preference or ReaderHardcoverSyncPreference(user=request.user)
    sync_write_available, sync_unavailable_reason = reader_sync_capability(connection)
    return render(request, "core/account.html", {
        "form": AccountProfileForm(instance=request.user),
        "reader_hardcover_form": form,
        "reader_hardcover_connection": connection,
        "reader_hardcover_oauth_available": oauth_application_status(
            HardcoverOAuthApplication.objects.first(), canonical_oauth_urls()
        )["key"] == "configured",
        "reader_hardcover_sync_form": ReaderHardcoverSyncPreferenceForm(
            instance=sync_preference,
            write_available=sync_write_available,
            unavailable_reason=sync_unavailable_reason,
        ),
        "reader_hardcover_sync_write_available": sync_write_available,
        "reader_hardcover_sync_unavailable_reason": sync_unavailable_reason,
        "reader_hardcover_sync_health": reader_sync_health(request.user),
    }, status=400)


@login_required
@sensitive_variables("connection")
def reader_hardcover_test(request):
    if request.method != "POST":
        return redirect("account")
    try:
        connection = test_reader_hardcover_connection(request.user)
    except ReaderHardcoverUnavailable as exc:
        messages.error(request, str(exc))
    else:
        AuditEvent.objects.create(
            actor=request.user,
            action="reader_hardcover.tested",
            object_type="ReaderHardcoverConnection",
            object_id=str(connection.pk),
            summary="Tested the personal Hardcover connection successfully.",
        )
        messages.success(request, "Your personal Hardcover connection is working.")
    return redirect("account")


@login_required
def reader_hardcover_disconnect(request):
    if request.method != "POST":
        return redirect("account")
    from .models import HardcoverOAuthStateUse
    for state_hash in request.session.pop(READER_OAUTH_SESSION_KEY, {}):
        HardcoverOAuthStateUse.objects.get_or_create(state_hash=state_hash)
        HardcoverOAuthStateUse.objects.filter(state_hash=state_hash).update(completed_or_cancelled=True)
    connection = ReaderHardcoverConnection.objects.filter(user=request.user).first()
    if connection:
        object_id = str(connection.pk)
        # Disable local access and consent before waiting on provider revocation.
        # Keep this in-memory snapshot solely for best-effort revocation below.
        ReaderHardcoverConnection.objects.filter(pk=connection.pk).update(is_valid=False, reconnect_required=True)
        preference = ReaderHardcoverSyncPreference.objects.filter(user=request.user).first()
        if preference and (preference.sync_completed_books or preference.sync_completion_dates):
            if preference.sync_completed_books:
                AuditEvent.objects.create(
                    actor=request.user,
                    action="reader_hardcover.sync_completed_books_disabled",
                    object_type="ReaderHardcoverSyncPreference",
                    object_id=str(preference.pk),
                    summary="Disabled personal Hardcover completed-book synchronization during disconnect.",
                )
            if preference.sync_completion_dates:
                AuditEvent.objects.create(
                    actor=request.user,
                    action="reader_hardcover.sync_completion_dates_disabled",
                    object_type="ReaderHardcoverSyncPreference",
                    object_id=str(preference.pk),
                    summary="Disabled personal Hardcover completion-date synchronization during disconnect.",
                )
            preference.sync_completed_books = False
            preference.sync_completion_dates = False
            preference.save(update_fields=["sync_completed_books", "sync_completion_dates", "updated_at"])
        connection.delete()
        if connection.connection_method == ReaderHardcoverConnection.ConnectionMethod.OAUTH:
            application = HardcoverOAuthApplication.objects.first()
            if application:
                try:
                    access_token = decrypt_token(connection.encrypted_token)
                    refresh_token = decrypt_token(connection.encrypted_refresh_token) if connection.encrypted_refresh_token else ""
                except TokenDecryptionError:
                    access_token = refresh_token = ""
                revoke_oauth_token(application=application, token=refresh_token, token_type_hint="refresh_token")
                revoke_oauth_token(application=application, token=access_token, token_type_hint="access_token")
        AuditEvent.objects.create(
            actor=request.user,
            action="reader_hardcover.disconnected",
            object_type="ReaderHardcoverConnection",
            object_id=object_id,
            summary="Disconnected the personal Hardcover account.",
        )
        messages.success(request, "Your personal Hardcover account was disconnected.")
    return redirect("account")


@login_required
def reader_hardcover_sync_preferences(request):
    if request.method != "POST":
        return redirect("account")
    connection = ReaderHardcoverConnection.objects.filter(user=request.user).first()
    write_available, unavailable_reason = reader_sync_capability(connection)
    preference = ReaderHardcoverSyncPreference.objects.filter(user=request.user).first()
    preference = preference or ReaderHardcoverSyncPreference(user=request.user)
    previous_completed = preference.sync_completed_books
    previous_dates = preference.sync_completion_dates
    form = ReaderHardcoverSyncPreferenceForm(
        request.POST,
        instance=preference,
        write_available=write_available,
        unavailable_reason=unavailable_reason,
    )
    if form.is_valid():
        with transaction.atomic():
            saved = form.save()
            transitions = (
                ("sync_completed_books", previous_completed, saved.sync_completed_books),
                ("sync_completion_dates", previous_dates, saved.sync_completion_dates),
            )
            labels = {
                "sync_completed_books": "completed-book",
                "sync_completion_dates": "completion-date",
            }
            for field, before, after in transitions:
                if before == after:
                    continue
                state = "enabled" if after else "disabled"
                AuditEvent.objects.create(
                    actor=request.user,
                    action=f"reader_hardcover.{field}_{state}",
                    object_type="ReaderHardcoverSyncPreference",
                    object_id=str(saved.pk),
                    summary=f"{state.title()} personal Hardcover {labels[field]} synchronization.",
                )
        messages.success(request, "Your personal Hardcover synchronization preferences were updated.")
        return redirect("account")
    oauth_status = oauth_application_status(HardcoverOAuthApplication.objects.first(), canonical_oauth_urls())
    return render(request, "core/account.html", {
        "form": AccountProfileForm(instance=request.user),
        "reader_hardcover_form": ReaderHardcoverConnectionForm(),
        "reader_hardcover_connection": connection,
        "reader_hardcover_oauth_available": oauth_status["key"] == "configured",
        "reader_hardcover_oauth_status": oauth_status,
        "reader_hardcover_sync_form": form,
        "reader_hardcover_sync_write_available": write_available,
        "reader_hardcover_sync_unavailable_reason": unavailable_reason,
        "reader_hardcover_sync_health": reader_sync_health(request.user),
    }, status=400)


@login_required
def reader_hardcover_sync_now(request):
    if request.method != "POST":
        return redirect("account")
    count, reason = make_existing_reader_events_due(request.user)
    if reason:
        messages.error(request, reason)
    elif count:
        messages.success(request, f"Made {count} existing Hardcover synchronization event{'s' if count != 1 else ''} ready for the worker. No historical submissions were scanned or queued.")
    else:
        messages.info(request, "There is no existing eligible Hardcover synchronization work to process.")
    return redirect("account")


@login_required
def my_stats(request):
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    group_approved_filter = Q(challenge_months__submissions__participant__user=request.user, challenge_months__submissions__status=BookSubmission.Status.APPROVED, challenge_months__submissions__is_removed=False)
    approved_submissions = BookSubmission.objects.filter(
        participant__user=request.user,
        status=BookSubmission.Status.APPROVED,
        is_removed=False,
    )
    totals = approved_submissions.aggregate(books=Count("id"))
    groups = list(ReadingGroup.objects.filter(memberships__user=request.user).distinct().annotate(
        reader_books=Count("challenge_months__submissions", filter=group_approved_filter, distinct=True),
    ).order_by("name"))

    month_ids = set(MonthEnrollment.objects.filter(participant__user=request.user).values_list("month_id", flat=True))
    month_ids.update(BookSubmission.objects.filter(participant__user=request.user, is_removed=False).values_list("month_id", flat=True))
    months = list(ChallengeMonth.objects.filter(pk__in=month_ids).select_related("group").annotate(
        reader_books=Count("submissions", filter=Q(submissions__participant__user=request.user, submissions__status=BookSubmission.Status.APPROVED, submissions__is_removed=False), distinct=True),
    ).order_by("-starts_on", "group__name"))
    membership_by_group = {
        membership.group_id: membership.pk
        for membership in Membership.objects.filter(user=request.user, group_id__in={month.group_id for month in months})
    }
    group_pages = {group.pk: 0 for group in groups}
    for month in months:
        participant_id = membership_by_group.get(month.group_id)
        month_scores = challenge_score_totals(
            month=month,
            participant_ids=[participant_id] if participant_id else [],
        )
        month.reader_pages = month_scores.get(participant_id, {}).get("total_pages", 0)
        group_pages[month.group_id] = group_pages.get(month.group_id, 0) + month.reader_pages
    for group in groups:
        group.reader_pages = group_pages.get(group.pk, 0)
    assignments = TeamAssignment.objects.filter(
        month_id__in=month_ids,
        participant__user=request.user,
        ended_at__isnull=True,
    ).select_related("team")
    team_by_month = {assignment.month_id: assignment.team for assignment in assignments}
    for month in months:
        month.reader_team = team_by_month.get(month.pk)

    submissions = BookSubmission.objects.filter(participant__user=request.user, is_removed=False).select_related(
        "month__group", "catalog_book", "catalog_edition", "scoring_catalog_edition"
    ).order_by("-completed_on", "-submitted_at")
    return render(request, "core/my_stats.html", {
        "approved_books": totals["books"] or 0,
        "approved_pages": sum(month.reader_pages for month in months),
        "group_count": len(groups),
        "month_count": len(months),
        "groups": groups,
        "months": months,
        "submissions": submissions,
        "profile": profile,
    })


@login_required(login_url="config-login")
def config_dashboard(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Platform owner access is required.")
    account_counts = get_user_model().objects.filter(is_superuser=False).aggregate(
        active=Count("id", filter=Q(is_active=True)),
        deactivated=Count("id", filter=Q(is_active=False)),
    )
    context = {
        "active_account_count": account_counts["active"],
        "deactivated_account_count": account_counts["deactivated"],
        "group_count": ReadingGroup.objects.count(),
        "recent_events": AuditEvent.objects.select_related("actor", "group")[:12],
        "platform_timezone": get_platform_settings().timezone,
    }
    return render(request, "core/config_dashboard.html", context)


@login_required(login_url="config-login")
def advanced_recovery(request):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    return render(request, "core/advanced_recovery.html", {
        "backup_advisory": stored_backup_advisory(),
        "platform_timezone": get_platform_settings().timezone,
    })


@login_required(login_url="config-login")
def recovery_history(request):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    operations = RecoveryOperation.objects.select_related("actor", "group", "challenge")
    page_obj = Paginator(operations, 50).get_page(request.GET.get("page"))
    return render(request, "core/recovery_history.html", {
        "operations": page_obj.object_list,
        "page_obj": page_obj,
        "platform_timezone": get_platform_settings().timezone,
    })


def _recovery_owner_denied(request):
    if not request.user.is_active or not request.user.is_superuser:
        return HttpResponseForbidden("Platform owner access is required.")
    return None


def _recovery_request_from_form(
    request, form, *, tier, action, target_type, target_id, target_label,
    group=None, challenge=None, impact=None, before_state=None,
):
    return RecoveryRequest(
        actor=request.user,
        tier=tier,
        action=action,
        target_type=target_type,
        target_id=str(target_id),
        target_label=target_label,
        reason=form.cleaned_data.get("reason", ""),
        required_confirmation=target_label,
        supplied_confirmation=form.cleaned_data["confirmation"],
        current_password=form.cleaned_data.get("current_password", ""),
        confirmation_method="exact target label" + (" plus current password" if tier == 3 else ""),
        group=group,
        challenge=challenge,
        impact=impact or {},
        before_state=before_state or {},
    )


def _add_recovery_error(form, error):
    if hasattr(error, "message_dict"):
        for field, messages_list in error.message_dict.items():
            target = field if field in form.fields else None
            for message in messages_list:
                form.add_error(target, message)
    else:
        for message in error.messages:
            form.add_error(None, message)


def _recovery_confirmation_page(request, *, form, title, description, cancel_url, tier, impact=None, action_label="Confirm Recovery", checkpoint=None):
    return render(request, "core/recovery_confirmation.html", {
        "form": form, "title": title, "description": description,
        "cancel_url": cancel_url, "tier": tier, "impact": impact,
        "action_label": action_label,
        "backup_advisory": stored_backup_advisory() if tier == 3 else None,
        "platform_timezone": get_platform_settings().timezone,
        "recovery_checkpoint": checkpoint,
    })


@login_required(login_url="config-login")
def recovery_challenge_list(request):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    challenges = ChallengeMonth.objects.select_related("group").order_by("group__name", "-starts_on", "name")
    return render(request, "core/recovery_challenge_list.html", {"challenges": challenges})


@login_required(login_url="config-login")
def recovery_challenge_detail(request, pk):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    month = get_object_or_404(ChallengeMonth.objects.select_related("group"), pk=pk)
    return render(request, "core/recovery_challenge_detail.html", {
        "month": month,
        "impact": challenge_purge_impact(month),
        "staffing": month.staff_assignments.select_related("membership", "team").order_by("ended_at", "membership__display_name"),
        "enrollments": month.enrollments.select_related("participant__user").order_by("participant__display_name"),
        "teams": month.teams.order_by("name"),
        "assignments": month.team_assignments.select_related("participant", "team").order_by("ended_at", "participant__display_name"),
    })


@sensitive_post_parameters("current_password")
@login_required(login_url="config-login")
def recovery_challenge_purge(request, pk):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    month = get_object_or_404(ChallengeMonth.objects.select_related("group"), pk=pk)
    impact = challenge_purge_impact(month)
    form = RecoveryConfirmationForm(request.POST or None, expected_confirmation=month.name, require_password=True)
    if request.method == "POST" and form.is_valid():
        operation_request = _recovery_request_from_form(
            request, form, tier=3, action="challenge.purge", target_type="ChallengeMonth",
            target_id=month.pk, target_label=month.name, group=month.group,
            impact=impact, before_state={"name": month.name, "status": month.status},
        )
        try:
            purge_challenge(month=month, recovery_request=operation_request)
        except ValidationError as error:
            _add_recovery_error(form, error)
        else:
            messages.success(request, f"Challenge {month.name} was permanently purged through Platform recovery.")
            return redirect("recovery-challenge-list")
    return _recovery_confirmation_page(
        request, form=form, tier=3, impact=impact,
        title=f"Permanently purge {month.name}?",
        description="This permanently removes the Challenge and its owned competition data. Shared accounts, Group memberships, catalog records, and unrelated Challenges remain.",
        cancel_url=reverse("recovery-challenge-detail", kwargs={"pk": month.pk}),
        action_label="Permanently Purge Challenge",
    )


@login_required(login_url="config-login")
def recovery_group_list(request):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    return render(request, "core/recovery_group_list.html", {"groups": ReadingGroup.objects.order_by("name")})


@login_required(login_url="config-login")
def recovery_group_detail(request, pk):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    group = get_object_or_404(ReadingGroup, pk=pk)
    return render(request, "core/recovery_group_detail.html", {
        "group": group,
        "memberships": group.memberships.select_related("user").order_by("display_name"),
    })


@login_required(login_url="config-login")
def recovery_group_status(request, pk):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    group = get_object_or_404(ReadingGroup, pk=pk)
    active = not group.is_active
    verb = "Reactivate" if active else "Deactivate"
    form = RecoveryConfirmationForm(request.POST or None, expected_confirmation=group.name)
    if request.method == "POST" and form.is_valid():
        operation_request = _recovery_request_from_form(
            request, form, tier=2, action=f"group.{'reactivate' if active else 'deactivate'}",
            target_type="ReadingGroup", target_id=group.pk, target_label=group.name,
            group=group, before_state={"is_active": group.is_active},
        )
        try:
            set_group_active(group=group, active=active, recovery_request=operation_request)
        except ValidationError as error:
            _add_recovery_error(form, error)
        else:
            messages.success(request, f"{group.name} was {verb.lower()}d through Platform recovery.")
            return redirect("recovery-group-detail", pk=group.pk)
    return _recovery_confirmation_page(request, form=form, tier=2, title=f"{verb} {group.name}?", description="The Group URL and all memberships, Challenges, submissions, and history remain stored.", cancel_url=reverse("recovery-group-detail", kwargs={"pk": group.pk}), action_label=verb)


@login_required(login_url="config-login")
def recovery_group_transfer_owner(request, pk):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    group = get_object_or_404(ReadingGroup, pk=pk)
    form = OwnershipTransferForm(request.POST or None, group=group, expected_confirmation=group.name)
    if request.method == "POST" and form.is_valid():
        target = form.cleaned_data["target_membership"]
        operation_request = _recovery_request_from_form(
            request, form, tier=2, action="group.transfer_ownership", target_type="ReadingGroup",
            target_id=group.pk, target_label=group.name, group=group,
            before_state={"owner_membership_ids": list(group.memberships.filter(is_active=True, role=Membership.Role.OWNER).values_list("pk", flat=True))},
        )
        try:
            transfer_group_ownership(group=group, target_membership=target, recovery_request=operation_request)
        except ValidationError as error:
            _add_recovery_error(form, error)
        else:
            messages.success(request, f"Group ownership was transferred to {target.display_name}.")
            return redirect("recovery-group-detail", pk=group.pk)
    return _recovery_confirmation_page(request, form=form, tier=2, title=f"Transfer ownership of {group.name}?", description="The selected active membership becomes the sole active Group Owner. Other current owners become Members.", cancel_url=reverse("recovery-group-detail", kwargs={"pk": group.pk}), action_label="Transfer Ownership")


@login_required(login_url="config-login")
def recovery_account_list(request):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    return render(request, "core/recovery_account_list.html", {"accounts": get_user_model().objects.order_by("username")})


@sensitive_post_parameters("current_password")
@login_required(login_url="config-login")
def recovery_account_status(request, pk):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    account_user = get_object_or_404(get_user_model(), pk=pk)
    active = not account_user.is_active
    tier = 3 if account_user.is_superuser else 2
    verb = "Reactivate" if active else "Deactivate"
    form = RecoveryConfirmationForm(request.POST or None, expected_confirmation=account_user.username, require_password=tier == 3)
    if request.method == "POST" and form.is_valid():
        operation_request = _recovery_request_from_form(
            request, form, tier=tier, action=f"account.{'reactivate' if active else 'deactivate'}",
            target_type="User", target_id=account_user.pk, target_label=account_user.username,
            before_state={"is_active": account_user.is_active, "is_platform_owner": account_user.is_superuser},
        )
        try:
            set_user_active(user=account_user, active=active, recovery_request=operation_request)
        except ValidationError as error:
            _add_recovery_error(form, error)
        else:
            messages.success(request, f"{account_user.username} was {verb.lower()}d through Platform recovery.")
            return redirect("recovery-account-list")
    return _recovery_confirmation_page(request, form=form, tier=tier, title=f"{verb} {account_user.username}?", description="The stable account identity and all historical relationships remain stored. Deactivation invalidates existing sessions.", cancel_url=reverse("recovery-account-list"), action_label=verb)


@login_required(login_url="config-login")
def recovery_membership_status(request, pk):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    membership = get_object_or_404(Membership.objects.select_related("group", "user"), pk=pk)
    active = not membership.is_active
    verb = "Reactivate" if active else "Deactivate"
    form = RecoveryConfirmationForm(request.POST or None, expected_confirmation=membership.display_name)
    if request.method == "POST" and form.is_valid():
        operation_request = _recovery_request_from_form(request, form, tier=2, action=f"membership.{'reactivate' if active else 'deactivate'}", target_type="Membership", target_id=membership.pk, target_label=membership.display_name, group=membership.group, before_state={"is_active": membership.is_active, "role": membership.role})
        try:
            set_membership_active(membership=membership, active=active, recovery_request=operation_request)
        except ValidationError as error:
            _add_recovery_error(form, error)
        else:
            messages.success(request, f"{membership.display_name}'s Membership was {verb.lower()}d.")
            return redirect("recovery-group-detail", pk=membership.group_id)
    return _recovery_confirmation_page(request, form=form, tier=2, title=f"{verb} Membership for {membership.display_name}?", description="Historical Group and Challenge relationships remain stored. Active staffing and participation must be ended first.", cancel_url=reverse("recovery-group-detail", kwargs={"pk": membership.group_id}), action_label=verb)


@login_required(login_url="config-login")
def recovery_membership_role(request, pk):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    membership = get_object_or_404(Membership.objects.select_related("group"), pk=pk)
    form = MembershipRoleRecoveryForm(request.POST or None, expected_confirmation=membership.display_name)
    if request.method == "POST" and form.is_valid():
        operation_request = _recovery_request_from_form(request, form, tier=2, action="membership.correct_role", target_type="Membership", target_id=membership.pk, target_label=membership.display_name, group=membership.group, before_state={"role": membership.role})
        try:
            correct_membership_role(membership=membership, role=form.cleaned_data["role"], recovery_request=operation_request)
        except ValidationError as error:
            _add_recovery_error(form, error)
        else:
            messages.success(request, f"{membership.display_name}'s Group role was corrected.")
            return redirect("recovery-group-detail", pk=membership.group_id)
    return _recovery_confirmation_page(request, form=form, tier=2, title=f"Correct Group role for {membership.display_name}?", description="Final active Group Owner protections remain enforced.", cancel_url=reverse("recovery-group-detail", kwargs={"pk": membership.group_id}), action_label="Correct Role")


@login_required(login_url="config-login")
def recovery_staffing_status(request, pk):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    assignment = get_object_or_404(ChallengeStaffAssignment.objects.select_related("month__group", "membership", "team"), pk=pk)
    active = assignment.ended_at is not None
    verb = "Restore" if active else "End"
    label = str(assignment)
    form = RecoveryConfirmationForm(request.POST or None, expected_confirmation=label)
    if request.method == "POST" and form.is_valid():
        operation_request = _recovery_request_from_form(request, form, tier=2, action=f"staffing.{'restore' if active else 'end'}", target_type="ChallengeStaffAssignment", target_id=assignment.pk, target_label=label, group=assignment.month.group, challenge=assignment.month, before_state={"is_active": assignment.ended_at is None, "role": assignment.role, "team_id": assignment.team_id})
        try:
            set_staffing_active(assignment=assignment, active=active, recovery_request=operation_request)
        except ValidationError as error:
            _add_recovery_error(form, error)
        else:
            messages.success(request, f"Staffing assignment was {verb.lower()}ed.")
            return redirect("recovery-challenge-detail", pk=assignment.month_id)
    return _recovery_confirmation_page(request, form=form, tier=2, title=f"{verb} {label}?", description="The historical staffing row remains stored. Restoration is allowed only when current role, participation, and Team constraints remain coherent.", cancel_url=reverse("recovery-challenge-detail", kwargs={"pk": assignment.month_id}), action_label=verb)


@login_required(login_url="config-login")
def recovery_staffing_role(request, pk):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    assignment = get_object_or_404(ChallengeStaffAssignment.objects.select_related("month__group", "membership", "team"), pk=pk)
    label = str(assignment)
    form = StaffingRoleRecoveryForm(request.POST or None, month=assignment.month, expected_confirmation=label)
    if request.method == "POST" and form.is_valid():
        operation_request = _recovery_request_from_form(request, form, tier=2, action="staffing.correct_role", target_type="ChallengeStaffAssignment", target_id=assignment.pk, target_label=label, group=assignment.month.group, challenge=assignment.month, before_state={"role": assignment.role, "team_id": assignment.team_id})
        try:
            correct_staffing_role(assignment=assignment, role=form.cleaned_data["role"], team=form.cleaned_data["team"], recovery_request=operation_request)
        except ValidationError as error:
            _add_recovery_error(form, error)
        else:
            messages.success(request, "The historical staffing assignment was ended and a corrected assignment was created.")
            return redirect("recovery-challenge-detail", pk=assignment.month_id)
    return _recovery_confirmation_page(request, form=form, tier=2, title=f"Correct staffing role for {assignment.membership.display_name}?", description="Recovery ends the current historical row and creates a coherent replacement. It never rewrites the original role.", cancel_url=reverse("recovery-challenge-detail", kwargs={"pk": assignment.month_id}), action_label="Correct Staffing Role")


@login_required(login_url="config-login")
def recovery_enrollment_status(request, pk):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    enrollment = get_object_or_404(MonthEnrollment.objects.select_related("month__group", "participant"), pk=pk)
    active = not enrollment.is_active
    verb = "Reactivate" if active else "Withdraw"
    label = f"{enrollment.participant.display_name} — {enrollment.month.name}"
    form = RecoveryConfirmationForm(request.POST or None, expected_confirmation=label)
    if request.method == "POST" and form.is_valid():
        operation_request = _recovery_request_from_form(request, form, tier=2, action=f"enrollment.{'reactivate' if active else 'withdraw'}", target_type="MonthEnrollment", target_id=enrollment.pk, target_label=label, group=enrollment.month.group, challenge=enrollment.month, before_state={"is_active": enrollment.is_active, "origin": enrollment.origin, "inactive_reason": enrollment.inactive_reason})
        try:
            set_enrollment_active(enrollment=enrollment, active=active, recovery_request=operation_request)
        except ValidationError as error:
            _add_recovery_error(form, error)
        else:
            messages.success(request, f"Enrollment was {'reactivated' if active else 'withdrawn'} through recovery.")
            return redirect("recovery-challenge-detail", pk=enrollment.month_id)
    return _recovery_confirmation_page(request, form=form, tier=2, title=f"{verb} {label}?", description="Signup answers, Personal TBR state, submissions, and Team history remain stored. Reactivation refuses to fabricate missing required registration answers.", cancel_url=reverse("recovery-challenge-detail", kwargs={"pk": enrollment.month_id}), action_label=verb)


@login_required(login_url="config-login")
def recovery_enrollment_origin(request, pk):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    enrollment = get_object_or_404(MonthEnrollment.objects.select_related("month__group", "participant"), pk=pk)
    label = f"{enrollment.participant.display_name} — {enrollment.month.name}"
    form = EnrollmentOriginRecoveryForm(request.POST or None, expected_confirmation=label)
    if request.method == "POST" and form.is_valid():
        operation_request = _recovery_request_from_form(request, form, tier=2, action="enrollment.correct_origin", target_type="MonthEnrollment", target_id=enrollment.pk, target_label=label, group=enrollment.month.group, challenge=enrollment.month, before_state={"origin": enrollment.origin, "is_active": enrollment.is_active})
        try:
            correct_enrollment_origin(enrollment=enrollment, origin=form.cleaned_data["origin"], recovery_request=operation_request)
        except ValidationError as error:
            _add_recovery_error(form, error)
        else:
            messages.success(request, "Enrollment origin was corrected without changing participation or registration data.")
            return redirect("recovery-challenge-detail", pk=enrollment.month_id)
    return _recovery_confirmation_page(request, form=form, tier=2, title=f"Correct enrollment origin for {label}?", description="This explicitly corrects only the stored registration origin. It does not create registration answers, participation, Team assignment, or submissions.", cancel_url=reverse("recovery-challenge-detail", kwargs={"pk": enrollment.month_id}), action_label="Correct Origin")


@login_required(login_url="config-login")
def recovery_team_status(request, pk):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    team = get_object_or_404(Team.objects.select_related("month__group"), pk=pk)
    archived = not team.is_archived
    verb = "Archive" if archived else "Reactivate"
    form = RecoveryConfirmationForm(request.POST or None, expected_confirmation=team.name)
    if request.method == "POST" and form.is_valid():
        operation_request = _recovery_request_from_form(request, form, tier=2, action=f"team.{'archive' if archived else 'reactivate'}", target_type="Team", target_id=team.pk, target_label=team.name, group=team.month.group, challenge=team.month, before_state={"is_archived": team.is_archived})
        try:
            set_team_archived(team=team, archived=archived, recovery_request=operation_request)
        except ValidationError as error:
            _add_recovery_error(form, error)
        else:
            messages.success(request, f"{team.name} was {verb.lower()}d through recovery.")
            return redirect("recovery-challenge-detail", pk=team.month_id)
    return _recovery_confirmation_page(request, form=form, tier=2, title=f"{verb} {team.name}?", description="Team assignment and staffing history remain stored.", cancel_url=reverse("recovery-challenge-detail", kwargs={"pk": team.month_id}), action_label=verb)


@login_required(login_url="config-login")
def recovery_team_delete(request, pk):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    team = get_object_or_404(Team.objects.select_related("month__group"), pk=pk)
    form = SafeDeleteConfirmationForm(request.POST or None, expected_confirmation=team.name)
    if request.method == "POST" and form.is_valid():
        operation_request = _recovery_request_from_form(request, form, tier=1, action="team.delete_unused", target_type="Team", target_id=team.pk, target_label=team.name, group=team.month.group, challenge=team.month, before_state={"name": team.name, "is_archived": team.is_archived})
        try:
            delete_unused_team(team=team, recovery_request=operation_request)
        except ValidationError as error:
            _add_recovery_error(form, error)
        else:
            messages.success(request, f"Unused Team {team.name} was deleted.")
            return redirect("recovery-challenge-detail", pk=team.month_id)
    return _recovery_confirmation_page(request, form=form, tier=1, title=f"Delete unused Team {team.name}?", description="Only a never-used Team in a Draft Challenge can be deleted. Any assignment, staffing, or reward history blocks deletion.", cancel_url=reverse("recovery-challenge-detail", kwargs={"pk": team.month_id}), action_label="Delete Unused Team")


@login_required(login_url="config-login")
def recovery_team_assignment_status(request, pk):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    assignment = get_object_or_404(TeamAssignment.objects.select_related("month__group", "participant", "team"), pk=pk)
    active = assignment.ended_at is not None
    verb = "Restore" if active else "End"
    label = f"{assignment.participant.display_name} → {assignment.team.name}"
    form = RecoveryConfirmationForm(request.POST or None, expected_confirmation=label)
    if request.method == "POST" and form.is_valid():
        operation_request = _recovery_request_from_form(request, form, tier=2, action=f"team_assignment.{'restore' if active else 'end'}", target_type="TeamAssignment", target_id=assignment.pk, target_label=label, group=assignment.month.group, challenge=assignment.month, before_state={"is_active": assignment.ended_at is None, "team_id": assignment.team_id})
        try:
            set_team_assignment_active(assignment=assignment, active=active, recovery_request=operation_request)
        except ValidationError as error:
            _add_recovery_error(form, error)
        else:
            messages.success(request, f"Team assignment was {verb.lower()}ed.")
            return redirect("recovery-challenge-detail", pk=assignment.month_id)
    return _recovery_confirmation_page(request, form=form, tier=2, title=f"{verb} Team assignment {label}?", description="Ending preserves the historical row and consistently ends any Team Leader assignment tied to it. Restoration requires coherent active participation and no competing current Team.", cancel_url=reverse("recovery-challenge-detail", kwargs={"pk": assignment.month_id}), action_label=verb)


@login_required(login_url="config-login")
def recovery_team_assignment_reassign(request, pk):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    assignment = get_object_or_404(TeamAssignment.objects.select_related("month__group", "participant", "team"), pk=pk)
    label = f"{assignment.participant.display_name} → {assignment.team.name}"
    form = TeamReassignmentRecoveryForm(request.POST or None, month=assignment.month, current_team=assignment.team, expected_confirmation=label)
    if request.method == "POST" and form.is_valid():
        operation_request = _recovery_request_from_form(request, form, tier=2, action="team_assignment.reassign", target_type="TeamAssignment", target_id=assignment.pk, target_label=label, group=assignment.month.group, challenge=assignment.month, before_state={"team_id": assignment.team_id, "is_active": assignment.ended_at is None})
        try:
            reassign_team_assignment(assignment=assignment, team=form.cleaned_data["team"], recovery_request=operation_request)
        except ValidationError as error:
            _add_recovery_error(form, error)
        else:
            messages.success(request, "The original Team assignment was ended and a new assignment was created atomically.")
            return redirect("recovery-challenge-detail", pk=assignment.month_id)
    return _recovery_confirmation_page(request, form=form, tier=2, title=f"Reassign {assignment.participant.display_name}?", description="The current historical row will be ended and a new Team assignment created. Team Leader history tied to the old Team is ended consistently.", cancel_url=reverse("recovery-challenge-detail", kwargs={"pk": assignment.month_id}), action_label="Reassign Reader")


@login_required(login_url="config-login")
def recovery_submission_list(request):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    submissions = BookSubmission.objects.select_related(
        "month__group", "participant__user",
    ).order_by("month__group__name", "-month__starts_on", "participant__display_name", "title")
    return render(request, "core/recovery_submission_list.html", {"submissions": submissions})


@login_required(login_url="config-login")
def recovery_submission_detail(request, pk):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    submission = get_object_or_404(
        BookSubmission.objects.select_related(
            "month__group", "participant__user", "catalog_book", "catalog_edition",
        ), pk=pk,
    )
    score = challenge_score_totals(
        month=submission.month, participant_ids=[submission.participant_id],
    ).get(submission.participant_id, {"base_pages": 0, "modifier_pages": 0, "total_pages": 0})
    return render(request, "core/recovery_submission_detail.html", {
        "submission": submission, "score": score,
        "impact": submission_purge_impact(submission),
        "claims": submission.theme_claims.select_related("theme").order_by("theme__starts_on", "theme__name"),
        "provenance": submission.modifier_provenance.order_by("effective_date", "pk"),
        "botm_matches": submission.botm_matches.select_related("botm_book").order_by("pk"),
        "tbr_matches": submission.personal_tbr_matches.select_related("personal_tbr_book").order_by("pk"),
    })


@login_required(login_url="config-login")
def recovery_submission_status(request, pk):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    submission = get_object_or_404(
        BookSubmission.objects.select_related("month__group", "participant"), pk=pk,
    )
    restore = submission.is_removed
    label = submission_recovery_label(submission)
    form = RecoveryConfirmationForm(request.POST or None, expected_confirmation=label)
    if request.method == "POST" and form.is_valid():
        operation_request = _recovery_request_from_form(
            request, form, tier=2,
            action="submission.restore" if restore else "submission.soft_remove",
            target_type="BookSubmission", target_id=submission.pk, target_label=label,
            group=submission.month.group, challenge=submission.month,
            before_state={"is_removed": submission.is_removed, "status": submission.status},
        )
        try:
            set_submission_removed(
                submission=submission, removed=not restore, recovery_request=operation_request,
            )
        except ValidationError as error:
            _add_recovery_error(form, error)
        else:
            messages.success(request, f"Submission was {'restored' if restore else 'soft removed'} through Platform recovery.")
            return redirect("recovery-submission-detail", pk=submission.pk)
    verb = "Restore" if restore else "Soft Remove"
    return _recovery_confirmation_page(
        request, form=form, tier=2, title=f"{verb} {submission.title}?",
        description="Northbound will reconcile Theme, BOTM, Personal TBR, completion, and cached submission score state through canonical services.",
        cancel_url=reverse("recovery-submission-detail", kwargs={"pk": submission.pk}),
        action_label=verb,
    )


@sensitive_post_parameters("current_password")
@login_required(login_url="config-login")
def recovery_submission_purge(request, pk):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    submission = get_object_or_404(
        BookSubmission.objects.select_related("month__group", "participant"), pk=pk,
    )
    label = submission_recovery_label(submission)
    impact = submission_purge_impact(submission)
    form = RecoveryConfirmationForm(
        request.POST or None, expected_confirmation=label, require_password=True,
    )
    if request.method == "POST" and form.is_valid():
        operation_request = _recovery_request_from_form(
            request, form, tier=3, action="submission.purge",
            target_type="BookSubmission", target_id=submission.pk, target_label=label,
            group=submission.month.group, challenge=submission.month, impact=impact,
            before_state={
                "title": submission.title, "status": submission.status,
                "is_removed": submission.is_removed,
                "participant_id": submission.participant_id,
            },
        )
        try:
            purge_submission(submission=submission, recovery_request=operation_request)
        except ValidationError as error:
            _add_recovery_error(form, error)
        else:
            messages.success(request, f"Submission {submission.title} was permanently purged through Platform recovery.")
            return redirect("recovery-submission-list")
    return _recovery_confirmation_page(
        request, form=form, tier=3, impact=impact,
        title=f"Permanently purge {submission.title}?",
        description="This removes the submission and directly linked claims, matches, and modifier records after reconciling Reader completion and score state.",
        cancel_url=reverse("recovery-submission-detail", kwargs={"pk": submission.pk}),
        action_label="Permanently Purge Submission",
    )


@login_required(login_url="config-login")
def recovery_theme_list(request):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    themes = MonthTheme.objects.select_related("month__group").annotate(
        recovery_claim_count=Count("claims"),
    ).order_by("month__group__name", "-month__starts_on", "starts_on", "name")
    return render(request, "core/recovery_theme_list.html", {"themes": themes})


@login_required(login_url="config-login")
def recovery_theme_detail(request, pk):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    theme = get_object_or_404(MonthTheme.objects.select_related("month__group"), pk=pk)
    return render(request, "core/recovery_theme_detail.html", {
        "theme": theme,
        "claims": theme.claims.select_related("submission__participant", "submission__month").order_by("submission__title"),
    })


@login_required(login_url="config-login")
def recovery_theme_status(request, pk):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    theme = get_object_or_404(MonthTheme.objects.select_related("month__group"), pk=pk)
    active = not theme.is_active
    label = f"Theme #{theme.pk}: {theme.name}"
    form = RecoveryConfirmationForm(request.POST or None, expected_confirmation=label)
    if request.method == "POST" and form.is_valid():
        operation_request = _recovery_request_from_form(
            request, form, tier=2, action="theme.reactivate" if active else "theme.retire",
            target_type="MonthTheme", target_id=theme.pk, target_label=label,
            group=theme.month.group, challenge=theme.month,
            before_state={"is_active": theme.is_active, "claim_count": theme.claims.count()},
        )
        try:
            set_theme_active(theme=theme, active=active, recovery_request=operation_request)
        except ValidationError as error:
            _add_recovery_error(form, error)
        else:
            messages.success(request, f"Theme was {'reactivated' if active else 'retired'}.")
            return redirect("recovery-theme-detail", pk=theme.pk)
    verb = "Reactivate" if active else "Retire"
    return _recovery_confirmation_page(
        request, form=form, tier=2, title=f"{verb} {theme.name}?",
        description="Existing claims and historically frozen rewards remain unchanged.",
        cancel_url=reverse("recovery-theme-detail", kwargs={"pk": theme.pk}), action_label=verb,
    )


@login_required(login_url="config-login")
def recovery_theme_correct(request, pk):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    theme = get_object_or_404(MonthTheme.objects.select_related("month__group"), pk=pk)
    label = f"Theme #{theme.pk}: {theme.name}"
    form = UnusedThemeRecoveryForm(
        request.POST or None, theme=theme, expected_confirmation=label,
    )
    if request.method == "POST" and form.is_valid():
        values = {
            field: form.cleaned_data[field]
            for field in (
                "name", "description", "starts_on", "ends_on", "bonus_pages",
                "allow_stacking", "prompt", "is_visible",
            )
        }
        operation_request = _recovery_request_from_form(
            request, form, tier=2, action="theme.correct_unused",
            target_type="MonthTheme", target_id=theme.pk, target_label=label,
            group=theme.month.group, challenge=theme.month,
            before_state={"claim_count": theme.claims.count()},
        )
        try:
            correct_unused_theme(theme=theme, values=values, recovery_request=operation_request)
        except ValidationError as error:
            _add_recovery_error(form, error)
        else:
            messages.success(request, "Unused Theme configuration was corrected.")
            return redirect("recovery-theme-detail", pk=theme.pk)
    return render(request, "core/recovery_theme_correct.html", {
        "form": form, "theme": theme,
        "cancel_url": reverse("recovery-theme-detail", kwargs={"pk": theme.pk}),
    })


@login_required(login_url="config-login")
def recovery_theme_claim(request, pk):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    claim = get_object_or_404(
        ThemeClaim.objects.select_related(
            "submission__month__group", "submission__participant", "theme",
        ), pk=pk,
    )
    label = f"Theme claim #{claim.pk}: {claim.theme.name} — {claim.submission.title}"
    form = ThemeClaimRecoveryForm(
        request.POST or None, expected_confirmation=label,
        initial={"status": claim.status},
    )
    if request.method == "POST" and form.is_valid():
        operation_request = _recovery_request_from_form(
            request, form, tier=2, action="theme_claim.reconcile",
            target_type="ThemeClaim", target_id=claim.pk, target_label=label,
            group=claim.submission.month.group, challenge=claim.submission.month,
            before_state={
                "status": claim.status,
                "approved_bonus_pages": claim.approved_bonus_pages,
            },
        )
        try:
            recover_theme_claim(
                claim=claim, status=form.cleaned_data["status"],
                force_rebuild=form.cleaned_data["rebuild_provenance"],
                recovery_request=operation_request,
            )
        except ValidationError as error:
            _add_recovery_error(form, error)
        else:
            messages.success(request, "Theme claim and canonical provenance were reconciled.")
            return redirect("recovery-submission-detail", pk=claim.submission_id)
    return render(request, "core/recovery_theme_claim.html", {
        "claim": claim, "form": form,
        "cancel_url": reverse("recovery-submission-detail", kwargs={"pk": claim.submission_id}),
    })


@login_required(login_url="config-login")
def recovery_provenance_list(request):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    provenance = ModifierProvenance.objects.select_related(
        "month__group", "participant", "submission",
    ).order_by("month__group__name", "-month__starts_on", "participant__display_name", "effective_date", "pk")
    return render(request, "core/recovery_provenance_list.html", {"provenance": provenance})


@login_required(login_url="config-login")
def recovery_provenance_detail(request, pk):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    provenance = get_object_or_404(
        ModifierProvenance.objects.select_related(
            "month__group", "participant", "submission", "applied_by", "voided_by",
        ), pk=pk,
    )
    return render(request, "core/recovery_provenance_detail.html", {"provenance": provenance})


def _provenance_recovery_action(request, provenance, *, action):
    label = f"Modifier #{provenance.pk}: {provenance.source_label}"
    tier = 3 if action == "purge" else 2
    form = RecoveryConfirmationForm(
        request.POST or None, expected_confirmation=label, require_password=tier == 3,
    )
    if request.method == "POST" and form.is_valid():
        operation_request = _recovery_request_from_form(
            request, form, tier=tier, action=f"provenance.{action}",
            target_type="ModifierProvenance", target_id=provenance.pk, target_label=label,
            group=provenance.month.group, challenge=provenance.month,
            before_state={
                "source_type": provenance.source_type,
                "source_reference": provenance.source_reference,
                "amount": provenance.amount,
                "is_active": provenance.is_active,
            },
        )
        try:
            if action == "void":
                void_provenance(provenance=provenance, recovery_request=operation_request)
            elif action == "rebuild":
                rebuild_provenance(provenance=provenance, recovery_request=operation_request)
            else:
                purge_malformed_provenance(provenance=provenance, recovery_request=operation_request)
        except ValidationError as error:
            _add_recovery_error(form, error)
        else:
            messages.success(request, f"Modifier provenance {action} completed through Platform recovery.")
            if action == "purge":
                return redirect("recovery-provenance-list")
            return redirect("recovery-provenance-detail", pk=provenance.pk)
    descriptions = {
        "void": "The frozen amount is preserved, but the modifier is made inactive only when source truth does not still require it.",
        "rebuild": "The modifier is recreated or restored only from a supported canonical source. No amount can be entered manually.",
        "purge": "Only malformed orphaned Theme provenance can be permanently removed in this slice.",
    }
    return _recovery_confirmation_page(
        request, form=form, tier=tier,
        title=f"{action.title()} {provenance.source_label}?",
        description=descriptions[action],
        cancel_url=reverse("recovery-provenance-detail", kwargs={"pk": provenance.pk}),
        action_label=action.title(),
    )


@login_required(login_url="config-login")
def recovery_provenance_void(request, pk):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    provenance = get_object_or_404(ModifierProvenance.objects.select_related("month__group"), pk=pk)
    return _provenance_recovery_action(request, provenance, action="void")


@login_required(login_url="config-login")
def recovery_provenance_rebuild(request, pk):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    provenance = get_object_or_404(ModifierProvenance.objects.select_related("month__group"), pk=pk)
    return _provenance_recovery_action(request, provenance, action="rebuild")


@sensitive_post_parameters("current_password")
@login_required(login_url="config-login")
def recovery_provenance_purge(request, pk):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    provenance = get_object_or_404(ModifierProvenance.objects.select_related("month__group"), pk=pk)
    return _provenance_recovery_action(request, provenance, action="purge")


def _recovery_book_values(cleaned_data):
    return {
        field: cleaned_data.get(field)
        for field in (
            "catalog_book", "catalog_edition", "title_snapshot", "author_snapshot",
            "page_count_snapshot", "cover_url_snapshot", "source_url_snapshot",
        )
    }


@login_required(login_url="config-login")
def recovery_botm_list(request):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    books = BotmBook.objects.select_related("month__group", "catalog_book").annotate(
        recovery_match_count=Count("matches"),
    ).order_by("month__group__name", "-month__starts_on", "position", "pk")
    matches = BotmMatch.objects.select_related(
        "month__group", "participant", "submission", "botm_book",
    ).order_by("month__group__name", "-month__starts_on", "participant__display_name", "pk")
    return render(request, "core/recovery_botm_list.html", {"books": books, "matches": matches})


@login_required(login_url="config-login")
def recovery_botm_book_detail(request, pk):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    book = get_object_or_404(BotmBook.objects.select_related("month__group", "catalog_book", "catalog_edition"), pk=pk)
    return render(request, "core/recovery_botm_book_detail.html", {
        "book": book,
        "matches": book.matches.select_related("participant", "submission").order_by("participant__display_name", "pk"),
    })


@login_required(login_url="config-login")
def recovery_botm_book_status(request, pk):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    book = get_object_or_404(BotmBook.objects.select_related("month__group"), pk=pk)
    retired = not book.is_retired
    label = f"BOTM book #{book.pk}: {book.title_snapshot}"
    form = RecoveryConfirmationForm(request.POST or None, expected_confirmation=label)
    if request.method == "POST" and form.is_valid():
        recovery_request = _recovery_request_from_form(
            request, form, tier=2, action="botm_book.retire" if retired else "botm_book.reactivate",
            target_type="BotmBook", target_id=book.pk, target_label=label,
            group=book.month.group, challenge=book.month,
            before_state={"is_retired": book.is_retired, "match_count": book.matches.count()},
        )
        try:
            set_botm_book_retired(book=book, retired=retired, recovery_request=recovery_request)
        except ValidationError as error:
            _add_recovery_error(form, error)
        else:
            messages.success(request, f"BOTM book was {'retired' if retired else 'reactivated'} and matching was reconciled.")
            return redirect("recovery-botm-book-detail", pk=book.pk)
    verb = "Retire" if retired else "Reactivate"
    return _recovery_confirmation_page(
        request, form=form, tier=2, title=f"{verb} {book.title_snapshot}?",
        description="Existing match and completion history remains durable while current qualification is synchronized.",
        cancel_url=reverse("recovery-botm-book-detail", kwargs={"pk": book.pk}), action_label=verb,
    )


@sensitive_post_parameters("current_password")
@login_required(login_url="config-login")
def recovery_botm_book_correct(request, pk):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    book = get_object_or_404(BotmBook.objects.select_related("month__group", "catalog_book", "catalog_edition"), pk=pk)
    used = book.matches.exists()
    tier = 3 if used else 2
    label = f"BOTM book #{book.pk}: {book.title_snapshot}"
    form = RecoveryBookIdentityForm(
        request.POST or None, expected_confirmation=label, require_password=used,
        initial={
            "catalog_book": book.catalog_book, "catalog_edition": book.catalog_edition,
            "title_snapshot": book.title_snapshot, "author_snapshot": book.author_snapshot,
            "page_count_snapshot": book.page_count_snapshot,
            "cover_url_snapshot": book.cover_url_snapshot, "source_url_snapshot": book.source_url_snapshot,
        },
    )
    form.fields["page_count_snapshot"].required = True
    if request.method == "POST" and form.is_valid():
        recovery_request = _recovery_request_from_form(
            request, form, tier=tier, action="botm_book.repair_identity",
            target_type="BotmBook", target_id=book.pk, target_label=label,
            group=book.month.group, challenge=book.month,
            before_state={"title": book.title_snapshot, "author": book.author_snapshot, "catalog_book_id": book.catalog_book_id, "match_count": book.matches.count()},
        )
        try:
            correct_botm_book(
                book=book, values=_recovery_book_values(form.cleaned_data),
                allow_used=used, recovery_request=recovery_request,
            )
        except ValidationError as error:
            _add_recovery_error(form, error)
        else:
            messages.success(request, "BOTM identity was repaired and match/reward state was reconciled.")
            return redirect("recovery-botm-book-detail", pk=book.pk)
    return render(request, "core/recovery_book_identity.html", {
        "form": form, "tier": tier, "kind": "BOTM Book", "object": book,
        "used": used, "cancel_url": reverse("recovery-botm-book-detail", kwargs={"pk": book.pk}),
        "backup_advisory": stored_backup_advisory() if tier == 3 else None,
        "platform_timezone": get_platform_settings().timezone,
    })


@login_required(login_url="config-login")
def recovery_botm_match(request, pk):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    match = get_object_or_404(BotmMatch.objects.select_related("month__group", "participant", "submission", "botm_book"), pk=pk)
    label = botm_match_label(match)
    form = BotmMatchRecoveryForm(
        request.POST or None, month=match.month, expected_confirmation=label,
        initial={"target_book": match.botm_book, "decision": "pending"},
    )
    if request.method == "POST" and form.is_valid():
        recovery_request = _recovery_request_from_form(
            request, form, tier=2, action="botm_match.recover",
            target_type="BotmMatch", target_id=match.pk, target_label=label,
            group=match.month.group, challenge=match.month,
            before_state={"status": match.status, "is_qualifying": match.is_qualifying, "botm_book_id": match.botm_book_id},
        )
        try:
            recover_botm_match(
                match=match, decision=form.cleaned_data["decision"],
                target_book=form.cleaned_data["target_book"], recovery_request=recovery_request,
            )
        except ValidationError as error:
            _add_recovery_error(form, error)
        else:
            messages.success(request, "BOTM match and dependent reward/completion state were reconciled.")
            return redirect("recovery-botm-list")
    return render(request, "core/recovery_match.html", {
        "form": form, "kind": "BOTM", "match": match,
        "book_title": match.botm_book.title_snapshot,
        "cancel_url": reverse("recovery-botm-list"),
        "purge_url": reverse("recovery-botm-match-purge", kwargs={"pk": match.pk}),
    })


@sensitive_post_parameters("current_password")
@login_required(login_url="config-login")
def recovery_botm_match_purge(request, pk):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    match = get_object_or_404(BotmMatch.objects.select_related("month__group", "participant", "submission", "botm_book"), pk=pk)
    label = botm_match_label(match)
    form = RecoveryConfirmationForm(request.POST or None, expected_confirmation=label, require_password=True)
    if request.method == "POST" and form.is_valid():
        recovery_request = _recovery_request_from_form(
            request, form, tier=3, action="botm_match.purge", target_type="BotmMatch",
            target_id=match.pk, target_label=label, group=match.month.group, challenge=match.month,
            before_state={"status": match.status, "is_qualifying": match.is_qualifying},
        )
        try:
            purge_botm_match(match=match, recovery_request=recovery_request)
        except ValidationError as error:
            _add_recovery_error(form, error)
        else:
            messages.success(request, "Malformed BOTM match was purged after source reconciliation.")
            return redirect("recovery-botm-list")
    return _recovery_confirmation_page(
        request, form=form, tier=3, title=f"Purge malformed {label}?",
        description="This permanently removes only the malformed match aggregate and its per-book provenance after completion reconciliation.",
        cancel_url=reverse("recovery-botm-list"), action_label="Purge Match",
    )


@login_required(login_url="config-login")
def recovery_tbr_list(request):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    tbrs = PersonalTBR.objects.select_related(
        "enrollment__month__group", "enrollment__participant__user",
    ).filter(confirmed_at__isnull=False).annotate(
        recovery_book_count=Count("books"),
    ).order_by("enrollment__month__group__name", "-enrollment__month__starts_on", "enrollment__participant__display_name")
    return render(request, "core/recovery_tbr_list.html", {"tbrs": tbrs})


@login_required(login_url="config-login")
def recovery_tbr_detail(request, pk):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    tbr = get_object_or_404(PersonalTBR.objects.select_related("enrollment__month__group", "enrollment__participant"), pk=pk, confirmed_at__isnull=False)
    matches = PersonalTBRMatch.objects.filter(personal_tbr_book__personal_tbr=tbr).select_related("personal_tbr_book", "submission").order_by("personal_tbr_book__position", "pk")
    award = PersonalTBRCompletionAward.objects.filter(personal_tbr=tbr).first()
    return render(request, "core/recovery_tbr_detail.html", {
        "tbr": tbr, "books": tbr.books.order_by("position", "pk"), "matches": matches,
        "award": award, "qualifying_match_count": matches.filter(is_qualifying=True).count(),
    })


@sensitive_post_parameters("current_password")
@login_required(login_url="config-login")
def recovery_tbr_entry(request, pk):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    book = get_object_or_404(PersonalTBRBook.objects.select_related("personal_tbr__enrollment__month__group", "personal_tbr__enrollment__participant", "catalog_book", "catalog_edition"), pk=pk, personal_tbr__confirmed_at__isnull=False)
    tbr = book.personal_tbr
    label = f"Locked TBR entry #{book.pk}: {tbr.enrollment.participant.display_name} — position {book.position}"
    form = RecoveryBookIdentityForm(
        request.POST or None, expected_confirmation=label, require_password=True,
        initial={
            "catalog_book": book.catalog_book, "catalog_edition": book.catalog_edition,
            "title_snapshot": book.title_snapshot, "author_snapshot": book.author_snapshot,
            "page_count_snapshot": book.page_count_snapshot,
            "cover_url_snapshot": book.cover_url_snapshot, "source_url_snapshot": book.source_url_snapshot,
        },
    )
    if request.method == "POST" and form.is_valid():
        recovery_request = _recovery_request_from_form(
            request, form, tier=3, action="personal_tbr.repair_entry",
            target_type="PersonalTBRBook", target_id=book.pk, target_label=label,
            group=tbr.enrollment.month.group, challenge=tbr.enrollment.month,
            before_state={"position": book.position, "title": book.title_snapshot, "author": book.author_snapshot, "match_count": book.matches.count()},
        )
        try:
            repair_tbr_entry(book=book, values=_recovery_book_values(form.cleaned_data), recovery_request=recovery_request)
        except ValidationError as error:
            _add_recovery_error(form, error)
        else:
            messages.success(request, "Locked TBR entry was repaired and all matching/reward state was reconciled.")
            return redirect("recovery-tbr-detail", pk=tbr.pk)
    return render(request, "core/recovery_book_identity.html", {
        "form": form, "tier": 3, "kind": "Locked Personal TBR Entry", "object": book,
        "used": True, "cancel_url": reverse("recovery-tbr-detail", kwargs={"pk": tbr.pk}),
        "backup_advisory": stored_backup_advisory(),
        "platform_timezone": get_platform_settings().timezone,
    })


@sensitive_post_parameters("current_password")
@login_required(login_url="config-login")
def recovery_tbr_rebuild(request, pk):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    tbr = get_object_or_404(PersonalTBR.objects.select_related("enrollment__month__group", "enrollment__participant"), pk=pk, confirmed_at__isnull=False)
    label = tbr_label(tbr)
    initial = [{
        "include": True, "position": book.position, "catalog_book": book.catalog_book,
        "catalog_edition": book.catalog_edition, "title_snapshot": book.title_snapshot,
        "author_snapshot": book.author_snapshot, "page_count_snapshot": book.page_count_snapshot,
        "cover_url_snapshot": book.cover_url_snapshot, "source_url_snapshot": book.source_url_snapshot,
    } for book in tbr.books.order_by("position", "pk")]
    confirmation_form = RecoveryConfirmationForm(request.POST or None, expected_confirmation=label, require_password=True, prefix="confirm")
    book_formset = LockedTbrListBookFormSet(request.POST or None, initial=initial, prefix="books")
    if request.method == "POST" and confirmation_form.is_valid() and book_formset.is_valid():
        books = [{
            "position": form.cleaned_data["position"],
            **_recovery_book_values(form.cleaned_data),
        } for form in book_formset if form.cleaned_data.get("include")]
        recovery_request = RecoveryRequest(
            actor=request.user, tier=3, action="personal_tbr.rebuild_locked_list",
            target_type="PersonalTBR", target_id=str(tbr.pk), target_label=label,
            reason=confirmation_form.cleaned_data["reason"], required_confirmation=label,
            supplied_confirmation=confirmation_form.cleaned_data["confirmation"],
            current_password=confirmation_form.cleaned_data["current_password"],
            confirmation_method="exact target label plus current password",
            group=tbr.enrollment.month.group, challenge=tbr.enrollment.month,
            before_state={"locked_count": tbr.books.count(), "qualifying_match_count": PersonalTBRMatch.objects.filter(personal_tbr_book__personal_tbr=tbr, is_qualifying=True).count()},
        )
        try:
            rebuild_locked_tbr(tbr=tbr, books=books, recovery_request=recovery_request)
        except ValidationError as error:
            _add_recovery_error(confirmation_form, error)
        else:
            messages.success(request, "The locked Personal TBR was atomically rebuilt and reconciled.")
            return redirect("recovery-tbr-detail", pk=tbr.pk)
    return render(request, "core/recovery_tbr_rebuild.html", {
        "tbr": tbr, "confirmation_form": confirmation_form, "book_formset": book_formset,
        "current_count": tbr.books.count(), "current_matched_count": PersonalTBRMatch.objects.filter(personal_tbr_book__personal_tbr=tbr, is_qualifying=True).count(),
        "current_completion": PersonalTBRCompletionAward.objects.filter(personal_tbr=tbr, is_qualifying=True).exists(),
        "cancel_url": reverse("recovery-tbr-detail", kwargs={"pk": tbr.pk}),
        "backup_advisory": stored_backup_advisory(),
        "platform_timezone": get_platform_settings().timezone,
    })


@login_required(login_url="config-login")
def recovery_tbr_match(request, pk):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    match = get_object_or_404(PersonalTBRMatch.objects.select_related("month__group", "participant", "submission", "personal_tbr_book__personal_tbr"), pk=pk)
    label = tbr_match_label(match)
    form = TbrMatchRecoveryForm(
        request.POST or None, personal_tbr=match.personal_tbr_book.personal_tbr,
        expected_confirmation=label,
        initial={"target_book": match.personal_tbr_book, "decision": "pending"},
    )
    if request.method == "POST" and form.is_valid():
        recovery_request = _recovery_request_from_form(
            request, form, tier=2, action="personal_tbr_match.recover",
            target_type="PersonalTBRMatch", target_id=match.pk, target_label=label,
            group=match.month.group, challenge=match.month,
            before_state={"status": match.status, "is_qualifying": match.is_qualifying, "personal_tbr_book_id": match.personal_tbr_book_id},
        )
        try:
            recover_tbr_match(match=match, decision=form.cleaned_data["decision"], target_book=form.cleaned_data["target_book"], recovery_request=recovery_request)
        except ValidationError as error:
            _add_recovery_error(form, error)
        else:
            messages.success(request, "Personal TBR match and dependent reward/completion state were reconciled.")
            return redirect("recovery-tbr-detail", pk=match.personal_tbr_book.personal_tbr_id)
    return render(request, "core/recovery_match.html", {
        "form": form, "kind": "Personal TBR", "match": match,
        "book_title": match.personal_tbr_book.title_snapshot,
        "cancel_url": reverse("recovery-tbr-detail", kwargs={"pk": match.personal_tbr_book.personal_tbr_id}),
        "purge_url": reverse("recovery-tbr-match-purge", kwargs={"pk": match.pk}),
    })


@sensitive_post_parameters("current_password")
@login_required(login_url="config-login")
def recovery_tbr_match_purge(request, pk):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    match = get_object_or_404(PersonalTBRMatch.objects.select_related("month__group", "participant", "personal_tbr_book__personal_tbr"), pk=pk)
    label = tbr_match_label(match)
    form = RecoveryConfirmationForm(request.POST or None, expected_confirmation=label, require_password=True)
    if request.method == "POST" and form.is_valid():
        recovery_request = _recovery_request_from_form(
            request, form, tier=3, action="personal_tbr_match.purge", target_type="PersonalTBRMatch",
            target_id=match.pk, target_label=label, group=match.month.group, challenge=match.month,
            before_state={"status": match.status, "is_qualifying": match.is_qualifying},
        )
        try:
            purge_tbr_match(match=match, recovery_request=recovery_request)
        except ValidationError as error:
            _add_recovery_error(form, error)
        else:
            messages.success(request, "Malformed Personal TBR match was purged after source reconciliation.")
            return redirect("recovery-tbr-detail", pk=match.personal_tbr_book.personal_tbr_id)
    return _recovery_confirmation_page(
        request, form=form, tier=3, title=f"Purge malformed {label}?",
        description="This permanently removes only the malformed match aggregate and its per-book provenance after completion reconciliation.",
        cancel_url=reverse("recovery-tbr-detail", kwargs={"pk": match.personal_tbr_book.personal_tbr_id}), action_label="Purge Match",
    )


@login_required(login_url="config-login")
def recovery_game_list(request):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    games = Game.objects.select_related("month__group").annotate(
        recovery_application_count=Count("reward_applications"),
    ).order_by("month__group__name", "-month__starts_on", "starts_at", "name")
    applications = GameRewardApplication.objects.select_related("game__month__group").annotate(
        recovery_recipient_count=Count("recipients"),
    ).order_by("game__month__group__name", "-game__month__starts_on", "applied_at", "pk")
    return render(request, "core/recovery_game_list.html", {"games": games, "applications": applications})


@login_required(login_url="config-login")
def recovery_game_detail(request, pk):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    game = get_object_or_404(Game.objects.select_related("month__group"), pk=pk)
    return render(request, "core/recovery_game_detail.html", {
        "game": game,
        "applications": game.reward_applications.prefetch_related("recipients__participant", "recipients__provenance").order_by("applied_at", "pk"),
    })


@login_required(login_url="config-login")
def recovery_game_status(request, pk):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    game = get_object_or_404(Game.objects.select_related("month__group"), pk=pk)
    active = not game.is_active
    label = f"Game #{game.pk}: {game.name}"
    form = RecoveryConfirmationForm(request.POST or None, expected_confirmation=label)
    if request.method == "POST" and form.is_valid():
        recovery_request = _recovery_request_from_form(
            request, form, tier=2, action="game.reactivate" if active else "game.retire",
            target_type="Game", target_id=game.pk, target_label=label,
            group=game.month.group, challenge=game.month,
            before_state={"is_active": game.is_active, "application_count": game.reward_applications.count()},
        )
        try:
            set_game_active(game=game, active=active, recovery_request=recovery_request)
        except ValidationError as error:
            _add_recovery_error(form, error)
        else:
            messages.success(request, f"Game was {'reactivated' if active else 'retired'} without changing frozen rewards.")
            return redirect("recovery-game-detail", pk=game.pk)
    verb = "Reactivate" if active else "Retire"
    return _recovery_confirmation_page(
        request, form=form, tier=2, title=f"{verb} {game.name}?",
        description="Frozen Game reward applications and recipients remain unchanged.",
        cancel_url=reverse("recovery-game-detail", kwargs={"pk": game.pk}), action_label=verb,
    )


@login_required(login_url="config-login")
def recovery_game_application(request, pk, mode):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    if mode not in {"void", "restore", "repair"}:
        raise Http404
    application = get_object_or_404(GameRewardApplication.objects.select_related("game__month__group"), pk=pk)
    label = game_application_label(application)
    form = RecoveryConfirmationForm(request.POST or None, expected_confirmation=label)
    if request.method == "POST" and form.is_valid():
        recovery_request = _recovery_request_from_form(
            request, form, tier=2, action=f"game_reward.{mode}",
            target_type="GameRewardApplication", target_id=application.pk, target_label=label,
            group=application.game.month.group, challenge=application.game.month,
            before_state={"is_voided": application.is_voided, "amount": application.amount, "recipient_count": application.recipients.count()},
        )
        try:
            recover_game_application(application=application, mode=mode, recovery_request=recovery_request)
        except ValidationError as error:
            _add_recovery_error(form, error)
        else:
            messages.success(request, f"Game reward {mode} completed from its frozen application and recipient truth.")
            return redirect("recovery-game-detail", pk=application.game_id)
    return _recovery_confirmation_page(
        request, form=form, tier=2, title=f"{mode.title()} {label}?",
        description=f"Frozen amount: +{application.amount}. Frozen recipients: {application.recipients.count()}. No current roster recalculation occurs.",
        cancel_url=reverse("recovery-game-detail", kwargs={"pk": application.game_id}), action_label=mode.title(),
    )


@login_required(login_url="config-login")
def recovery_game_recreate(request, pk):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    application = get_object_or_404(GameRewardApplication.objects.select_related("game__month__group"), pk=pk)
    label = game_application_label(application)
    form = GameReplacementRecoveryForm(request.POST or None, expected_confirmation=label)
    if request.method == "POST" and form.is_valid():
        recovery_request = _recovery_request_from_form(
            request, form, tier=2, action="game_reward.recreate",
            target_type="GameRewardApplication", target_id=application.pk, target_label=label,
            group=application.game.month.group, challenge=application.game.month,
            before_state={"is_voided": application.is_voided, "frozen_amount": application.amount},
        )
        try:
            recreate_game_application(application=application, amount=form.cleaned_data["amount"], recovery_request=recovery_request)
        except ValidationError as error:
            _add_recovery_error(form, error)
        else:
            messages.success(request, "A new canonical Game reward application was created; the original void history remains unchanged.")
            return redirect("recovery-game-detail", pk=application.game_id)
    return render(request, "core/recovery_game_recreate.html", {
        "application": application, "form": form,
        "cancel_url": reverse("recovery-game-detail", kwargs={"pk": application.game_id}),
    })


@login_required(login_url="config-login")
def recovery_checkpoint_list(request):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    checkpoints = ProgressCheckpoint.objects.select_related("month__group").annotate(
        recovery_result_count=Count("results"),
        recovery_met_count=Count(
            "results", filter=Q(results__outcome=ProgressCheckpointResult.Outcome.MET),
        ),
        recovery_below_count=Count(
            "results", filter=Q(results__outcome=ProgressCheckpointResult.Outcome.BELOW),
        ),
    ).order_by("month__group__name", "-month__starts_on", "position", "pk")
    return render(request, "core/recovery_checkpoint_list.html", {
        "checkpoints": checkpoints,
        "platform_timezone": get_platform_settings().timezone,
    })


@login_required(login_url="config-login")
def recovery_checkpoint_detail(request, pk):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    checkpoint = get_object_or_404(
        ProgressCheckpoint.objects.select_related("month__group"), pk=pk,
    )
    return render(request, "core/recovery_checkpoint_detail.html", {
        "checkpoint": checkpoint,
        "summary": checkpoint_result_summary(checkpoint),
        "platform_timezone": get_platform_settings().timezone,
    })


@sensitive_post_parameters("current_password")
@login_required(login_url="config-login")
def recovery_checkpoint_reset(request, pk):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    checkpoint = get_object_or_404(
        ProgressCheckpoint.objects.select_related("month__group"), pk=pk,
    )
    if checkpoint.evaluation_state != ProgressCheckpoint.EvaluationState.EVALUATED:
        messages.error(request, "Only an evaluated checkpoint is eligible for emergency reset.")
        return redirect("recovery-checkpoint-detail", pk=checkpoint.pk)
    label = checkpoint_recovery_label(checkpoint)
    impact = checkpoint_reset_impact(checkpoint)
    summary = checkpoint_result_summary(checkpoint)
    form = RecoveryConfirmationForm(
        request.POST or None, expected_confirmation=label, require_password=True,
    )
    if request.method == "POST" and form.is_valid():
        recovery_request = _recovery_request_from_form(
            request, form, tier=3, action="CHECKPOINT_EVALUATION_RESET",
            target_type="ProgressCheckpoint", target_id=checkpoint.pk,
            target_label=label, group=checkpoint.month.group,
            challenge=checkpoint.month, impact=impact,
            before_state={
                "configuration": checkpoint_configuration_snapshot(checkpoint),
                "evaluation_state": checkpoint.evaluation_state,
                "evaluated_at": checkpoint.evaluated_at.isoformat() if checkpoint.evaluated_at else None,
                **summary,
            },
        )
        try:
            reset_checkpoint_evaluation(
                checkpoint=checkpoint, recovery_request=recovery_request,
            )
        except ValidationError as error:
            _add_recovery_error(form, error)
        else:
            messages.success(
                request,
                "Checkpoint evaluation was reset to Pending under Recovery Hold. Automatic evaluation remains paused.",
            )
            return redirect("recovery-checkpoint-detail", pk=checkpoint.pk)
    return _recovery_confirmation_page(
        request, form=form, tier=3, impact=impact,
        title=f"Reset evaluation for checkpoint {checkpoint.position}?",
        description=(
            "All immutable Reader results from this evaluation will be removed together. "
            "Configuration remains intact, the checkpoint returns to Pending, and Recovery Hold pauses automatic processing."
        ),
        cancel_url=reverse("recovery-checkpoint-detail", kwargs={"pk": checkpoint.pk}),
        action_label="Reset Evaluation",
        checkpoint=checkpoint,
    )


@login_required(login_url="config-login")
def recovery_checkpoint_release(request, pk):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    checkpoint = get_object_or_404(
        ProgressCheckpoint.objects.select_related("month__group"), pk=pk,
    )
    if not checkpoint.recovery_hold:
        messages.error(request, "This checkpoint is not under Recovery Hold.")
        return redirect("recovery-checkpoint-detail", pk=checkpoint.pk)
    label = checkpoint_recovery_label(checkpoint)
    form = RecoveryConfirmationForm(request.POST or None, expected_confirmation=label)
    if request.method == "POST" and form.is_valid():
        recovery_request = _recovery_request_from_form(
            request, form, tier=2, action="CHECKPOINT_EVALUATION_RELEASED",
            target_type="ProgressCheckpoint", target_id=checkpoint.pk,
            target_label=label, group=checkpoint.month.group,
            challenge=checkpoint.month,
            before_state={
                "configuration": checkpoint_configuration_snapshot(checkpoint),
                "evaluation_state": checkpoint.evaluation_state,
                "recovery_hold": checkpoint.recovery_hold,
            },
        )
        try:
            release_checkpoint_evaluation(
                checkpoint=checkpoint, recovery_request=recovery_request,
            )
        except ValidationError as error:
            _add_recovery_error(form, error)
        else:
            messages.success(
                request,
                "Recovery Hold was released. The existing scheduler is now authoritative again.",
            )
            return redirect("recovery-checkpoint-detail", pk=checkpoint.pk)
    return _recovery_confirmation_page(
        request, form=form, tier=2,
        title=f"Release checkpoint {checkpoint.position} for evaluation?",
        description=(
            "This clears only Recovery Hold. It does not evaluate the checkpoint now. "
            "A past-due checkpoint will follow the existing scheduler and Challenge lifecycle rules on the next run."
        ),
        cancel_url=reverse("recovery-checkpoint-detail", kwargs={"pk": checkpoint.pk}),
        action_label="Release for Evaluation",
        checkpoint=checkpoint,
    )


@login_required(login_url="config-login")
def recovery_hardcover_list(request):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    group_connections = list(
        HardcoverConnection.objects.select_related("group").order_by("group__name", "pk")
    )
    reader_connections = list(
        ReaderHardcoverConnection.objects.select_related("user").order_by("user__username", "pk")
    )
    for connection in (*group_connections, *reader_connections):
        connection.recovery_status = sanitized_credential_status(connection)
    return render(request, "core/recovery_hardcover_list.html", {
        "group_connections": group_connections,
        "reader_connections": reader_connections,
        "platform_timezone": get_platform_settings().timezone,
    })


def _recovery_credential_clear_page(request, *, connection, owner_type):
    reader_connection = owner_type == "Reader"
    label = (
        reader_credential_label(connection)
        if reader_connection else group_credential_label(connection)
    )
    status = sanitized_credential_status(connection)
    form = RecoveryConfirmationForm(request.POST or None, expected_confirmation=label)
    if request.method == "POST" and form.is_valid():
        recovery_request = _recovery_request_from_form(
            request, form, tier=2,
            action=(
                "hardcover.reader_connection_cleared"
                if reader_connection else "hardcover.group_connection_cleared"
            ),
            target_type=connection.__class__.__name__, target_id=connection.pk,
            target_label=label,
            group=None if reader_connection else connection.group,
            before_state={
                "owner_type": owner_type,
                "owner_id": connection.user_id if reader_connection else connection.group_id,
                "connection_existed": True,
                "sanitized_status": status,
            },
        )
        try:
            if reader_connection:
                clear_reader_hardcover_connection(
                    connection=connection, recovery_request=recovery_request,
                )
            else:
                clear_group_hardcover_connection(
                    connection=connection, recovery_request=recovery_request,
                )
        except ValidationError as error:
            _add_recovery_error(form, error)
        else:
            messages.success(
                request,
                f"Broken {owner_type} Hardcover connection was cleared. Its owner may reconnect through the normal workflow.",
            )
            return redirect("recovery-hardcover-list")
    return render(request, "core/recovery_credential_clear.html", {
        "connection": connection, "owner_type": owner_type,
        "owner_label": connection.user.username if reader_connection else connection.group.name,
        "status": status, "form": form,
        "cancel_url": reverse("recovery-hardcover-list"),
        "platform_timezone": get_platform_settings().timezone,
    })


@login_required(login_url="config-login")
def recovery_reader_hardcover_clear(request, pk):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    connection = get_object_or_404(
        ReaderHardcoverConnection.objects.select_related("user"), pk=pk,
    )
    return _recovery_credential_clear_page(
        request, connection=connection, owner_type="Reader",
    )


@login_required(login_url="config-login")
def recovery_group_hardcover_clear(request, pk):
    denied = _recovery_owner_denied(request)
    if denied:
        return denied
    connection = get_object_or_404(
        HardcoverConnection.objects.select_related("group"), pk=pk,
    )
    return _recovery_credential_clear_page(
        request, connection=connection, owner_type="Group",
    )


def _platform_group_directory(group_slug=None):
    owner_memberships = Membership.objects.filter(
        is_active=True,
        role=Membership.Role.OWNER,
        user__is_superuser=False,
    ).select_related("user")
    current_months = ChallengeMonth.objects.exclude(
        status=ChallengeMonth.Status.ARCHIVED
    ).order_by("-starts_on")
    latest_event = AuditEvent.objects.filter(group=OuterRef("pk")).order_by("-created_at")
    group_query = ReadingGroup.objects.annotate(
        participant_count=Count(
            "memberships",
            filter=Q(memberships__is_active=True, memberships__user__is_superuser=False),
            distinct=True,
        ),
        recent_activity_summary=Subquery(latest_event.values("summary")[:1]),
        recent_activity_at=Subquery(latest_event.values("created_at")[:1]),
        recent_activity_actor=Subquery(latest_event.values("actor__username")[:1]),
    )
    if group_slug is not None:
        group_query = group_query.filter(slug=group_slug)
    groups = list(
        group_query
        .prefetch_related(
            Prefetch("memberships", queryset=owner_memberships, to_attr="directory_owners"),
            Prefetch("challenge_months", queryset=current_months, to_attr="directory_months"),
        )
        .order_by("name", "pk")
    )
    for group in groups:
        group.current_challenge = next(
            (month for month in group.directory_months if month.status == ChallengeMonth.Status.ACTIVE),
            group.directory_months[0] if group.directory_months else None,
        )
    return groups


@login_required(login_url="config-login")
def config_group_list(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Platform owner access is required.")
    return render(request, "core/config_group_list.html", {"groups": _platform_group_directory()})


@login_required(login_url="config-login")
def config_group_detail(request, group_slug):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Platform owner access is required.")
    group = next(iter(_platform_group_directory(group_slug)), None)
    if group is None:
        raise Http404
    return render(request, "core/config_group_detail.html", {"group": group})


@login_required(login_url="config-login")
def config_group_status_toggle(request, group_slug):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Platform owner access is required.")
    group = get_object_or_404(ReadingGroup, slug=group_slug)
    return redirect("recovery-group-status", pk=group.pk)


@login_required(login_url="config-login")
def platform_owner_list(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Platform owner access is required.")
    owners = get_user_model().objects.filter(is_superuser=True).order_by("username")
    invitations = PlatformOwnerInvitation.objects.select_related("created_by", "redeemed_by", "revoked_by")
    return render(request, "core/platform_owner_list.html", {"owners": owners, "invitations": invitations})


@login_required(login_url="config-login")
def platform_owner_create(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Platform owner access is required.")
    form = PlatformOwnerInvitationForm(request.POST or None, owner=request.user)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            invitation, token = PlatformOwnerInvitation.issue(request.user)
            AuditEvent.objects.create(
                actor=request.user,
                action="platform.owner_invitation_created",
                object_type="PlatformOwnerInvitation",
                object_id=str(invitation.pk),
                summary="Created a seven-day platform owner invitation.",
            )
        base_url = settings.NORTHBOUND_URL or request.build_absolute_uri("/").rstrip("/")
        invitation_url = f"{base_url}{reverse('platform-owner-accept', kwargs={'token': token})}"
        return render(request, "core/platform_owner_invitation_created.html", {
            "invitation": invitation,
            "invitation_url": invitation_url,
        })
    return render(request, "core/platform_owner_create.html", {"form": form})


def platform_owner_accept(request, token):
    invitation = PlatformOwnerInvitation.objects.filter(
        token_hash=hash_platform_owner_invitation_token(token)
    ).first()
    if not invitation or not invitation.is_valid:
        return render(request, "core/platform_owner_invitation_invalid.html", status=410)

    form = PlatformOwnerAcceptanceForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            invitation = PlatformOwnerInvitation.objects.select_for_update().get(pk=invitation.pk)
            if not invitation.is_valid:
                return render(request, "core/platform_owner_invitation_invalid.html", status=410)
            owner = form.save()
            invitation.redeemed_at = timezone.now()
            invitation.redeemed_by = owner
            invitation.save(update_fields=["redeemed_at", "redeemed_by"])
            AuditEvent.objects.create(
                actor=owner,
                action="platform.owner_invitation_redeemed",
                object_type="PlatformOwnerInvitation",
                object_id=str(invitation.pk),
                summary=f"Redeemed a platform owner invitation as {owner.username}.",
            )
        login(request, owner)
        messages.success(request, "Your Platform Owner account is ready.")
        return redirect("config-dashboard")
    return render(request, "core/platform_owner_accept.html", {"form": form, "invitation": invitation})


@login_required(login_url="config-login")
def platform_owner_invitation_revoke(request, pk):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Platform owner access is required.")
    invitation = get_object_or_404(PlatformOwnerInvitation, pk=pk)
    if request.method == "POST" and invitation.is_valid:
        invitation.revoked_at = timezone.now()
        invitation.revoked_by = request.user
        invitation.save(update_fields=["revoked_at", "revoked_by"])
        AuditEvent.objects.create(
            actor=request.user,
            action="platform.owner_invitation_revoked",
            object_type="PlatformOwnerInvitation",
            object_id=str(invitation.pk),
            summary="Revoked an unused platform owner invitation.",
        )
        messages.success(request, "The platform owner invitation was revoked.")
    return redirect("platform-owner-list")


@login_required(login_url="config-login")
def platform_owner_status_toggle(request, pk):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Platform owner access is required.")
    owner = get_object_or_404(get_user_model(), pk=pk, is_superuser=True)
    return redirect("recovery-account-status", pk=owner.pk)


@login_required(login_url="config-login")
def config_user_list(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Platform owner access is required.")
    users = get_user_model().objects.filter(is_superuser=False).annotate(
        membership_count=Count("reading_memberships", distinct=True)
    ).order_by("username")
    return render(request, "core/config_user_list.html", {"users": users})


@login_required(login_url="config-login")
def config_user_detail(request, pk):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Platform owner access is required.")
    account_user = get_object_or_404(get_user_model(), pk=pk, is_superuser=False)
    profile, _ = UserProfile.objects.get_or_create(user=account_user)
    current_statuses = (
        ChallengeMonth.Status.UPCOMING,
        ChallengeMonth.Status.ACTIVE,
        ChallengeMonth.Status.FINALIZING,
    )
    memberships = account_user.reading_memberships.select_related("group").prefetch_related(
        Prefetch(
            "month_enrollments",
            queryset=MonthEnrollment.objects.filter(month__status__in=current_statuses, is_active=True).select_related("month"),
            to_attr="current_challenge_enrollments",
        ),
        Prefetch(
            "challenge_staff_assignments",
            queryset=ChallengeStaffAssignment.objects.filter(
                month__status__in=current_statuses,
                ended_at__isnull=True,
            ).select_related("month", "team"),
            to_attr="current_challenge_staffing",
        ),
    ).order_by("group__name")
    return render(request, "core/config_user_detail.html", {
        "account_user": account_user,
        "profile": profile,
        "memberships": memberships,
    })


@login_required(login_url="config-login")
def config_user_edit(request, pk):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Platform owner access is required.")
    account_user = get_object_or_404(get_user_model(), pk=pk, is_superuser=False)
    original_identity = {
        "username": account_user.username,
        "first_name": account_user.first_name,
        "last_name": account_user.last_name,
        "email": account_user.email,
        "discord_username": UserProfile.objects.get_or_create(user=account_user)[0].discord_username,
    }
    form = PlatformAccountIdentityForm(request.POST or None, instance=account_user)
    if request.method == "POST" and form.is_valid():
        changes = []
        field_labels = {
            "username": "username",
            "first_name": "first name",
            "last_name": "last name",
            "email": "email address",
            "discord_username": "Discord username",
        }
        for field_name, label in field_labels.items():
            if original_identity[field_name] != form.cleaned_data[field_name]:
                changes.append(label)
        with transaction.atomic():
            updated = form.save()
            if changes:
                AuditEvent.objects.create(
                    actor=request.user,
                    action="account.identity_updated",
                    object_type="User",
                    object_id=str(updated.pk),
                    summary=f"Updated identity fields for account {updated.username}: {', '.join(changes)}.",
                )
        if changes:
            messages.success(request, f"Account identity for {updated.username} was updated.")
        else:
            messages.info(request, f"No identity changes were needed for {updated.username}.")
        return redirect("config-user-detail", pk=updated.pk)
    return render(request, "core/config_user_edit.html", {
        "account_user": account_user,
        "form": form,
    })


@login_required(login_url="config-login")
def config_user_password_reset(request, pk):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Platform owner access is required.")
    account_user = get_object_or_404(get_user_model(), pk=pk, is_superuser=False)
    if request.method == "POST":
        temporary_password = secrets.token_urlsafe(12)
        account_user.set_password(temporary_password)
        account_user.save(update_fields=["password"])
        profile, _ = UserProfile.objects.get_or_create(user=account_user)
        profile.must_change_password = True
        profile.save(update_fields=["must_change_password"])
        AuditEvent.objects.create(
            actor=request.user,
            action="account.temporary_password_issued",
            object_type="User",
            object_id=str(account_user.pk),
            summary=f"Issued a temporary password for {account_user.username}; forced password change enabled.",
        )
        return render(request, "core/config_temporary_password.html", {"account_user": account_user, "temporary_password": temporary_password})
    return render(request, "core/confirm_remove.html", {
        "eyebrow": "Platform User Management",
        "title": f"Reset {account_user.username}'s password?",
        "description": "Northbound will generate a temporary password and require the user to replace it after signing in. Their existing password cannot be viewed or recovered.",
        "cancel_url": reverse("config-user-detail", kwargs={"pk": account_user.pk}),
        "action_label": "Generate Temporary Password",
        "hide_reason": True,
    })


@login_required(login_url="config-login")
def config_user_status_toggle(request, pk):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Platform owner access is required.")
    account_user = get_object_or_404(get_user_model(), pk=pk, is_superuser=False)
    return redirect("recovery-account-status", pk=account_user.pk)


AUDIT_PAGE_SIZE = 50


def _audit_date_bounds(value):
    try:
        selected_date = date.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    platform_timezone = get_platform_timezone()
    start = datetime.combine(selected_date, time.min, tzinfo=platform_timezone)
    end = datetime.combine(selected_date + timedelta(days=1), time.min, tzinfo=platform_timezone)
    return start, end


def _filtered_audit_events(request):
    events = AuditEvent.objects.select_related("actor", "group")
    filters = {
        "search": request.GET.get("search", "").strip(),
        "action": request.GET.get("action", "").strip(),
        "actor": request.GET.get("actor", "").strip(),
        "group": request.GET.get("group", "").strip(),
        "date": request.GET.get("date", "").strip(),
    }
    if filters["search"]:
        events = events.filter(summary__icontains=filters["search"])
    if filters["action"]:
        events = events.filter(action=filters["action"])
    if filters["actor"] == "system":
        events = events.filter(actor__isnull=True)
    elif filters["actor"].isdigit():
        events = events.filter(actor_id=int(filters["actor"]))
    if filters["group"].isdigit():
        events = events.filter(group_id=int(filters["group"]))
    date_bounds = _audit_date_bounds(filters["date"])
    if date_bounds:
        events = events.filter(created_at__gte=date_bounds[0], created_at__lt=date_bounds[1])
    return events, filters


def _audit_filter_options():
    actions = sorted(
        (
            {"value": action, "label": audit_action_label(action)}
            for action in AuditEvent.objects.values_list("action", flat=True).distinct()
        ),
        key=lambda item: (item["label"], item["value"]),
    )
    actors = [
        {"id": actor_id, "username": username, "is_active": is_active}
        for actor_id, username, is_active in AuditEvent.objects.exclude(actor__isnull=True)
        .values_list("actor_id", "actor__username", "actor__is_active")
        .distinct()
        .order_by("actor__username")
    ]
    groups = [
        {"id": group_id, "name": name}
        for group_id, name in AuditEvent.objects.exclude(group__isnull=True)
        .values_list("group_id", "group__name")
        .distinct()
        .order_by("group__name")
    ]
    return actions, actors, groups


@login_required(login_url="config-login")
def config_audit(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Platform owner access is required.")
    events, filters = _filtered_audit_events(request)
    paginator = Paginator(events, AUDIT_PAGE_SIZE)
    page_obj = paginator.get_page(request.GET.get("page"))
    query = request.GET.copy()
    query.pop("page", None)
    actions, actors, groups = _audit_filter_options()
    return render(request, "core/config_audit.html", {
        "events": page_obj.object_list,
        "page_obj": page_obj,
        "filters": filters,
        "filters_active": any(filters.values()),
        "filter_query": query.urlencode(),
        "action_options": actions,
        "actor_options": actors,
        "group_options": groups,
        "platform_timezone": get_platform_settings().timezone,
    })


def _csv_safe(value):
    text = str(value or "")
    return f"'{text}" if text.startswith(("=", "+", "-", "@")) else text


@login_required(login_url="config-login")
def config_audit_export(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Platform owner access is required.")
    events, _ = _filtered_audit_events(request)
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    platform_timezone = get_platform_timezone()
    export_date = timezone.localdate(timezone=platform_timezone).isoformat()
    response["Content-Disposition"] = f'attachment; filename="northbound-audit-activity-{export_date}.csv"'
    writer = csv.writer(response)
    writer.writerow(["Timestamp", "Action", "Action Identifier", "Actor", "Group", "Summary"])
    for event in events.iterator():
        local_timestamp = timezone.localtime(event.created_at, platform_timezone).isoformat()
        writer.writerow([
            local_timestamp,
            _csv_safe(event.action_label),
            _csv_safe(event.action),
            _csv_safe(event.actor.username if event.actor else "System"),
            _csv_safe(event.group.name if event.group else "Platform"),
            _csv_safe(safe_audit_summary(event.summary)),
        ])
    return response


@login_required(login_url="config-login")
def platform_settings(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Platform owner access is required.")
    return redirect("config-dashboard")


@login_required(login_url="config-login")
def platform_general_settings(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Platform owner access is required.")
    platform_settings = get_platform_settings()
    original_values = {
        field: getattr(platform_settings, field)
        for field in (
            "display_name",
            "timezone",
            "allow_public_registration",
            "allow_user_group_creation",
        )
    }
    form = PlatformSettingsForm(request.POST or None, instance=platform_settings)
    if request.method == "POST" and form.is_valid():
        changed_fields = list(form.changed_data)
        form.save()
        if changed_fields:
            field_labels = {
                "display_name": "Platform display name",
                "timezone": "Platform timezone",
                "allow_public_registration": "Public registration",
                "allow_user_group_creation": "Normal account group creation",
            }

            def display_value(value):
                return "Enabled" if value is True else "Disabled" if value is False else str(value)

            changes = "; ".join(
                f"{field_labels[field]} changed from {display_value(original_values[field])} to "
                f"{display_value(form.cleaned_data[field])}"
                for field in changed_fields
            )
            AuditEvent.objects.create(
                actor=request.user,
                action="platform.general_settings_updated",
                object_type="PlatformSettings",
                object_id=str(platform_settings.pk),
                summary=f"Updated General Settings: {changes}.",
            )
            messages.success(request, "General Settings were updated.")
        else:
            messages.info(request, "General Settings were already up to date.")
        return redirect("platform-general-settings")
    return render(request, "core/platform_general_settings.html", {"form": form})


@login_required(login_url="config-login")
@sensitive_post_parameters("client_secret")
@sensitive_variables("client_secret")
def platform_hardcover_oauth(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Platform owner access is required.")
    application = HardcoverOAuthApplication.objects.first()
    form = HardcoverOAuthApplicationForm(request.POST or None, instance=application)
    urls = canonical_oauth_urls()
    if request.method == "POST" and form.is_valid():
        client_secret = form.cleaned_data["client_secret"]
        if urls.error:
            form.add_error(None, urls.error)
        else:
            created = application is None
            application = application or HardcoverOAuthApplication()
            application, replaced_secret = save_oauth_application(
                application=application,
                client_id=form.cleaned_data["client_id"],
                client_secret=client_secret,
                enabled=form.cleaned_data["enabled"],
                urls=urls,
            )
            action = "platform.hardcover_oauth_created" if created else "platform.hardcover_oauth_updated"
            details = []
            if replaced_secret:
                details.append("client secret replaced")
            details.append("enabled" if application.enabled else "disabled")
            AuditEvent.objects.create(
                actor=request.user,
                action=action,
                object_type="HardcoverOAuthApplication",
                object_id=str(application.pk),
                summary=f"Hardcover OAuth configuration saved ({'; '.join(details)}).",
            )
            messages.success(request, "Hardcover OAuth configuration was saved locally.")
            return redirect("platform-hardcover-oauth")
    return render(request, "core/platform_hardcover_oauth.html", {
        "form": form,
        "application": application,
        "oauth_status": oauth_application_status(application, urls),
        "oauth_scopes": OAUTH_SCOPES,
        "website_url": urls.website_url,
        "redirect_uri": urls.redirect_uri,
        "url_error": urls.error,
    })


@login_required(login_url="config-login")
def platform_system_status(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Platform owner access is required.")
    return render(request, "core/platform_system_status.html", build_system_status())


@login_required(login_url="config-login")
def platform_storage_maintenance(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Platform owner access is required.")
    context = storage_overview()
    context.update({
        "audit_retention_years": AUDIT_RETENTION_YEARS,
        "platform_timezone": get_platform_settings().timezone,
    })
    return render(request, "core/platform_storage_maintenance.html", context)


def _maintenance_confirmation(request, **context):
    context.setdefault("error", "")
    context.setdefault("cancel_url", reverse("platform-storage-maintenance"))
    return render(request, "core/platform_maintenance_confirm.html", context)


@login_required(login_url="config-login")
def platform_cache_cleanup(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Platform owner access is required.")
    usage = disposable_cache_usage()
    if not usage["count"]:
        messages.info(request, "There are currently no disposable cache files eligible for cleanup.")
        return redirect("platform-storage-maintenance")
    context = {
        "eyebrow": "Disposable Cache Cleanup",
        "title": "Remove Disposable Cache Files?",
        "description": (
            f"This permanently removes {usage['count']} explicitly reproducible cache file(s) "
            f"using approximately {filesizeformat(usage['size'])}. Persistent media and Stored Backups are not affected."
        ),
        "confirmation_word": "CLEANUP",
        "action_label": "Clean Disposable Cache",
    }
    if request.method == "POST":
        if request.POST.get("confirmation") != "CLEANUP":
            context["error"] = "Enter CLEANUP exactly to confirm."
        else:
            try:
                result = cleanup_disposable_cache(actor=request.user)
            except (MaintenanceBusyError, RuntimeError) as exc:
                context["error"] = str(exc)
            else:
                message = (
                    f"Removed {result['count']} disposable cache file(s) and reclaimed "
                    f"{filesizeformat(result['size'])}."
                )
                if result["failed_count"]:
                    messages.warning(
                        request,
                        f"{message} {result['failed_count']} file(s) could not be removed.",
                    )
                else:
                    messages.success(request, message)
                return redirect("platform-storage-maintenance")
    return _maintenance_confirmation(request, **context)


@login_required(login_url="config-login")
def platform_audit_prune(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Platform owner access is required.")
    try:
        years = int(request.POST.get("years") or request.GET.get("years") or "")
        preview = audit_prune_preview(years)
    except (TypeError, ValueError):
        messages.error(request, "Choose one, two, or three years of audit history to retain.")
        return redirect("platform-storage-maintenance")
    context = {
        "eyebrow": "Audit History Pruning",
        "title": "Permanently Prune Audit History?",
        "description": (
            f"This permanently deletes {preview['affected_count']} audit event(s) older than "
            f"{years} year(s). Deleted history can be recovered only by restoring an appropriate backup."
        ),
        "confirmation_word": "PRUNE",
        "action_label": "Prune Audit History",
        "hidden_fields": {"years": years},
    }
    if request.method == "POST":
        if request.POST.get("confirmation") != "PRUNE":
            context["error"] = "Enter PRUNE exactly to confirm."
        else:
            try:
                result = prune_audit_history(years=years, actor=request.user)
            except (MaintenanceBusyError, RuntimeError) as exc:
                context["error"] = str(exc)
            else:
                messages.success(
                    request,
                    f"Pruned {result['count']} audit event(s) older than {years} year(s).",
                )
                return redirect("platform-storage-maintenance")
    return _maintenance_confirmation(request, **context)


@login_required(login_url="config-login")
def platform_sqlite_optimize(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Platform owner access is required.")
    if connection.vendor != "sqlite":
        messages.info(request, "Northbound database optimization is available only for SQLite deployments.")
        return redirect("platform-storage-maintenance")
    overview = storage_overview()
    context = {
        "eyebrow": "SQLite Database Optimization",
        "title": "Optimize the SQLite Database?",
        "description": (
            f"The database currently uses approximately {filesizeformat(overview['database_size'] or 0)}. "
            "Optimization compacts unused pages and may briefly require exclusive database access. "
            "It does not delete live records."
        ),
        "confirmation_word": "OPTIMIZE",
        "action_label": "Optimize Database",
    }
    if request.method == "POST":
        if request.POST.get("confirmation") != "OPTIMIZE":
            context["error"] = "Enter OPTIMIZE exactly to confirm."
        else:
            try:
                result = optimize_sqlite_database(actor=request.user)
            except (MaintenanceBusyError, RuntimeError, OSError) as exc:
                context["error"] = str(exc)
            else:
                messages.success(
                    request,
                    f"SQLite optimization completed. Database size changed from "
                    f"{filesizeformat(result['before_size'])} to {filesizeformat(result['after_size'])}; "
                    f"{filesizeformat(result['reclaimed'])} reclaimed.",
                )
                return redirect("platform-storage-maintenance")
    return _maintenance_confirmation(request, **context)


def _stored_backup_or_404(filename):
    try:
        backup_path = stored_backup_path(filename)
    except ValueError:
        raise Http404
    if not backup_path.is_file():
        raise Http404
    return backup_path


@login_required(login_url="config-login")
def platform_backups(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Platform owner access is required.")
    is_sqlite = connection.vendor == "sqlite"
    backup_settings = PlatformBackupSettings.load()
    backup_settings_form = PlatformBackupSettingsForm(request.POST or None, instance=backup_settings)
    if request.method == "POST" and backup_settings_form.is_valid():
        backup_settings_form.save()
        if is_sqlite:
            for expired_backup in list_automatic_backups()[backup_settings.retention_count:]:
                expired_backup.unlink(missing_ok=True)
        selected_days = ", ".join(dict(PlatformBackupSettings.Weekday.choices)[day] for day in backup_settings.weekdays)
        AuditEvent.objects.create(actor=request.user, action="platform.backup_settings_updated", object_type="PlatformBackupSettings", object_id=str(backup_settings.pk), summary=f"Updated automatic backups to {selected_days} at {backup_settings.backup_time}; retaining {backup_settings.retention_count}.")
        messages.success(request, "Automatic backup settings were updated.")
        return redirect("platform-backups")
    stored_paths = list_stored_backups() if is_sqlite else []
    stored_backups = [{
        "name": path.name,
        "size": path.stat().st_size,
        "modified": datetime.fromtimestamp(path.stat().st_mtime, tz=get_platform_timezone()),
        "kind": "Automatic" if path.name.startswith("northbound-automatic-") else "Manual",
    } for path in stored_paths]
    return render(request, "core/platform_backups.html", {
        "is_sqlite": is_sqlite,
        "restore_pending": pending_restore_path().exists() if is_sqlite else False,
        "web_restart_enabled": settings.NORTHBOUND_WEB_RESTART,
        "backup_settings_form": backup_settings_form,
        "stored_backups": stored_backups,
        "backup_location": str(automatic_backup_directory()) if is_sqlite else None,
        "stored_backup_size": sum(backup["size"] for backup in stored_backups),
        "next_scheduled_run": next_scheduled_backup(backup_settings) if is_sqlite else None,
        "platform_timezone": get_platform_settings().timezone,
    })


@login_required(login_url="config-login")
def platform_backup_create(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Platform owner access is required.")
    if request.method != "POST":
        return redirect("platform-backups")
    if connection.vendor != "sqlite":
        messages.error(request, "In-app backups currently support the standard SQLite deployment. Back up PostgreSQL with its native tools.")
        return redirect("platform-backups")
    try:
        backup_path = create_stored_backup()
    except (OSError, ValueError, sqlite3.Error) as exc:
        messages.error(request, f"The backup could not be created: {exc}")
        return redirect("platform-backups")
    AuditEvent.objects.create(
        actor=request.user,
        action="platform.backup_created",
        object_type="PlatformBackup",
        object_id=backup_path.name,
        summary="Created a stored manual backup containing SQLite data and uploaded media.",
    )
    messages.success(request, "Backup created and added to Stored Backups.")
    return redirect("platform-backups")


@login_required(login_url="config-login")
def stored_backup_download(request, filename):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Platform owner access is required.")
    backup_path = _stored_backup_or_404(filename)
    AuditEvent.objects.create(
        actor=request.user,
        action="platform.backup_downloaded",
        object_type="PlatformBackup",
        object_id=filename,
        summary=f"Downloaded stored backup {filename}.",
    )
    return FileResponse(backup_path.open("rb"), as_attachment=True, filename=filename, content_type="application/zip")


@login_required(login_url="config-login")
def stored_backup_delete(request, filename):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Platform owner access is required.")
    backup_path = _stored_backup_or_404(filename)
    if request.method == "POST":
        backup_path.unlink()
        AuditEvent.objects.create(
            actor=request.user,
            action="platform.backup_deleted",
            object_type="PlatformBackup",
            object_id=filename,
            summary=f"Deleted stored backup {filename}.",
        )
        messages.success(request, "The stored backup was deleted.")
        return redirect("platform-backups")
    return render(request, "core/confirm_remove.html", {
        "eyebrow": "Stored Backups",
        "title": "Delete This Backup?",
        "description": f"{filename} will be permanently removed from stored backups.",
        "cancel_url": reverse("platform-backups"),
        "action_label": "Delete Backup",
        "hide_reason": True,
    })


@login_required(login_url="config-login")
def platform_backup_restore(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Platform owner access is required.")
    if request.method != "POST" or connection.vendor != "sqlite":
        return redirect("platform-backups")
    uploaded_backup = request.FILES.get("backup")
    if not uploaded_backup:
        messages.error(request, "Choose a Northbound backup ZIP to restore.")
        return redirect("platform-backups")
    try:
        stage_restore(uploaded_backup)
    except (ValueError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
        messages.error(request, f"The backup could not be staged: {exc}")
        return redirect("platform-backups")
    AuditEvent.objects.create(actor=request.user, action="platform.restore_staged", object_type="PlatformBackup", summary="Validated and staged a platform restore for the next application restart.")
    messages.success(request, "Backup validated and staged. Restart Northbound to apply it before the web server starts.")
    return redirect("platform-backups")


@require_GET
def health(request):
    return health_response()


def _restore_confirmation_error(request):
    if not request.user.check_password(request.POST.get("current_password", "")):
        return "Your current password is incorrect."
    if request.POST.get("confirmation") != "RESTORE":
        return "Enter RESTORE exactly to confirm."
    return ""


def _render_restore_restarting(request):
    AuditEvent.objects.create(
        actor=request.user,
        action="platform.restore_restart_requested",
        object_type="PlatformBackup",
        summary="Requested a graceful application restart to apply the staged restore.",
    )

    def stop_gunicorn():
        try:
            master_pid = int(Path("/tmp/northbound-gunicorn.pid").read_text().strip())
            os.kill(master_pid, signal.SIGTERM)
        except (OSError, ValueError):
            pass

    threading.Timer(2, stop_gunicorn).start()
    return render(request, "core/platform_restore_restarting.html")


@login_required(login_url="config-login")
def stored_backup_restore(request, filename):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Platform owner access is required.")
    if connection.vendor != "sqlite":
        return redirect("platform-backups")
    backup_path = _stored_backup_or_404(filename)
    error = ""
    if request.method == "POST":
        error = _restore_confirmation_error(request)
        if not error:
            try:
                stage_stored_restore(backup_path)
            except (ValueError, zipfile.BadZipFile, json.JSONDecodeError, OSError) as exc:
                error = f"The backup could not be staged: {exc}"
            else:
                AuditEvent.objects.create(
                    actor=request.user,
                    action="platform.restore_staged",
                    object_type="PlatformBackup",
                    object_id=filename,
                    summary=f"Validated and staged stored backup {filename} for restoration.",
                )
                if settings.NORTHBOUND_WEB_RESTART:
                    return _render_restore_restarting(request)
                messages.success(
                    request,
                    "Backup validated and staged. Restart Northbound to apply it before the web server starts.",
                )
                return redirect("platform-backups")
    return render(request, "core/platform_restore_restart.html", {
        "error": error,
        "backup_name": filename,
        "form_action": reverse("stored-backup-restore", kwargs={"filename": filename}),
    })


@login_required(login_url="config-login")
def platform_restore_restart(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Platform owner access is required.")
    if not settings.NORTHBOUND_WEB_RESTART or connection.vendor != "sqlite" or not pending_restore_path().exists():
        messages.error(request, "A web-controlled restore restart is not available.")
        return redirect("platform-backups")
    error = ""
    if request.method == "POST":
        error = _restore_confirmation_error(request)
        if not error:
            return _render_restore_restarting(request)
    return render(request, "core/platform_restore_restart.html", {
        "error": error,
        "form_action": reverse("platform-restore-restart"),
    })


def setup(request):
    if get_user_model().objects.filter(is_superuser=True).exists():
        if request.user.is_authenticated and request.user.is_superuser:
            return redirect("config-dashboard")
        return redirect("config-login")
    form = FirstRunSetupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            if get_user_model().objects.filter(is_superuser=True).exists():
                return redirect("config-login")
            user = form.save()
            AuditEvent.objects.create(
                actor=user,
                action="platform.initial_owner_created",
                object_type="User",
                object_id=str(user.pk),
                summary="Completed first-run setup and created the initial platform owner.",
            )
        login(request, user)
        messages.success(request, "Platform setup complete.")
        return redirect("config-dashboard")
    return render(request, "core/setup.html", {"form": form})


def register(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    platform_settings = get_platform_settings()
    if not platform_settings.allow_public_registration:
        return render(request, "registration/registration_unavailable.html", status=403)
    form = PublicRegistrationForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        login(request, user)
        if platform_settings.allow_user_group_creation:
            messages.success(request, "Account created. You can create a reading group or join one with its access code.")
        else:
            messages.success(request, "Account created. Join an existing reading group with its access code.")
        return redirect("dashboard")
    return render(request, "registration/register.html", {"form": form})


@login_required
def dashboard(request):
    if request.user.is_superuser:
        return redirect("config-dashboard")
    groups = ReadingGroup.objects.filter(memberships__user=request.user, memberships__is_active=True, is_active=True).distinct()
    return render(request, "core/dashboard.html", {"groups": groups})


@login_required
@sensitive_post_parameters("hardcover_api_token")
@sensitive_variables("token")
def group_create(request):
    if not request.user.is_superuser and not get_platform_settings().allow_user_group_creation:
        return render(request, "core/group_creation_unavailable.html", status=403)
    form = GroupCreateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        token = form.cleaned_data.get("hardcover_api_token", "")
        if token:
            try:
                test_catalog_connection(token)
            except HardcoverConnectionError as exc:
                form.add_error("hardcover_api_token", str(exc))
                return render(request, "core/group_create.html", {"form": form})
        with transaction.atomic():
            group = form.save(request.user)
            if token:
                HardcoverConnection.objects.create(
                    group=group,
                    encrypted_token=encrypt_token(token),
                    token_hint=token[-4:],
                    tested_at=timezone.now(),
                    is_valid=True,
                )
            AuditEvent.objects.create(actor=request.user, group=group, action="group.created", object_type="ReadingGroup", object_id=str(group.pk), summary=f"Created reading group {group.name}")
        messages.success(request, f"{group.name} was created. Share its six-character access code with invited readers.")
        if request.user.is_superuser:
            return redirect("config-group-detail", group_slug=group.slug)
        return redirect("group-detail", group_slug=group.slug)
    return render(request, "core/group_create.html", {"form": form})


@login_required
@sensitive_post_parameters("api_token")
@sensitive_variables("token")
def group_edit(request, group_slug):
    group = get_object_or_404(ReadingGroup, slug=group_slug, is_active=True)
    if not can_manage_group(request.user, group):
        return HttpResponseForbidden("Group settings permission is required.")
    previous_name = group.name
    previous_timezone = group.timezone
    connection = HardcoverConnection.objects.filter(group=group).first()
    action = request.POST.get("action", "save_group") if request.method == "POST" else None
    form = GroupEditForm(request.POST if action == "save_group" else None, instance=group)
    hardcover_form = HardcoverConnectionForm(request.POST if action == "save_hardcover" else None)
    if action == "save_group" and form.is_valid():
        updated = form.save()
        AuditEvent.objects.create(
            actor=request.user,
            group=updated,
            action="group.updated",
            object_type="ReadingGroup",
            object_id=str(updated.pk),
            summary=f"Updated group name from {previous_name} to {updated.name} and timezone from {previous_timezone} to {updated.timezone}.",
        )
        messages.success(request, "Group details updated.")
        return redirect("group-detail", group_slug=updated.slug)
    if action == "test_existing" and connection:
        try:
            token = decrypt_token(connection.encrypted_token)
            test_catalog_connection(token)
        except (TokenDecryptionError, HardcoverConnectionError) as exc:
            connection.is_valid = False
            connection.last_error = str(exc)[:300]
            messages.error(request, str(exc))
        else:
            connection.is_valid = True
            connection.last_error = ""
            messages.success(request, "Hardcover catalog access is working.")
        connection.tested_at = timezone.now()
        connection.save(update_fields=["is_valid", "last_error", "tested_at"])
        return redirect("group-edit", group_slug=group.slug)
    if action == "save_hardcover" and hardcover_form.is_valid():
        token = hardcover_form.cleaned_data["api_token"]
        try:
            test_catalog_connection(token)
        except HardcoverConnectionError as exc:
            hardcover_form.add_error("api_token", str(exc))
        else:
            connection, _ = HardcoverConnection.objects.update_or_create(
                group=group,
                defaults={"encrypted_token": encrypt_token(token), "token_hint": token[-4:], "tested_at": timezone.now(), "is_valid": True, "last_error": ""},
            )
            AuditEvent.objects.create(actor=request.user, group=group, action="hardcover.connected", object_type="HardcoverConnection", object_id=str(connection.pk), summary="Saved and tested a read-only Hardcover catalog connection.")
            messages.success(request, "Hardcover catalog access was connected successfully.")
            return redirect("group-edit", group_slug=group.slug)
    return render(request, "core/group_edit.html", {"form": form, "hardcover_form": hardcover_form, "group": group, "connection": connection})


@login_required
def group_join(request):
    form = GroupJoinForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        group = form.cleaned_data["group"]
        membership, created = Membership.objects.get_or_create(
            group=group,
            user=request.user,
            defaults={"role": Membership.Role.MEMBER, "display_name": request.user.get_full_name() or request.user.username, "is_active": True},
        )
        if not created and membership.is_active:
            messages.info(request, "You are already a member of that reading group.")
        else:
            if not created:
                membership.is_active = True
                membership.role = Membership.Role.MEMBER
                membership.save(update_fields=["is_active", "role"])
            AuditEvent.objects.create(actor=request.user, group=group, action="membership.joined", object_type="Membership", object_id=str(membership.pk), summary=f"{membership.display_name} joined using the group access code")
            messages.success(request, f"You joined {group.name} as a member.")
        return redirect("group-detail", group_slug=group.slug)
    return render(request, "core/form_page.html", {"form": form, "title": "Join a Reading Group", "eyebrow": "Invitation Access"})


@login_required
def group_access_code(request, group_slug):
    group = get_object_or_404(ReadingGroup, slug=group_slug)
    can_manage_access_code = can_manage_group(request.user, group)
    if not can_manage_access_code and not can_view_access_code(request.user, group):
        return HttpResponseForbidden("You do not have permission to view this group access code.")
    if request.method == "POST" and not can_manage_access_code:
        return HttpResponseForbidden("Group settings permission is required.")
    form = GroupAccessCodeForm(request.POST or None, group=group)
    if request.method == "POST" and form.is_valid():
        form.save(group)
        AuditEvent.objects.create(actor=request.user, group=group, action="group.access_code_changed", object_type="ReadingGroup", object_id=str(group.pk), summary=f"Updated the group access code and visibility ({group.access_code_visibility}).")
        messages.success(request, "The group access code settings were updated.")
        return redirect("group-detail", group_slug=group.slug)
    return render(request, "core/access_code.html", {"form": form, "group": group, "can_manage_access_code": can_manage_access_code})


@login_required
def group_hardcover_connection(request, group_slug):
    group = get_object_or_404(ReadingGroup, slug=group_slug, is_active=True)
    if not can_manage_group(request.user, group):
        return HttpResponseForbidden("Group settings permission is required.")
    return redirect("group-edit", group_slug=group.slug)


@login_required
@sensitive_post_parameters("api_token")
@sensitive_variables("token")
def hardcover_test_token(request):
    if request.method != "POST":
        return JsonResponse({"ok": False, "message": "A POST request is required."}, status=405)
    token = request.POST.get("api_token", "").strip()
    if not token:
        return JsonResponse({"ok": False, "message": "Enter a token first."}, status=400)
    try:
        test_catalog_connection(token)
    except HardcoverConnectionError as exc:
        return JsonResponse({"ok": False, "message": str(exc)}, status=400)
    return JsonResponse({"ok": True, "message": "Hardcover catalog access is working."})


@login_required
def group_hardcover_disconnect(request, group_slug):
    group = get_object_or_404(ReadingGroup, slug=group_slug, is_active=True)
    if not can_manage_group(request.user, group):
        return HttpResponseForbidden("Group settings permission is required.")
    connection = get_object_or_404(HardcoverConnection, group=group)
    if request.method == "POST":
        connection.delete()
        AuditEvent.objects.create(actor=request.user, group=group, action="hardcover.disconnected", object_type="HardcoverConnection", summary="Removed the group's Hardcover catalog connection.")
        messages.success(request, "Hardcover was disconnected.")
        return redirect("group-edit", group_slug=group.slug)
    return render(request, "core/confirm_remove.html", {
        "eyebrow": "Hardcover",
        "title": "Disconnect Hardcover?",
        "description": "Catalog search will stop for this group. Existing submissions and cached book information will be preserved.",
        "cancel_url": reverse("group-edit", kwargs={"group_slug": group.slug}),
        "action_label": "Confirm Disconnect",
        "hide_reason": True,
    })


@login_required
def group_detail(request, group_slug):
    group = get_object_or_404(ReadingGroup, slug=group_slug, is_active=True)
    membership = membership_for(request.user, group)
    if not request.user.is_superuser and not membership:
        return HttpResponseForbidden("You are not a member of this reading group.")
    active_challenges = visible_challenges_for(
        request.user,
        group.challenge_months.exclude(status=ChallengeMonth.Status.ARCHIVED),
    )
    return render(request, "core/group_detail.html", {
        "group": group,
        "membership": membership,
        "can_manage_participants": can_manage_participants(request.user, group),
        "can_manage_months": can_manage_months(request.user, group),
        "can_edit_group": can_manage_group(request.user, group),
        "can_manage_group_announcements": can_manage_group_announcements(request.user, group),
        "can_view_access_code": can_manage_group(request.user, group) or can_view_access_code(request.user, group),
        "participant_count": group.memberships.filter(is_active=True, user__is_superuser=False).count(),
        "active_months": active_challenges,
        "active_month_count": active_challenges.count(),
    })


@login_required
def group_announcement_update(request, group_slug):
    group = get_object_or_404(ReadingGroup, slug=group_slug, is_active=True)
    if not can_manage_group_announcements(request.user, group):
        return HttpResponseForbidden("Group announcement management permission is required.")
    if request.method != "POST":
        return redirect("group-detail", group_slug=group.slug)
    if not group.announcement_enabled:
        messages.error(request, "Enable the group announcement from Edit Group before updating it here.")
        return redirect("group-detail", group_slug=group.slug)
    announcement = request.POST.get("announcement", "").strip()
    if not announcement:
        messages.error(request, "The enabled group announcement cannot be blank.")
        return redirect("group-detail", group_slug=group.slug)
    group.announcement = announcement
    group.save(update_fields=["announcement"])
    AuditEvent.objects.create(actor=request.user, group=group, action="group.announcement_updated", object_type="ReadingGroup", object_id=str(group.pk), summary="Updated the group announcement.")
    messages.success(request, "Group announcement updated.")
    return redirect("group-detail", group_slug=group.slug)


@login_required
def participant_list(request, group_slug):
    group = get_object_or_404(ReadingGroup, slug=group_slug, is_active=True)
    membership = membership_for(request.user, group)
    if not request.user.is_superuser and not membership:
        return HttpResponseForbidden("You are not a member of this reading group.")
    participants = list(group.memberships.filter(user__is_superuser=False).select_related("user"))
    participant_ids = [participant.pk for participant in participants]
    planning_by_participant = historical_reader_planning_data(
        group=group,
        participant_ids=participant_ids,
    )
    for participant in participants:
        participant.planning = planning_by_participant[participant.pk]
    return render(request, "core/participant_list.html", {"group": group, "participants": participants, "can_manage": can_manage_participants(request.user, group), "can_manage_permissions": can_manage_permissions(request.user, group), "can_remove": can_manage_participants(request.user, group)})


@login_required
def participant_detail(request, group_slug, pk):
    group = get_object_or_404(ReadingGroup, slug=group_slug, is_active=True)
    viewer_membership = membership_for(request.user, group)
    if not request.user.is_superuser and not viewer_membership:
        return HttpResponseForbidden("You are not a member of this reading group.")
    participant = get_object_or_404(Membership.objects.select_related("user"), pk=pk, group=group, user__is_superuser=False)
    approved_submissions = participant.submissions.filter(status=BookSubmission.Status.APPROVED, is_removed=False)
    totals = approved_submissions.aggregate(approved_books=Count("id"))
    participated_month_ids = set(participant.month_enrollments.filter(month__group=group).values_list("month_id", flat=True))
    participated_month_ids.update(participant.submissions.filter(month__group=group, is_removed=False).values_list("month_id", flat=True))
    months = list(group.challenge_months.filter(pk__in=participated_month_ids).annotate(
        participant_books=Count("submissions", filter=Q(submissions__participant=participant, submissions__status=BookSubmission.Status.APPROVED, submissions__is_removed=False), distinct=True),
    ))
    assignments = {
        assignment.month_id: assignment.team
        for assignment in TeamAssignment.objects.filter(
            month__group=group,
            participant=participant,
            ended_at__isnull=True,
        ).select_related("team")
    }
    for month in months:
        month.participant_team = assignments.get(month.pk)
        month.participant_pages = challenge_score_totals(
            month=month,
            participant_ids=[participant.pk],
        )[participant.pk]["total_pages"]
    detailed_access = request.user.is_superuser or participant.user_id == request.user.id
    profile = getattr(participant.user, "northbound_profile", None)
    can_view_discord_username = (
        detailed_access
        or viewer_membership.role in {Membership.Role.OWNER, Membership.Role.MODERATOR}
        or bool(profile and profile.discord_username_is_public)
    )
    return render(request, "core/participant_detail.html", {
        "group": group,
        "participant": participant,
        "approved_books": totals["approved_books"] or 0,
        "approved_pages": sum(month.participant_pages for month in months),
        "months_participated": len(months),
        "months": months if detailed_access else [],
        "detailed_access": detailed_access,
        "can_view_discord_username": can_view_discord_username,
    })


@login_required
def challenge_participant_detail(request, group_slug, month_pk, pk):
    month = get_object_or_404(ChallengeMonth.objects.select_related("group"), pk=month_pk, group__slug=group_slug)
    if not can_view_challenge(request.user, month):
        raise Http404("Challenge not found.")
    enrollment = get_object_or_404(
        MonthEnrollment.objects.select_related(
            "participant__user", "participant__user__northbound_profile"
        ).prefetch_related("signup_answers__question"),
        month=month, participant_id=pk,
    )
    participant = enrollment.participant
    current_assignment = TeamAssignment.objects.filter(
        month=month, participant=participant, ended_at__isnull=True
    ).select_related("team").first()
    team = current_assignment.team if current_assignment else None
    scope = challenge_review_scope(request.user, month)
    is_self = participant.user_id == request.user.id
    in_review_scope = bool(scope and (
        scope[0] == "challenge" or (team and team.pk in scope[1])
    ))
    if not (in_review_scope or is_self):
        return HttpResponseForbidden("You may view only Readers in your Challenge review scope.")

    score_access = can_view_reader_scores(request.user, month, team=team, reader=participant)
    registration_access = can_view_challenge_registration_answers(request.user, month)
    planning = (
        historical_reader_planning_data(month=month, participant_ids=[participant.pk])[participant.pk]
        if registration_access else None
    )
    profile = getattr(participant.user, "northbound_profile", None)
    viewer_membership = membership_for(request.user, month.group)
    discord_access = (
        request.user.is_superuser or is_self or can_operate_challenge(request.user, month)
        or bool(viewer_membership and viewer_membership.role in {Membership.Role.OWNER, Membership.Role.MODERATOR})
        or bool(profile and profile.discord_username_is_public)
    )
    staff_roles = list(ChallengeStaffAssignment.objects.filter(
        month=month, membership=participant, ended_at__isnull=True
    ).values_list("role", flat=True))
    role_labels = dict(ChallengeStaffAssignment.Role.choices)
    challenge_role = ", ".join(role_labels[role] for role in staff_roles) if staff_roles else "Reader"
    score = challenge_score_totals(month=month, participant_ids=[participant.pk])[participant.pk] if score_access else None

    registration_rows = []
    if registration_access:
        answer_map = {answer.question_id: answer.value for answer in enrollment.signup_answers.all()}
        for question in month.signup_questions.all():
            value = answer_map.get(question.pk, "")
            registration_rows.append({
                "question": question,
                "value": ", ".join(value) if isinstance(value, list) else value,
            })

    personal_tbr = PersonalTBR.objects.filter(enrollment=enrollment, confirmed_at__isnull=False).first()
    tbr_presentation = build_personal_tbr_reader_presentation(personal_tbr=personal_tbr) if personal_tbr else None
    botm_books = list(month.botm_books.filter(is_retired=False).select_related(
        "catalog_book", "catalog_edition"
    ).order_by("position", "pk"))
    botm_presentation = build_botm_reader_presentation(month=month, participant=participant, books=botm_books)

    themes = list(month.themes.filter(is_active=True).order_by("starts_on", "name"))
    claims_by_theme = {}
    for claim in ThemeClaim.objects.filter(
        theme__in=themes, submission__month=month, submission__participant=participant,
        submission__is_removed=False,
    ).select_related("submission", "theme").order_by("theme_id", "-submission__submitted_at"):
        claims_by_theme.setdefault(claim.theme_id, claim)
    for theme in themes:
        theme.reader_claim = claims_by_theme.get(theme.pk)
    theme_completed_count = sum(
        theme.reader_claim is not None and theme.reader_claim.status == ThemeClaim.Status.APPROVED
        for theme in themes
    )
    submissions = list(BookSubmission.objects.filter(
        month=month, participant=participant, is_removed=False
    ).select_related("catalog_book", "catalog_edition").order_by("-completed_on", "-pk"))
    provenance = list(ModifierProvenance.objects.filter(
        month=month, participant=participant, is_active=True
    ).select_related("submission").order_by("effective_date", "pk")) if score_access else []

    return render(request, "core/challenge_participant_detail.html", {
        "month": month, "participant": participant, "enrollment": enrollment,
        "team": team, "challenge_role": challenge_role,
        "can_view_discord_username": discord_access,
        "can_view_reader_scores": score_access, "score": score,
        "can_view_registration_answers": registration_access,
        "planning": planning, "registration_rows": registration_rows,
        "personal_tbr_reader": tbr_presentation,
        "botm_reader": botm_presentation, "botm_books": botm_books,
        "themes": themes, "theme_completed_count": theme_completed_count,
        "submissions": submissions, "provenance": provenance,
    })


@login_required
def participant_role_edit(request, group_slug, pk):
    group = get_object_or_404(ReadingGroup, slug=group_slug)
    if not can_manage_permissions(request.user, group):
        return HttpResponseForbidden("Group permission management is required.")
    participant = get_object_or_404(Membership, pk=pk, group=group, user__is_superuser=False)
    if participant.user_id == request.user.id and not request.user.is_superuser:
        messages.error(request, "Group owners cannot change their own role. Another group owner or Platform Owner must do that.")
        return redirect("participant-list", group_slug=group.slug)
    form = MembershipRoleForm(request.POST or None, instance=participant)
    if request.method == "POST" and form.is_valid():
        previous_role = participant.role
        updated = form.save()
        AuditEvent.objects.create(actor=request.user, group=group, action="membership.role_changed", object_type="Membership", object_id=str(updated.pk), summary=f"Changed {updated.display_name} from {previous_role} to {updated.role}; active={updated.is_active}")
        messages.success(request, f"Updated {updated.display_name}'s access.")
        return redirect("participant-list", group_slug=group.slug)
    return render(request, "core/form_page.html", {"form": form, "title": f"Adjust Role: {participant.display_name}", "eyebrow": "Platform Owner Control"})


@login_required
def participant_permissions_edit(request, group_slug, pk):
    group = get_object_or_404(ReadingGroup, slug=group_slug)
    if not can_manage_permissions(request.user, group):
        return HttpResponseForbidden("Group owner or platform root access is required.")
    participant = get_object_or_404(Membership.objects.select_related("user"), pk=pk, group=group, user__is_superuser=False)
    if participant.user_id == request.user.id and not request.user.is_superuser:
        messages.error(request, "You cannot change your own permission set.")
        return redirect("participant-list", group_slug=group.slug)
    previous_role = participant.role
    previous_overrides = participant.permission_overrides.copy()
    form = MembershipPermissionsForm(request.POST or None, membership=participant)
    if request.method == "POST" and form.is_valid():
        updated = form.save()
        AuditEvent.objects.create(
            actor=request.user,
            group=group,
            action="membership.permissions_changed",
            object_type="Membership",
            object_id=str(updated.pk),
            summary=f"Changed {updated.display_name} permissions; role {previous_role} to {updated.role}; overrides {previous_overrides} to {updated.permission_overrides}.",
        )
        messages.success(request, f"Updated {updated.display_name}'s permissions.")
        return redirect("participant-list", group_slug=group.slug)
    return render(request, "core/permissions_edit.html", {"form": form, "group": group, "participant": participant})


@login_required
def month_list(request, group_slug):
    group = get_object_or_404(ReadingGroup, slug=group_slug, is_active=True)
    membership = membership_for(request.user, group)
    if not request.user.is_superuser and not membership:
        return HttpResponseForbidden("You are not a member of this reading group.")
    viewing_archive = request.GET.get("archive") == "1"
    if viewing_archive:
        months = group.challenge_months.filter(status=ChallengeMonth.Status.ARCHIVED)
    else:
        months = group.challenge_months.exclude(status=ChallengeMonth.Status.ARCHIVED)
    months = visible_challenges_for(request.user, months)
    months = list(months.annotate(
        approved_books=Count("submissions", filter=Q(submissions__status=BookSubmission.Status.APPROVED, submissions__is_removed=False), distinct=True),
    ))
    for month in months:
        month.approved_pages = sum(
            score["total_pages"] for score in challenge_score_totals(month=month).values()
        )
    return render(request, "core/month_list.html", {"group": group, "months": months, "can_manage": can_manage_months(request.user, group), "viewing_archive": viewing_archive, "archived_count": group.challenge_months.filter(status=ChallengeMonth.Status.ARCHIVED).count()})


@login_required
def team_list(request, group_slug, month_pk):
    month = get_object_or_404(ChallengeMonth.objects.select_related("group"), pk=month_pk, group__slug=group_slug)
    group = month.group
    membership = membership_for(request.user, month.group)
    if not request.user.is_superuser and not membership:
        return HttpResponseForbidden("You are not a member of this reading group.")
    if not can_view_challenge(request.user, month):
        raise Http404("Challenge not found.")
    viewing_archive = request.GET.get("archive") == "1"
    leader_assignments = ChallengeStaffAssignment.objects.filter(
        role=ChallengeStaffAssignment.Role.TEAM_LEADER,
        ended_at__isnull=True,
    ).select_related("membership")
    teams = list(
        month.teams.filter(is_archived=viewing_archive).prefetch_related(
            Prefetch(
                "assignments",
                queryset=TeamAssignment.objects.filter(
                    ended_at__isnull=True,
                    participant__month_enrollments__month=month,
                    participant__month_enrollments__is_active=True,
                ).select_related("participant"),
            ),
            Prefetch("staff_assignments", queryset=leader_assignments, to_attr="current_leader_assignments"),
        )
    )
    _annotate_team_leader_rosters(teams)
    participant_ids = {
        assignment.participant_id
        for team in teams
        for assignment in team.assignments.all()
    }
    score_by_participant = challenge_score_totals(month=month, participant_ids=participant_ids)
    for team in teams:
        team.can_view_standings = can_view_team_standings(request.user, month, team=team)
        team.score_total = sum(
            score_by_participant[assignment.participant_id]["total_pages"]
            for assignment in team.assignments.all()
        )
    mutable = month_is_configurable(month)
    host_access = can_operate_challenge(request.user, month)
    return render(request, "core/team_list.html", {"group": group, "month": month, "teams": teams, "can_manage": mutable and host_access, "can_remove": mutable and host_access, "can_manage_leaders": not viewing_archive and host_access, "viewing_archive": viewing_archive, "archived_count": month.teams.filter(is_archived=True).count()})


@login_required
def team_detail(request, group_slug, month_pk, pk):
    team = get_object_or_404(
        Team.objects.select_related("month__group"),
        pk=pk,
        month_id=month_pk,
        month__group__slug=group_slug,
    )
    month = team.month
    membership = membership_for(request.user, month.group)
    if not request.user.is_superuser and not membership:
        return HttpResponseForbidden("You are not a member of this reading group.")
    if not can_view_challenge(request.user, month):
        raise Http404("Challenge not found.")

    current_leaders = list(team.staff_assignments.filter(
        role=ChallengeStaffAssignment.Role.TEAM_LEADER, ended_at__isnull=True,
    ).select_related("membership").order_by("membership__display_name", "membership_id"))
    leader_ids = {assignment.membership_id for assignment in current_leaders}
    full_roster = list(team.assignments.filter(
        ended_at__isnull=True,
        participant__month_enrollments__month=month,
        participant__month_enrollments__is_active=True,
    ).select_related("participant", "participant__user", "participant__user__northbound_profile").distinct())
    participant_ids = [assignment.participant_id for assignment in full_roster]
    planning_access = can_view_challenge_registration_answers(request.user, month)
    standings_access = can_view_team_standings(request.user, month, team=team)
    score_access = can_view_reader_scores(request.user, month, team=team)
    review_scope = challenge_review_scope(request.user, month)
    progress_access = bool(review_scope and (review_scope[0] == "challenge" or team.pk in review_scope[1]))
    discord_access = (
        request.user.is_superuser or can_operate_challenge(request.user, month)
        or bool(membership and membership.role in {Membership.Role.OWNER, Membership.Role.MODERATOR})
    )

    filter_errors = []
    default_from, default_to = month.starts_on, month.ends_on

    def parsed_date(name, default):
        raw = request.GET.get(name, "").strip()
        if not raw:
            return default
        try:
            value = date.fromisoformat(raw)
        except ValueError:
            filter_errors.append(f"Enter a valid {name.title()} Date.")
            return default
        if default_from and value < default_from or default_to and value > default_to:
            filter_errors.append(f"{name.title()} Date must fall within the Challenge schedule.")
            return default
        return value

    effective_from = parsed_date("from", default_from) if score_access else default_from
    effective_to = parsed_date("to", default_to) if score_access else default_to
    if effective_from and effective_to and effective_from > effective_to:
        filter_errors.append("From Date cannot be after To Date.")
        effective_from, effective_to = default_from, default_to
    show_bonuses = request.GET.get("bonuses", "1") != "0" if score_access else False

    allowed_progress = {"any", "complete", "incomplete"}
    theme_filter = request.GET.get("themes", "any") if progress_access else "any"
    tbr_filter = request.GET.get("tbr", "any") if progress_access else "any"
    botm_filter = request.GET.get("botm", "any") if progress_access else "any"
    if theme_filter not in allowed_progress: theme_filter = "any"
    if botm_filter not in allowed_progress: botm_filter = "any"
    if tbr_filter not in allowed_progress | {"none"}: tbr_filter = "any"

    show_discord = discord_access and request.GET.get("show_discord") == "1"
    show_average = planning_access and request.GET.get("show_average") == "1"
    show_last = planning_access and request.GET.get("show_last") == "1"
    show_completed = planning_access and request.GET.get("show_completed") == "1"
    planning_by_participant = historical_reader_planning_data(
        month=month, participant_ids=participant_ids,
    ) if planning_access else {}
    score_by_participant = challenge_score_totals(
        month=month, participant_ids=participant_ids,
        effective_from=effective_from, effective_to=effective_to,
    ) if (score_access or standings_access) else {}

    theme_count = month.themes.filter(is_active=True).count() if progress_access else 0
    approved_theme_ids = set(ThemeClaim.objects.filter(
        submission__month=month, submission__participant_id__in=participant_ids,
        submission__is_removed=False, status=ThemeClaim.Status.APPROVED,
        theme__is_active=True,
    ).values_list("submission__participant_id", "theme_id")) if progress_access else set()
    tbrs = {tbr.enrollment.participant_id: tbr for tbr in PersonalTBR.objects.filter(
        enrollment__month=month, enrollment__participant_id__in=participant_ids,
        confirmed_at__isnull=False,
    ).select_related("enrollment").prefetch_related("books")} if progress_access else {}
    tbr_completed = set(PersonalTBRMatch.objects.filter(
        month=month, participant_id__in=participant_ids,
        status=PersonalTBRMatch.Status.CONFIRMED, is_qualifying=True,
    ).values_list("participant_id", "personal_tbr_book_id")) if progress_access else set()
    botm_count = month.botm_books.filter(is_retired=False).count() if progress_access else 0
    botm_completed = set(BotmMatch.objects.filter(
        month=month, participant_id__in=participant_ids, botm_book__is_retired=False,
        status=BotmMatch.Status.CONFIRMED, is_qualifying=True,
    ).values_list("participant_id", "botm_book_id")) if progress_access else set()

    for assignment in full_roster:
        participant_id = assignment.participant_id
        assignment.is_team_leader = participant_id in leader_ids
        assignment.planning = planning_by_participant.get(participant_id)
        if score_access:
            scores = score_by_participant.get(participant_id, {})
            assignment.base_pages = scores.get("base_pages") or 0
            assignment.modifier_pages = scores.get("modifier_pages") or 0
            assignment.total_pages = assignment.base_pages + (assignment.modifier_pages if show_bonuses else 0)
        assignment.theme_total = theme_count
        assignment.theme_completed = len({theme_id for reader_id, theme_id in approved_theme_ids if reader_id == participant_id})
        personal_tbr = tbrs.get(participant_id)
        assignment.tbr_total = personal_tbr.books.count() if personal_tbr else None
        assignment.tbr_completed = len({book_id for reader_id, book_id in tbr_completed if reader_id == participant_id}) if personal_tbr else None
        assignment.botm_total = botm_count
        assignment.botm_completed = len({book_id for reader_id, book_id in botm_completed if reader_id == participant_id})

    def progress_matches(completed, total, selected, *, missing=False):
        if selected == "any": return True
        if selected == "none": return missing
        if missing or not total: return False
        return (completed == total) if selected == "complete" else (completed < total)

    roster = []
    for assignment in full_roster:
        if not progress_matches(assignment.theme_completed, assignment.theme_total, theme_filter): continue
        if not progress_matches(assignment.tbr_completed or 0, assignment.tbr_total or 0, tbr_filter, missing=assignment.tbr_total is None): continue
        if not progress_matches(assignment.botm_completed, assignment.botm_total, botm_filter): continue
        roster.append(assignment)

    allowed_sorts = {"reader"}
    if score_access:
        allowed_sorts.update({"base", "total"})
        if show_bonuses: allowed_sorts.add("modifier")
    if progress_access: allowed_sorts.update({"themes", "tbr", "botm"})
    sort_key = request.GET.get("sort", "reader")
    sort_key = sort_key if sort_key in allowed_sorts else "reader"
    direction = request.GET.get("direction", "asc") if request.GET.get("direction") in {"asc", "desc"} else "asc"

    def sort_value(assignment):
        if sort_key == "base": return assignment.base_pages
        if sort_key == "modifier": return assignment.modifier_pages
        if sort_key == "total": return assignment.total_pages
        if sort_key == "themes": return assignment.theme_completed
        if sort_key == "tbr": return assignment.tbr_completed
        if sort_key == "botm": return assignment.botm_completed
        return assignment.participant.display_name.casefold()

    def sorted_role_group(assignments):
        if sort_key == "reader":
            return sorted(assignments, key=lambda item: (item.participant.display_name.casefold(), item.participant_id), reverse=direction == "desc")
        available = [item for item in assignments if sort_value(item) is not None]
        unavailable = [item for item in assignments if sort_value(item) is None]
        available.sort(key=lambda item: (item.participant.display_name.casefold(), item.participant_id))
        available.sort(key=sort_value, reverse=direction == "desc")
        unavailable.sort(key=lambda item: (item.participant.display_name.casefold(), item.participant_id))
        return available + unavailable

    roster = sorted_role_group([item for item in roster if item.is_team_leader]) + sorted_role_group([item for item in roster if not item.is_team_leader])
    sort_labels = {"reader": "Reader", "base": "Base", "modifier": "Modifier", "total": "Total", "themes": "Themes", "tbr": "TBR", "botm": "BOTM"}
    sort_options = [{"value": key, "label": label} for key, label in sort_labels.items() if key in allowed_sorts]
    active_filters = any((
        effective_from != default_from, effective_to != default_to, not show_bonuses,
        theme_filter != "any", tbr_filter != "any", botm_filter != "any",
        show_discord, show_average, show_last, show_completed,
    ))
    date_range_active = effective_from != default_from or effective_to != default_to

    host_access = can_operate_challenge(request.user, month)
    mutable = month_is_configurable(month)
    return render(request, "core/team_detail.html", {
        "group": month.group,
        "month": month,
        "team": team,
        "current_leaders": current_leaders,
        "roster": roster,
        "team_total": (sum(item.total_pages for item in roster) if score_access else sum(score["total_pages"] for score in score_by_participant.values())) if standings_access else None,
        "team_base_total": sum(assignment.base_pages for assignment in roster) if score_access else None,
        "can_view_team_standings": standings_access,
        "can_view_reader_scores": score_access,
        "can_view_planning_data": planning_access,
        "can_filter_progress": progress_access,
        "can_filter_discord": discord_access,
        "full_roster_count": len(full_roster),
        "show_bonuses": show_bonuses,
        "effective_from": effective_from,
        "effective_to": effective_to,
        "filter_errors": filter_errors,
        "theme_filter": theme_filter,
        "tbr_filter": tbr_filter,
        "botm_filter": botm_filter,
        "filters_active": active_filters,
        "date_range_active": date_range_active,
        "show_discord": show_discord,
        "show_average": show_average,
        "show_last": show_last,
        "show_completed": show_completed,
        "sort_key": sort_key,
        "sort_direction": direction,
        "sort_options": sort_options,
        "can_edit_team": mutable and host_access,
        "can_manage_leaders": not team.is_archived and host_access,
        "can_remove_roster": mutable and host_access,
    })


def _annotate_team_leader_rosters(teams):
    for team in teams:
        leader_ids = {assignment.membership_id for assignment in team.current_leader_assignments}
        for roster_assignment in team.assignments.all():
            roster_assignment.is_team_leader = roster_assignment.participant_id in leader_ids


@login_required
def team_stats_settings(request, group_slug, month_pk):
    return redirect("challenge-visibility-settings", group_slug=group_slug, month_pk=month_pk)


@login_required
def challenge_visibility_settings(request, group_slug, month_pk):
    month = get_object_or_404(ChallengeMonth.objects.select_related("group"), pk=month_pk, group__slug=group_slug)
    if not can_configure_competition_visibility(request.user, month):
        return HttpResponseForbidden("Challenge visibility configuration authority is required.")
    if reject_locked_month(request, month, "change its visibility"):
        return redirect("challenge-settings", group_slug=group_slug, month_pk=month.pk)
    form = CompetitionVisibilityForm(request.POST or None, instance=month)
    if request.method == "POST" and form.is_valid():
        previous = (month.team_standings_visibility, month.reader_scores_visibility)
        updated = form.save()
        AuditEvent.objects.create(
            actor=request.user,
            group=month.group,
            action="challenge.competition_visibility_changed",
            object_type="ChallengeMonth",
            object_id=str(month.pk),
            summary=(
                f"Changed competition visibility for {month.name}: team standings "
                f"{previous[0]} to {updated.team_standings_visibility}; reader scores "
                f"{previous[1]} to {updated.reader_scores_visibility}."
            ),
        )
        messages.success(request, "Competition visibility updated.")
        return redirect("challenge-settings", group_slug=group_slug, month_pk=month.pk)
    return render(request, "core/challenge_visibility_settings.html", {"form": form, "month": month})


@login_required
def participant_deactivate(request, group_slug, pk):
    group = get_object_or_404(ReadingGroup, slug=group_slug)
    if not can_manage_participants(request.user, group):
        return HttpResponseForbidden("Group membership management permission is required.")
    participant = get_object_or_404(Membership, pk=pk, group=group, user__is_superuser=False)
    if participant.user_id == request.user.id:
        messages.error(request, "You cannot remove your own group membership.")
        return redirect("participant-list", group_slug=group.slug)
    if request.method == "POST":
        reason = request.POST.get("reason", "").strip()
        participant.is_active = False
        participant.save(update_fields=["is_active"])
        AuditEvent.objects.create(actor=request.user, group=group, action="membership.deactivated", object_type="Membership", object_id=str(participant.pk), summary=f"Deactivated {participant.display_name}. Reason: {reason or 'Not provided'}")
        messages.success(request, f"{participant.display_name} was removed from active participation. Historical records were preserved.")
        return redirect("participant-list", group_slug=group.slug)
    return render(request, "core/confirm_remove.html", {"title": f"Remove {participant.display_name}?", "description": "This deactivates the participant while preserving historical teams, books, and statistics.", "cancel_url": reverse("participant-list", kwargs={"group_slug": group.slug})})


@login_required
def team_assignment_remove(request, group_slug, month_pk, pk):
    assignment = get_object_or_404(
        TeamAssignment.objects.select_related("month__group", "participant", "team"),
        pk=pk,
        month_id=month_pk,
        month__group__slug=group_slug,
        ended_at__isnull=True,
    )
    if not can_operate_challenge(request.user, assignment.month):
        return HttpResponseForbidden("A current Host assignment is required.")
    if reject_locked_month(request, assignment.month, "change its team roster"):
        return redirect(assignment.month)
    if request.method == "POST":
        reason = request.POST.get("reason", "").strip()
        group = assignment.month.group
        month = assignment.month
        end_team_assignment(
            assignment=assignment,
            actor=request.user,
            reason=reason or "staff removed the current team assignment",
        )
        messages.success(request, "Participant removed from the team. Their reading history was not deleted.")
        return redirect("team-list", group_slug=group.slug, month_pk=month.pk)
    return render(request, "core/confirm_remove.html", {"title": f"Remove {assignment.participant.display_name} from {assignment.team.name}?", "description": "The reader remains in the participant database and can be assigned to another team.", "cancel_url": reverse("team-list", kwargs={"group_slug": group_slug, "month_pk": month_pk})})


@login_required
def submission_remove(request, group_slug, month_pk, pk):
    submission = get_object_or_404(BookSubmission.objects.select_related("month__group", "participant"), pk=pk, month_id=month_pk, month__group__slug=group_slug, is_removed=False)
    if not can_operate_challenge(request.user, submission.month):
        return HttpResponseForbidden("A current Host assignment is required.")
    if reject_locked_month(request, submission.month, "remove a submission"):
        return redirect(submission.month)
    if request.method == "POST":
        submission.is_removed = True
        submission.removed_at = timezone.now()
        submission.removed_by = request.user
        submission.removal_reason = request.POST.get("reason", "").strip()
        submission.save(update_fields=["is_removed", "removed_at", "removed_by", "removal_reason"])
        synchronize_submission(submission)
        synchronize_personal_tbr_submission(submission)
        AuditEvent.objects.create(actor=request.user, group=submission.month.group, action="submission.removed", object_type="BookSubmission", object_id=str(submission.pk), summary=f"Removed {submission.title} by {submission.participant.display_name}. Reason: {submission.removal_reason or 'Not provided'}")
        messages.success(request, "Book entry removed from active totals. Its audit record was preserved.")
        return redirect(submission.month)
    return render(request, "core/confirm_remove.html", {"title": f"Remove {submission.title}?", "description": "The entry will disappear from active totals but remain recoverable in the database audit history.", "cancel_url": submission.month.get_absolute_url()})


@login_required
def month_create(request, group_slug):
    group = get_object_or_404(ReadingGroup, slug=group_slug)
    if not can_manage_months(request.user, group):
        return HttpResponseForbidden("Month management permission is required.")
    form = ChallengeCreateForm(request.POST or None, group=group)
    if request.method == "POST" and form.is_valid():
        month, assignments = form.save(created_by=request.user)
        AuditEvent.objects.create(
            actor=request.user,
            group=group,
            action="month.created",
            object_type="ChallengeMonth",
            object_id=str(month.pk),
            summary=f"Created {month.name} in Draft and assigned {len(assignments)} Host(s).",
        )
        for assignment in assignments:
            AuditEvent.objects.create(
                actor=request.user,
                group=group,
                action="challenge.host_assigned",
                object_type="ChallengeStaffAssignment",
                object_id=str(assignment.pk),
                summary=f"Assigned {assignment.membership.display_name} as a Host for {month.name}.",
            )
        messages.success(request, f"{month.name} was created in Draft and handed off to its selected Host(s).")
        return redirect("challenge-settings", group_slug=group.slug, month_pk=month.pk)
    return render(request, "core/challenge_create.html", {
        "form": form,
        "title": "Create Challenge",
        "eyebrow": group.name,
        "form_note": "Create the Draft and assign the Hosts who will configure and operate it.",
        "eligible_hosts": form.fields["hosts"].queryset,
    })


@login_required
def month_edit(request, group_slug, pk):
    month = get_object_or_404(ChallengeMonth.objects.select_related("group"), pk=pk, group__slug=group_slug)
    if not can_manage_months(request.user, month.group):
        return HttpResponseForbidden("Month management permission is required.")
    with timezone.override(ZoneInfo(month.group.timezone)):
        form = ChallengeMonthForm(request.POST or None, instance=month)
        if request.method == "POST" and form.is_valid():
            updated = form.save()
            AuditEvent.objects.create(actor=request.user, group=month.group, action="month.updated", object_type="ChallengeMonth", object_id=str(month.pk), summary=f"Updated Challenge identity and schedule for {month.name}.")
            messages.success(request, "Challenge updated.")
            return redirect(updated)
        return render(request, "core/month_edit.html", {"form": form, "month": month, "can_delete_draft": can_manage_months(request.user, month.group) and month.status == ChallengeMonth.Status.DRAFT})


@login_required
def challenge_general_settings(request, group_slug, month_pk):
    month = get_object_or_404(ChallengeMonth.objects.select_related("group"), pk=month_pk, group__slug=group_slug)
    if not (can_manage_months(request.user, month.group) or can_operate_challenge(request.user, month)):
        return HttpResponseForbidden("Challenge configuration authority is required.")
    form = ChallengeGeneralSettingsForm(request.POST or None, instance=month)
    if request.method == "POST" and form.is_valid():
        updated = form.save()
        AuditEvent.objects.create(
            actor=request.user,
            group=month.group,
            action="month.updated",
            object_type="ChallengeMonth",
            object_id=str(month.pk),
            summary=f"Updated general Challenge settings for {updated.name}.",
        )
        messages.success(request, "General settings updated.")
        return redirect("challenge-settings", group_slug=group_slug, month_pk=month.pk)
    return render(request, "core/challenge_general_settings.html", {"month": month, "form": form})


@login_required
def challenge_schedule_settings(request, group_slug, month_pk):
    month = get_object_or_404(ChallengeMonth.objects.select_related("group"), pk=month_pk, group__slug=group_slug)
    if not (can_manage_months(request.user, month.group) or can_operate_challenge(request.user, month)):
        return HttpResponseForbidden("Challenge configuration authority is required.")
    with timezone.override(ZoneInfo(month.group.timezone)):
        form = ChallengeScheduleForm(request.POST or None, instance=month)
        if request.method == "POST" and form.is_valid():
            form.save()
            AuditEvent.objects.create(
                actor=request.user,
                group=month.group,
                action="month.updated",
                object_type="ChallengeMonth",
                object_id=str(month.pk),
                summary=f"Updated Challenge schedule for {month.name}.",
            )
            messages.success(request, "Challenge schedule updated.")
            return redirect("challenge-settings", group_slug=group_slug, month_pk=month.pk)
        return render(request, "core/challenge_schedule_settings.html", {"month": month, "form": form})


@login_required
def challenge_settings(request, group_slug, month_pk):
    month = get_object_or_404(ChallengeMonth.objects.select_related("group"), pk=month_pk, group__slug=group_slug)
    can_edit_general = can_manage_months(request.user, month.group) or can_operate_challenge(request.user, month)
    can_configure_registration = can_configure_challenge_registration(request.user, month)
    can_configure_visibility = can_configure_competition_visibility(request.user, month)
    can_configure_announcement = month_is_configurable(month) and can_manage_challenge_announcements(request.user, month)
    can_transition_lifecycle = can_transition_challenge(request.user, month)
    can_configure_games = month_is_configurable(month) and can_operate_challenge(request.user, month)
    can_configure_botm = month_is_configurable(month) and can_operate_challenge(request.user, month)
    can_manage_hosts = can_manage_challenge_hosts(request.user, month.group)
    can_manage_operations = month_is_configurable(month) and can_operate_challenge(request.user, month)
    if not (can_edit_general or can_configure_registration or can_configure_visibility or can_configure_announcement or can_transition_lifecycle):
        return HttpResponseForbidden("Challenge settings authority is required.")
    return render(request, "core/challenge_settings.html", {
        "month": month,
        "can_edit_general": can_edit_general,
        "can_configure_registration": can_configure_registration,
        "can_configure_checkpoints": can_configure_registration,
        "can_configure_visibility": can_configure_visibility,
        "can_configure_announcement": can_configure_announcement,
        "can_transition_lifecycle": can_transition_lifecycle,
        "can_configure_games": can_configure_games,
        "can_configure_botm": can_configure_botm,
        "can_manage_hosts": can_manage_hosts,
        "can_manage_operations": can_manage_operations,
        "lifecycle_targets": lifecycle_transition_targets(month),
        "signup_question_count": month.signup_questions.count(),
        "checkpoint_count": month.progress_checkpoints.count(),
    })


@login_required
def challenge_games_settings(request, group_slug, month_pk):
    month = get_object_or_404(ChallengeMonth.objects.select_related("group"), pk=month_pk, group__slug=group_slug)
    if not can_operate_challenge(request.user, month):
        return HttpResponseForbidden("Only a current Host or Platform Owner may configure Challenge Games.")
    if reject_locked_month(request, month, "change Games availability"):
        return redirect("challenge-settings", group_slug=group_slug, month_pk=month.pk)
    form = ChallengeGamesSettingsForm(request.POST or None, instance=month)
    if request.method == "POST" and form.is_valid():
        changed = "games_enabled" in form.changed_data
        form.save()
        if changed:
            AuditEvent.objects.create(
                actor=request.user,
                group=month.group,
                action="challenge.games_setting_updated",
                object_type="ChallengeMonth",
                object_id=str(month.pk),
                summary=f"{'Enabled' if month.games_enabled else 'Disabled'} Games for {month.name}.",
            )
        messages.success(request, "Games setting updated.")
        return redirect("challenge-settings", group_slug=group_slug, month_pk=month.pk)
    return render(request, "core/challenge_games_settings.html", {"month": month, "form": form})


@login_required
def challenge_botm_settings(request, group_slug, month_pk):
    month = get_object_or_404(ChallengeMonth.objects.select_related("group"), pk=month_pk, group__slug=group_slug)
    if not can_operate_challenge(request.user, month):
        return HttpResponseForbidden("Only a current Host or Platform Owner may configure Book of the Month.")
    if reject_locked_month(request, month, "change Book of the Month settings"):
        return redirect("challenge-settings", group_slug=group_slug, month_pk=month.pk)
    form = ChallengeBotmSettingsForm(request.POST or None, instance=month)
    if request.method == "POST" and form.is_valid():
        changed = list(form.changed_data)
        form.save()
        synchronize_challenge(month)
        if changed:
            AuditEvent.objects.create(
                actor=request.user, group=month.group, action="botm.settings_updated",
                object_type="ChallengeMonth", object_id=str(month.pk),
                summary=f"Updated BOTM settings for {month.name}: {', '.join(changed)}.",
            )
        messages.success(request, "Book of the Month settings updated.")
        return redirect("challenge-settings", group_slug=group_slug, month_pk=month.pk)
    return render(request, "core/challenge_botm_settings.html", {"month": month, "form": form})


@login_required
def challenge_signup_settings(request, group_slug, month_pk):
    month = get_object_or_404(ChallengeMonth.objects.select_related("group"), pk=month_pk, group__slug=group_slug)
    if not can_configure_challenge_registration(request.user, month):
        return HttpResponseForbidden("Challenge registration configuration authority is required.")
    questions = list(month.signup_questions.all())
    schema_locked = month.signup_schema_is_locked
    lifecycle_locked = not month_is_configurable(month)
    tbr_settings_form = ChallengeTbrSettingsForm(
        request.POST if request.method == "POST" and request.POST.get("action") == "save_tbr_settings" else None,
        instance=month,
        prefix="tbr",
    )
    if request.method == "POST" and request.POST.get("action") == "save_tbr_settings":
        if lifecycle_locked:
            messages.error(request, f"{month.get_status_display()} Challenges are read-only.")
            return redirect("challenge-signup-settings", group_slug=group_slug, month_pk=month.pk)
        if tbr_settings_form.is_valid():
            changed = list(tbr_settings_form.changed_data)
            tbr_settings_form.save()
            from .personal_tbr_rewards import synchronize_personal_tbr_book_rewards_for_challenge
            synchronize_personal_tbr_book_rewards_for_challenge(month)
            from .personal_tbr_completion import synchronize_personal_tbr_completions_for_challenge
            synchronize_personal_tbr_completions_for_challenge(month)
            if changed:
                AuditEvent.objects.create(
                    actor=request.user,
                    group=month.group,
                    action="challenge.tbr_settings_updated",
                    object_type="ChallengeMonth",
                    object_id=str(month.pk),
                    summary=f"Updated Personal TBR settings for {month.name}: {', '.join(changed)}.",
                )
            messages.success(request, "Personal TBR settings updated.")
            return redirect("challenge-signup-settings", group_slug=group_slug, month_pk=month.pk)
    if schema_locked or lifecycle_locked:
        return render(request, "core/challenge_signup_settings.html", {
            "month": month,
            "questions": questions,
            "schema_locked": schema_locked,
            "lifecycle_locked": lifecycle_locked,
            "configuration_read_only": True,
            "tbr_settings_form": tbr_settings_form,
        })
    initial = [
        {
            "wording": question.wording,
            "question_type": question.question_type,
            "is_required": question.is_required,
            "choices_text": "\n".join(question.choices),
            "ORDER": question.position,
        }
        for question in questions
    ]
    registration_post = request.POST if request.method == "POST" and request.POST.get("action") != "save_tbr_settings" else None
    settings_form = ChallengeRegistrationSettingsForm(registration_post, instance=month, prefix="settings")
    question_formset = ChallengeSignupQuestionFormSet(
        registration_post,
        initial=initial,
        prefix="questions",
    )
    if request.method == "POST" and request.POST.get("action") != "save_tbr_settings" and settings_form.is_valid() and question_formset.is_valid():
        with transaction.atomic():
            locked_month = ChallengeMonth.objects.select_for_update().get(pk=month.pk)
            if locked_month.enrollments.exists():
                messages.error(request, "Signup questions were locked because a registration now exists.")
                return redirect("challenge-signup-settings", group_slug=group_slug, month_pk=month.pk)
            locked_month.registration_answer_editing_policy = settings_form.cleaned_data["registration_answer_editing_policy"]
            locked_month.registration_answer_editing_hours = settings_form.cleaned_data["registration_answer_editing_hours"]
            locked_month.save(update_fields=["registration_answer_editing_policy", "registration_answer_editing_hours"])
            active_forms = [
                form for form in question_formset.forms
                if form.cleaned_data and not form.cleaned_data.get("DELETE")
            ]
            active_forms.sort(key=lambda form: form.cleaned_data.get("ORDER") or 0)
            locked_month.signup_questions.all().delete()
            for position, question_form in enumerate(active_forms, start=1):
                ChallengeSignupQuestion.objects.create(
                    month=locked_month,
                    wording=question_form.cleaned_data["wording"],
                    question_type=question_form.cleaned_data["question_type"],
                    is_required=question_form.cleaned_data["is_required"],
                    choices=question_form.cleaned_data["choices"],
                    position=position,
                )
            AuditEvent.objects.create(
                actor=request.user,
                group=month.group,
                action="challenge.registration_schema_updated",
                object_type="ChallengeMonth",
                object_id=str(month.pk),
                summary=f"Updated registration policy and {len(active_forms)} signup question(s) for {month.name}.",
            )
        messages.success(request, "Challenge registration settings updated.")
        return redirect("challenge-signup-settings", group_slug=group_slug, month_pk=month.pk)
    return render(request, "core/challenge_signup_settings.html", {
        "month": month,
        "settings_form": settings_form,
        "question_formset": question_formset,
        "questions": questions,
        "schema_locked": False,
        "lifecycle_locked": False,
        "configuration_read_only": False,
        "tbr_settings_form": tbr_settings_form,
    })


@login_required
def challenge_progress_checkpoints(request, group_slug, month_pk):
    month = get_object_or_404(ChallengeMonth.objects.select_related("group"), pk=month_pk, group__slug=group_slug)
    if not can_configure_challenge_registration(request.user, month):
        return HttpResponseForbidden("Challenge configuration authority is required.")
    checkpoints = list(month.progress_checkpoints.all())
    configuration_locked = (
        not month_is_configurable(month)
        or any(checkpoint.evaluation_state != ProgressCheckpoint.EvaluationState.PENDING for checkpoint in checkpoints)
    )
    if configuration_locked:
        return render(request, "core/challenge_progress_checkpoints.html", {
            "month": month,
            "checkpoints": checkpoints,
            "configuration_locked": True,
        })
    initial = [{
        "checkpoint_id": checkpoint.pk,
        "scheduled_at": checkpoint.scheduled_at,
        "threshold_percentage": checkpoint.threshold_percentage,
        "progress_basis": checkpoint.progress_basis,
        "target_basis": checkpoint.target_basis,
        "fixed_target_pages": checkpoint.fixed_target_pages,
        "ORDER": checkpoint.position,
    } for checkpoint in checkpoints]
    with timezone.override(ZoneInfo(month.group.timezone)):
        formset = ProgressCheckpointFormSet(request.POST or None, initial=initial, prefix="checkpoints")
        if request.method == "POST" and formset.is_valid():
            with transaction.atomic():
                locked_month = ChallengeMonth.objects.select_for_update().get(pk=month.pk)
                locked_checkpoints = list(locked_month.progress_checkpoints.select_for_update().order_by("pk"))
                if locked_month.progress_checkpoints.exclude(
                    evaluation_state=ProgressCheckpoint.EvaluationState.PENDING
                ).exists():
                    messages.error(request, "Checkpoint configuration was locked because evaluation has begun.")
                    return redirect("challenge-progress-checkpoints", group_slug=group_slug, month_pk=month.pk)
                active_forms = [form for form in formset.forms if form.cleaned_data and not form.cleaned_data.get("DELETE")]
                active_forms.sort(key=lambda form: form.cleaned_data.get("ORDER") or 0)
                existing_by_id = {checkpoint.pk: checkpoint for checkpoint in locked_checkpoints}
                supplied_ids = [form.cleaned_data.get("checkpoint_id") for form in active_forms if form.cleaned_data.get("checkpoint_id")]
                if len(supplied_ids) != len(set(supplied_ids)) or any(checkpoint_id not in existing_by_id for checkpoint_id in supplied_ids):
                    messages.error(request, "Checkpoint configuration changed while this page was open. Review it and try again.")
                    return redirect("challenge-progress-checkpoints", group_slug=group_slug, month_pk=month.pk)
                omitted = [checkpoint for checkpoint in locked_checkpoints if checkpoint.pk not in supplied_ids]
                if any(checkpoint.recovery_hold for checkpoint in omitted):
                    messages.error(request, "A checkpoint under Recovery Hold must be explicitly released before it can be removed.")
                    return redirect("challenge-progress-checkpoints", group_slug=group_slug, month_pk=month.pk)
                locked_month.progress_checkpoints.update(position=F("position") + 10)
                for position, checkpoint_form in enumerate(active_forms, start=1):
                    values = {
                        "scheduled_at": checkpoint_form.cleaned_data["scheduled_at"],
                        "threshold_percentage": checkpoint_form.cleaned_data["threshold_percentage"],
                        "progress_basis": checkpoint_form.cleaned_data["progress_basis"],
                        "target_basis": checkpoint_form.cleaned_data["target_basis"],
                        "fixed_target_pages": checkpoint_form.cleaned_data["fixed_target_pages"],
                        "position": position,
                    }
                    checkpoint_id = checkpoint_form.cleaned_data.get("checkpoint_id")
                    if checkpoint_id:
                        ProgressCheckpoint.objects.filter(pk=checkpoint_id).update(**values)
                    else:
                        ProgressCheckpoint.objects.create(month=locked_month, **values)
                ProgressCheckpoint.objects.filter(pk__in=[checkpoint.pk for checkpoint in omitted]).delete()
                AuditEvent.objects.create(
                    actor=request.user,
                    group=month.group,
                    action="challenge.progress_checkpoints_updated",
                    object_type="ChallengeMonth",
                    object_id=str(month.pk),
                    summary=f"Updated {len(active_forms)} progress checkpoint(s) for {month.name}.",
                )
            messages.success(request, "Progress checkpoints updated.")
            return redirect("challenge-progress-checkpoints", group_slug=group_slug, month_pk=month.pk)
        return render(request, "core/challenge_progress_checkpoints.html", {
            "month": month,
            "checkpoints": checkpoints,
            "formset": formset,
            "configuration_locked": False,
        })


@login_required
def challenge_lifecycle_transition(request, group_slug, pk, target_status):
    month = get_object_or_404(ChallengeMonth.objects.select_related("group"), pk=pk, group__slug=group_slug)
    if not can_transition_challenge(request.user, month):
        return HttpResponseForbidden("Challenge lifecycle authority is required.")
    allowed_targets = {target["value"]: target for target in lifecycle_transition_targets(month)}
    target = allowed_targets.get(target_status)
    if not target:
        messages.error(request, "That lifecycle transition is not available.")
        return redirect(month)
    completed_recovery = month.status == ChallengeMonth.Status.COMPLETED and target_status == ChallengeMonth.Status.FINALIZING
    if request.method == "POST":
        previous_status = month.status
        try:
            month.transition_to(
                target_status,
                confirm_reversal=target["backward"],
                confirm_completed_recovery=completed_recovery,
            )
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
            return redirect(month)
        AuditEvent.objects.create(
            actor=request.user,
            group=month.group,
            action="challenge.lifecycle_changed",
            object_type="ChallengeMonth",
            object_id=str(month.pk),
            summary=f"Changed {month.name} lifecycle from {previous_status} to {target_status}.",
        )
        messages.success(request, f"Challenge moved to {month.get_status_display()}.")
        return redirect(month)
    if completed_recovery:
        description = (
            "This reopens a Challenge whose results were declared final. Final-result visibility may change, "
            "but registrations, staffing, teams, submissions, reviews, themes, scores, and history will be preserved."
        )
        action_label = "Confirm Recovery to Finalizing"
    elif target["backward"]:
        description = (
            "This moves the Challenge backward one operational stage without deleting or rewinding any existing records."
        )
        action_label = f"Confirm Move to {target['label']}"
    else:
        description = "This moves the Challenge forward one lifecycle stage. Existing records will be preserved."
        action_label = f"Move to {target['label']}"
    return render(request, "core/confirm_remove.html", {
        "eyebrow": "Challenge Lifecycle",
        "title": f"Move {month.name} to {target['label']}?",
        "description": description,
        "cancel_url": month.get_absolute_url(),
        "action_label": action_label,
        "hide_reason": True,
    })


@login_required
def month_delete(request, group_slug, pk):
    month = get_object_or_404(ChallengeMonth.objects.select_related("group"), pk=pk, group__slug=group_slug)
    if not can_manage_months(request.user, month.group):
        return HttpResponseForbidden("Month management permission is required.")
    if month.status != ChallengeMonth.Status.DRAFT:
        messages.error(request, "Only draft months can be deleted.")
        return redirect(month)
    if request.method == "POST":
        group = month.group
        month_name = month.name
        month.delete()
        AuditEvent.objects.create(actor=request.user, group=group, action="month.deleted", object_type="ChallengeMonth", summary=f"Deleted draft month {month_name}.")
        messages.success(request, f"Draft month {month_name} was deleted.")
        return redirect("group-detail", group_slug=group.slug)
    return render(request, "core/confirm_remove.html", {"title": f"Delete draft {month.name}?", "description": "This permanently removes the draft and its prepared teams and enrollments.", "cancel_url": month.get_absolute_url(), "action_label": "Confirm Delete", "hide_reason": True})


@login_required
def member_create(request, group_slug):
    group = get_object_or_404(ReadingGroup, slug=group_slug)
    if not can_manage_participants(request.user, group):
        return HttpResponseForbidden("Group member management permission is required.")
    form = MemberCreateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        membership = form.save(group)
        AuditEvent.objects.create(actor=request.user, group=group, action="membership.created", object_type="Membership", object_id=str(membership.pk), summary=f"Added {membership.display_name} as {membership.get_role_display()}")
        messages.success(request, f"{membership.display_name} can now sign in with the temporary password.")
        return redirect("group-detail", group_slug=group.slug)
    return render(request, "core/form_page.html", {"form": form, "title": "Add Member", "eyebrow": group.name})


@login_required
def month_detail(request, group_slug, pk):
    month = get_object_or_404(ChallengeMonth.objects.select_related("group"), pk=pk, group__slug=group_slug)
    membership = membership_for(request.user, month.group)
    if not request.user.is_superuser and not membership:
        return HttpResponseForbidden("You are not a member of this reading group.")
    if not can_view_challenge(request.user, month):
        raise Http404("Challenge not found.")
    leader_assignments = ChallengeStaffAssignment.objects.filter(
        role=ChallengeStaffAssignment.Role.TEAM_LEADER,
        ended_at__isnull=True,
    ).select_related("membership")
    teams = list(
        month.teams.filter(is_archived=False).prefetch_related(
            Prefetch(
                "assignments",
                queryset=TeamAssignment.objects.filter(
                    ended_at__isnull=True,
                    participant__month_enrollments__month=month,
                    participant__month_enrollments__is_active=True,
                ).select_related("participant"),
            ),
            Prefetch("staff_assignments", queryset=leader_assignments, to_attr="current_leader_assignments"),
        )
    )
    _annotate_team_leader_rosters(teams)
    participant_ids = {
        assignment.participant_id
        for team in teams
        for assignment in team.assignments.all()
    }
    score_by_participant = challenge_score_totals(month=month, participant_ids=participant_ids)
    comparison_teams = []
    for team in teams:
        team.can_view_standings = can_view_team_standings(request.user, month, team=team)
        if team.can_view_standings:
            team.visible_approved_pages = sum(
                score_by_participant[assignment.participant_id]["total_pages"]
                for assignment in team.assignments.all()
            )
            comparison_teams.append(team)
    max_team_pages = max((team.visible_approved_pages for team in comparison_teams), default=0)
    for team in comparison_teams:
        team.chart_percent = round((team.visible_approved_pages / max_team_pages) * 100, 1) if max_team_pages else 0
    theme_access = can_operate_challenge(request.user, month)
    theme_queryset = month.themes.all() if theme_access else month.themes.filter(is_active=True, is_visible=True)
    theme_preview = list(theme_queryset[:3])
    theme_more_count = max(theme_queryset.count() - len(theme_preview), 0)
    game_preview = list(month.games.all()[:3]) if month.games_enabled else []
    game_more_count = max(month.games.count() - len(game_preview), 0) if month.games_enabled else 0
    botm_preview = list(month.botm_books.filter(is_retired=False).select_related(
        "catalog_book", "catalog_edition"
    ).order_by("position", "pk")) if month.botm_enabled else []
    botm_reader = build_botm_reader_presentation(
        month=month,
        participant=membership,
        books=botm_preview,
    ) if month.botm_enabled else None
    personal_tbr = PersonalTBR.objects.filter(
        enrollment__month=month,
        enrollment__participant=membership,
        confirmed_at__isnull=False,
    ).first() if membership and month.tbr_enabled else None
    personal_tbr_reader = build_personal_tbr_reader_presentation(
        personal_tbr=personal_tbr,
    ) if personal_tbr else None
    visible_submissions = month.submissions.filter(is_removed=False).select_related(
        "participant", "participant__user", "participant__user__northbound_profile",
        "catalog_book", "catalog_edition",
    ).prefetch_related("theme_claims__theme")
    if request.user.is_superuser:
        submission_heading = "All Submissions · Platform Administration"
    else:
        visible_submissions = visible_submissions.filter(participant=membership)
        submission_heading = "My Submissions"
    pending_submissions = scope_reviewable_submissions(
        request.user,
        month,
        month.submissions.filter(
            Q(status=BookSubmission.Status.PENDING) | Q(theme_claims__status=ThemeClaim.Status.PENDING),
            is_removed=False,
        ),
    ).distinct()
    enrollment = MonthEnrollment.objects.filter(month=month, participant=membership).first() if membership else None
    registration_lifecycle_open = month.status in {ChallengeMonth.Status.UPCOMING, ChallengeMonth.Status.ACTIVE}
    context = {
        "month": month,
        "membership": membership,
        "book_count": month.submissions.filter(is_removed=False).count(),
        "can_manage_months": can_manage_months(request.user, month.group),
        "can_configure_registration": can_configure_challenge_registration(request.user, month),
        "can_configure_optional_features": month_is_configurable(month) and can_operate_challenge(request.user, month),
        "can_configure_checkpoints": can_configure_challenge_registration(request.user, month),
        "can_access_challenge_settings": (
            can_manage_months(request.user, month.group)
            or can_configure_challenge_registration(request.user, month)
            or can_transition_challenge(request.user, month)
        ),
        "can_transition_lifecycle": can_transition_challenge(request.user, month),
        "lifecycle_targets": lifecycle_transition_targets(month),
        "can_manage_challenge_announcements": month_is_configurable(month) and can_manage_challenge_announcements(request.user, month),
        "can_manage_teams": month_is_configurable(month) and can_operate_challenge(request.user, month),
        "can_review": month.status in REVIEWABLE_MONTH_STATUSES and can_review_challenge(request.user, month),
        "can_remove_submission": month_is_configurable(month) and can_operate_challenge(request.user, month),
        "is_enrolled": bool(enrollment and enrollment.is_active),
        "reader_enrollment": enrollment,
        "can_edit_registration": bool(enrollment and enrollment.is_active and enrollment.can_reader_edit_registration_answers()),
        "active_enrollment_count": month.enrollments.filter(is_active=True).count(),
        "can_self_register": bool(
            membership
            and not request.user.is_superuser
            and month.registration_is_open
            and registration_lifecycle_open
            and (not enrollment or enrollment.inactive_reason != MonthEnrollment.InactiveReason.REMOVED)
            and (not enrollment or not enrollment.is_active)
            and not month.staff_assignments.filter(
                membership=membership,
                role=ChallengeStaffAssignment.Role.FLOATER,
                ended_at__isnull=True,
            ).exists()
        ),
        "can_self_withdraw": bool(
            enrollment
            and enrollment.is_active
            and registration_lifecycle_open
        ),
        "pending_count": pending_submissions.count(),
        "teams": teams,
        "comparison_teams": comparison_teams,
        "theme_preview": theme_preview,
        "theme_more_count": theme_more_count,
        "game_preview": game_preview,
        "game_more_count": game_more_count,
        "botm_preview": botm_preview,
        "botm_reader": botm_reader,
        "personal_tbr": personal_tbr,
        "personal_tbr_reader": personal_tbr_reader,
        "active_team_count": month.teams.filter(is_archived=False).count(),
        "visible_submissions": visible_submissions,
        "submission_heading": submission_heading,
        "current_hosts": month.staff_assignments.filter(
            role=ChallengeStaffAssignment.Role.HOST,
            ended_at__isnull=True,
        ).select_related("membership"),
        "can_manage_hosts": can_manage_challenge_hosts(request.user, month.group),
        "current_floaters": month.staff_assignments.filter(
            role=ChallengeStaffAssignment.Role.FLOATER,
            ended_at__isnull=True,
        ).select_related("membership"),
        "can_manage_floaters": can_operate_challenge(request.user, month),
    }
    return render(request, "core/month_detail.html", context)


@login_required
def challenge_host_list(request, group_slug, month_pk):
    month = get_object_or_404(ChallengeMonth.objects.select_related("group"), pk=month_pk, group__slug=group_slug)
    membership = membership_for(request.user, month.group)
    if not request.user.is_superuser and not membership:
        return HttpResponseForbidden("You are not a member of this reading group.")
    if not can_view_challenge(request.user, month):
        raise Http404("Challenge not found.")
    can_manage = can_manage_challenge_hosts(request.user, month.group)
    if request.method == "POST" and not can_manage:
        return HttpResponseForbidden("Month management permission is required.")
    form = ChallengeHostAssignmentForm(request.POST or None, month=month) if can_manage else None
    if request.method == "POST" and form.is_valid():
        assignment = form.save(assigned_by=request.user)
        AuditEvent.objects.create(
            actor=request.user,
            group=month.group,
            action="challenge.host_assigned",
            object_type="ChallengeStaffAssignment",
            object_id=str(assignment.pk),
            summary=f"Assigned {assignment.membership.display_name} as a Host for {month.name}.",
        )
        messages.success(request, f"{assignment.membership.display_name} is now a Host for {month.name}.")
        return redirect("challenge-host-list", group_slug=group_slug, month_pk=month_pk)
    assignments = month.staff_assignments.filter(
        role=ChallengeStaffAssignment.Role.HOST,
    ).select_related("membership", "assigned_by", "ended_by")
    return render(
        request,
        "core/challenge_host_list.html",
        {
            "month": month,
            "form": form,
            "can_manage": can_manage,
            "current_hosts": assignments.filter(ended_at__isnull=True),
            "past_hosts": assignments.filter(ended_at__isnull=False).order_by("-ended_at"),
        },
    )


@login_required
def challenge_host_end(request, group_slug, month_pk, pk):
    assignment = get_object_or_404(
        ChallengeStaffAssignment.objects.select_related("month__group", "membership"),
        pk=pk,
        month_id=month_pk,
        month__group__slug=group_slug,
        role=ChallengeStaffAssignment.Role.HOST,
        ended_at__isnull=True,
    )
    if not can_manage_challenge_hosts(request.user, assignment.month.group):
        return HttpResponseForbidden("Month management permission is required.")
    if request.method == "POST":
        assignment.ended_at = timezone.now()
        assignment.ended_by = request.user
        assignment.save(update_fields=["ended_at", "ended_by"])
        AuditEvent.objects.create(
            actor=request.user,
            group=assignment.month.group,
            action="challenge.host_ended",
            object_type="ChallengeStaffAssignment",
            object_id=str(assignment.pk),
            summary=f"Ended {assignment.membership.display_name}'s Host assignment for {assignment.month.name}.",
        )
        messages.success(request, f"{assignment.membership.display_name} is no longer a current Host for {assignment.month.name}.")
        return redirect("challenge-host-list", group_slug=group_slug, month_pk=month_pk)
    return render(
        request,
        "core/confirm_remove.html",
        {
            "eyebrow": "Challenge Host",
            "title": f"Remove {assignment.membership.display_name} as Host?",
            "description": "Their Host assignment will end, but its dates and attribution will remain in staffing history.",
            "cancel_url": reverse("challenge-host-list", kwargs={"group_slug": group_slug, "month_pk": month_pk}),
            "action_label": "Remove Host",
            "hide_reason": True,
        },
    )


@login_required
def host_assignment_notice_open(request, group_slug, month_pk, pk):
    assignment = get_object_or_404(
        ChallengeStaffAssignment.objects.select_related("month__group", "membership"),
        pk=pk,
        month_id=month_pk,
        month__group__slug=group_slug,
        membership__user=request.user,
        membership__is_active=True,
        role=ChallengeStaffAssignment.Role.HOST,
        ended_at__isnull=True,
    )
    if assignment.host_assignment_notice_seen_at is None:
        assignment.host_assignment_notice_seen_at = timezone.now()
        assignment.save(update_fields=["host_assignment_notice_seen_at"])
    return redirect("challenge-settings", group_slug=group_slug, month_pk=month_pk)


@login_required
def challenge_floater_list(request, group_slug, month_pk):
    month = get_object_or_404(ChallengeMonth.objects.select_related("group"), pk=month_pk, group__slug=group_slug)
    membership = membership_for(request.user, month.group)
    if not request.user.is_superuser and not membership:
        return HttpResponseForbidden("You are not a member of this reading group.")
    if not can_view_challenge(request.user, month):
        raise Http404("Challenge not found.")
    can_manage = can_operate_challenge(request.user, month)
    if request.method == "POST" and not can_manage:
        return HttpResponseForbidden("A current Host assignment is required.")
    form = ChallengeFloaterAssignmentForm(request.POST or None, month=month) if can_manage else None
    if request.method == "POST" and form.is_valid():
        assignment = form.save(assigned_by=request.user)
        AuditEvent.objects.create(
            actor=request.user,
            group=month.group,
            action="challenge.floater_assigned",
            object_type="ChallengeStaffAssignment",
            object_id=str(assignment.pk),
            summary=f"Assigned {assignment.membership.display_name} as a Floater for {month.name}.",
        )
        messages.success(request, f"{assignment.membership.display_name} is now a non-competing Floater for {month.name}.")
        return redirect("challenge-floater-list", group_slug=group_slug, month_pk=month_pk)
    assignments = month.staff_assignments.filter(
        role=ChallengeStaffAssignment.Role.FLOATER,
    ).select_related("membership", "assigned_by", "ended_by")
    return render(
        request,
        "core/challenge_floater_list.html",
        {
            "month": month,
            "form": form,
            "can_manage": can_manage,
            "current_floaters": assignments.filter(ended_at__isnull=True),
            "past_floaters": assignments.filter(ended_at__isnull=False).order_by("-ended_at"),
        },
    )


@login_required
def challenge_floater_end(request, group_slug, month_pk, pk):
    assignment = get_object_or_404(
        ChallengeStaffAssignment.objects.select_related("month__group", "membership"),
        pk=pk,
        month_id=month_pk,
        month__group__slug=group_slug,
        role=ChallengeStaffAssignment.Role.FLOATER,
        ended_at__isnull=True,
    )
    if not can_operate_challenge(request.user, assignment.month):
        return HttpResponseForbidden("A current Host assignment is required.")
    if request.method == "POST":
        assignment.ended_at = timezone.now()
        assignment.ended_by = request.user
        assignment.save(update_fields=["ended_at", "ended_by"])
        AuditEvent.objects.create(
            actor=request.user,
            group=assignment.month.group,
            action="challenge.floater_ended",
            object_type="ChallengeStaffAssignment",
            object_id=str(assignment.pk),
            summary=f"Ended {assignment.membership.display_name}'s Floater assignment for {assignment.month.name}.",
        )
        messages.success(request, f"{assignment.membership.display_name} is no longer a current Floater for {assignment.month.name}.")
        return redirect("challenge-floater-list", group_slug=group_slug, month_pk=month_pk)
    return render(
        request,
        "core/confirm_remove.html",
        {
            "eyebrow": "Challenge Floater",
            "title": f"Remove {assignment.membership.display_name} as Floater?",
            "description": "Their non-competing staffing assignment will end, but its dates and attribution will remain in staffing history.",
            "cancel_url": reverse("challenge-floater-list", kwargs={"group_slug": group_slug, "month_pk": month_pk}),
            "action_label": "Remove Floater",
            "hide_reason": True,
        },
    )
@login_required
def team_leader_list(request, group_slug, month_pk, team_pk):
    team = get_object_or_404(
        Team.objects.select_related("month__group"),
        pk=team_pk,
        month_id=month_pk,
        month__group__slug=group_slug,
    )
    membership = membership_for(request.user, team.month.group)
    if not request.user.is_superuser and not membership:
        return HttpResponseForbidden("You are not a member of this reading group.")
    if not can_view_challenge(request.user, team.month):
        raise Http404("Challenge not found.")
    can_manage = can_operate_challenge(request.user, team.month)
    if request.method == "POST" and not can_manage:
        return HttpResponseForbidden("A current Host assignment is required.")
    form = ChallengeTeamLeaderAssignmentForm(request.POST or None, team=team) if can_manage else None
    if request.method == "POST" and form.is_valid():
        assignment = form.save(assigned_by=request.user)
        AuditEvent.objects.create(
            actor=request.user,
            group=team.month.group,
            action="challenge.team_leader_assigned",
            object_type="ChallengeStaffAssignment",
            object_id=str(assignment.pk),
            summary=(
                f"Assigned {assignment.membership.display_name} as a Team Leader for {team.name} "
                f"in {team.month.name}."
            ),
        )
        messages.success(request, f"{assignment.membership.display_name} is now a Team Leader for {team.name}.")
        return redirect("team-leader-list", group_slug=group_slug, month_pk=month_pk, team_pk=team_pk)
    assignments = team.staff_assignments.filter(
        role=ChallengeStaffAssignment.Role.TEAM_LEADER,
    ).select_related("membership", "assigned_by", "ended_by")
    return render(
        request,
        "core/team_leader_list.html",
        {
            "month": team.month,
            "team": team,
            "form": form,
            "can_manage": can_manage,
            "current_leaders": assignments.filter(ended_at__isnull=True),
            "past_leaders": assignments.filter(ended_at__isnull=False).order_by("-ended_at"),
        },
    )


@login_required
def team_leader_end(request, group_slug, month_pk, team_pk, pk):
    assignment = get_object_or_404(
        ChallengeStaffAssignment.objects.select_related("month__group", "membership", "team"),
        pk=pk,
        month_id=month_pk,
        month__group__slug=group_slug,
        team_id=team_pk,
        role=ChallengeStaffAssignment.Role.TEAM_LEADER,
        ended_at__isnull=True,
    )
    if not can_operate_challenge(request.user, assignment.month):
        return HttpResponseForbidden("A current Host assignment is required.")
    if request.method == "POST":
        assignment.ended_at = timezone.now()
        assignment.ended_by = request.user
        assignment.save(update_fields=["ended_at", "ended_by"])
        AuditEvent.objects.create(
            actor=request.user,
            group=assignment.month.group,
            action="challenge.team_leader_ended",
            object_type="ChallengeStaffAssignment",
            object_id=str(assignment.pk),
            summary=(
                f"Ended {assignment.membership.display_name}'s Team Leader assignment for "
                f"{assignment.team.name} in {assignment.month.name}."
            ),
        )
        messages.success(request, f"{assignment.membership.display_name} is no longer a Team Leader for {assignment.team.name}.")
        return redirect("team-leader-list", group_slug=group_slug, month_pk=month_pk, team_pk=team_pk)
    return render(
        request,
        "core/confirm_remove.html",
        {
            "eyebrow": "Team Leader",
            "title": f"Remove {assignment.membership.display_name} as Team Leader?",
            "description": "Their staffing assignment will end, but they will remain enrolled and assigned to the team.",
            "cancel_url": reverse(
                "team-leader-list",
                kwargs={"group_slug": group_slug, "month_pk": month_pk, "team_pk": team_pk},
            ),
            "action_label": "Remove Team Leader",
            "hide_reason": True,
        },
    )


@login_required
def month_announcement_update(request, group_slug, month_pk):
    month = get_object_or_404(ChallengeMonth.objects.select_related("group"), pk=month_pk, group__slug=group_slug)
    if not can_manage_challenge_announcements(request.user, month):
        return HttpResponseForbidden("Challenge announcement management authority is required.")
    if reject_locked_month(request, month, "change its announcement"):
        return redirect("challenge-settings", group_slug=group_slug, month_pk=month.pk)
    form = ChallengeAnnouncementForm(request.POST or None, instance=month)
    if request.method == "POST" and form.is_valid():
        form.save()
        AuditEvent.objects.create(actor=request.user, group=month.group, action="challenge.announcement_updated", object_type="ChallengeMonth", object_id=str(month.pk), summary=f"Updated the Challenge announcement for {month.name}.")
        messages.success(request, "Challenge Announcement updated.")
        return redirect("challenge-settings", group_slug=group_slug, month_pk=month.pk)
    return render(request, "core/challenge_announcement_settings.html", {"month": month, "form": form})


@login_required
def team_create(request, group_slug, month_pk):
    month = get_object_or_404(ChallengeMonth, pk=month_pk, group__slug=group_slug)
    if not can_operate_challenge(request.user, month):
        return HttpResponseForbidden("A current Host assignment is required.")
    if reject_locked_month(request, month, "add a team"):
        return redirect(month)
    form = TeamForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        team = form.save(commit=False)
        team.month = month
        team.save()
        return redirect("team-list", group_slug=group_slug, month_pk=month_pk)
    return render(request, "core/form_page.html", {"form": form, "title": "Add Team", "eyebrow": month.name})


@login_required
def team_edit(request, group_slug, month_pk, pk):
    team = get_object_or_404(Team.objects.select_related("month__group"), pk=pk, month_id=month_pk, month__group__slug=group_slug)
    if not can_operate_challenge(request.user, team.month):
        return HttpResponseForbidden("A current Host assignment is required.")
    if reject_locked_month(request, team.month, "edit a team"):
        return redirect(team.month)
    form = TeamForm(request.POST or None, instance=team)
    if request.method == "POST" and form.is_valid():
        previous_name = team.name
        updated = form.save()
        AuditEvent.objects.create(actor=request.user, group=team.month.group, action="team.updated", object_type="Team", object_id=str(team.pk), summary=f"Updated team {previous_name} to {updated.name} for {team.month.name}.")
        messages.success(request, "Team updated.")
        return redirect("team-list", group_slug=group_slug, month_pk=month_pk)
    can_delete_team = can_operate_challenge(request.user, team.month) and team.month.status == ChallengeMonth.Status.DRAFT and not team.assignments.exists()
    return render(request, "core/team_edit.html", {"form": form, "team": team, "can_delete_team": can_delete_team})


@login_required
def team_archive_toggle(request, group_slug, month_pk, pk):
    team = get_object_or_404(Team.objects.select_related("month__group"), pk=pk, month_id=month_pk, month__group__slug=group_slug)
    if not can_operate_challenge(request.user, team.month):
        return HttpResponseForbidden("A current Host assignment is required.")
    if reject_locked_month(request, team.month, "archive or restore a team"):
        return redirect(team.month)
    edit_url = reverse("team-edit", kwargs={"group_slug": group_slug, "month_pk": month_pk, "pk": pk})
    if request.method != "POST":
        if team.is_archived:
            return redirect(edit_url)
        return render(request, "core/confirm_remove.html", {
            "eyebrow": "Team Action",
            "title": f"Archive {team.name}?",
            "description": "This hides the team from active views and selectors while preserving its roster, submissions, and statistics.",
            "cancel_url": edit_url,
            "action_label": "Confirm Archive",
            "hide_reason": True,
        })
    team.is_archived = not team.is_archived
    team.save(update_fields=["is_archived"])
    action = "archived" if team.is_archived else "restored"
    AuditEvent.objects.create(actor=request.user, group=team.month.group, action=f"team.{action}", object_type="Team", object_id=str(team.pk), summary=f"{action.title()} {team.name} for {team.month.name}.")
    messages.success(request, f"{team.name} was {action}.")
    target = reverse("team-list", kwargs={"group_slug": group_slug, "month_pk": month_pk})
    return redirect(f"{target}?archive=1" if team.is_archived else target)


@login_required
def team_delete(request, group_slug, month_pk, pk):
    team = get_object_or_404(Team.objects.select_related("month__group"), pk=pk, month_id=month_pk, month__group__slug=group_slug)
    if not can_operate_challenge(request.user, team.month):
        return HttpResponseForbidden("A current Host assignment is required.")
    if team.month.status != ChallengeMonth.Status.DRAFT or team.assignments.exists():
        messages.error(request, "Only unused teams in a Draft month can be deleted. Archive this team instead.")
        return redirect("team-edit", group_slug=group_slug, month_pk=month_pk, pk=pk)
    if request.method == "POST":
        group = team.month.group
        month = team.month
        team_name = team.name
        team.delete()
        AuditEvent.objects.create(actor=request.user, group=group, action="team.deleted", object_type="Team", summary=f"Deleted unused draft team {team_name} from {month.name}.")
        messages.success(request, f"{team_name} was deleted.")
        return redirect("team-list", group_slug=group_slug, month_pk=month_pk)
    return render(request, "core/confirm_remove.html", {"title": f"Delete {team.name}?", "description": "This permanently removes the unused team from this Draft month.", "cancel_url": reverse("team-edit", kwargs={"group_slug": group_slug, "month_pk": month_pk, "pk": pk}), "action_label": "Confirm Delete", "hide_reason": True})


@login_required
def team_assignment_create(request, group_slug, month_pk):
    month = get_object_or_404(ChallengeMonth, pk=month_pk, group__slug=group_slug)
    if not can_operate_challenge(request.user, month):
        return HttpResponseForbidden("A current Host assignment is required.")
    if reject_locked_month(request, month, "change its team roster"):
        return redirect(month)
    form = TeamAssignmentForm(request.POST or None, month=month)
    if request.method == "POST" and form.is_valid():
        assignment = form.save(actor=request.user)
        messages.success(request, "Participant assigned to the team.")
        return redirect(month)
    return render(request, "core/form_page.html", {"form": form, "title": "Assign Participant", "eyebrow": month.name})


@login_required
def month_participant_list(request, group_slug, month_pk):
    month = get_object_or_404(ChallengeMonth.objects.select_related("group"), pk=month_pk, group__slug=group_slug)
    membership = membership_for(request.user, month.group)
    if not request.user.is_superuser and not membership:
        return HttpResponseForbidden("You are not a member of this reading group.")
    if not can_view_challenge(request.user, month):
        raise Http404("Challenge not found.")
    current_month_assignments = TeamAssignment.objects.filter(month=month, ended_at__isnull=True).select_related("team")
    enrollments = list(month.enrollments.select_related(
        "participant__user", "participant__user__northbound_profile", "inactivated_by"
    ).prefetch_related(
        Prefetch(
            "participant__team_assignments",
            queryset=current_month_assignments,
            to_attr="current_month_team_assignments",
        ),
        "signup_answers__question",
    ))
    mutable = month_is_configurable(month)
    host_access = can_operate_challenge(request.user, month)
    planning_access = can_view_challenge_registration_answers(request.user, month)
    planning_by_participant = historical_reader_planning_data(
        month=month,
        participant_ids=[enrollment.participant_id for enrollment in enrollments],
    ) if planning_access else {}
    checkpoint_warnings = {}
    if planning_access:
        for result in ProgressCheckpointResult.objects.filter(
            checkpoint__month=month,
            outcome=ProgressCheckpointResult.Outcome.BELOW,
        ).select_related("checkpoint").order_by("checkpoint__scheduled_at", "pk"):
            checkpoint_warnings.setdefault(result.participant_id, []).append(result)
    questions = list(month.signup_questions.all()) if planning_access else []
    for enrollment in enrollments:
        enrollment.planning = planning_by_participant.get(enrollment.participant_id)
        enrollment.current_team_name = (
            enrollment.participant.current_month_team_assignments[0].team.name
            if enrollment.participant.current_month_team_assignments
            else ""
        )
        enrollment.checkpoint_warnings = checkpoint_warnings.get(enrollment.participant_id, [])
        if planning_access:
            answer_map = {answer.question_id: answer.value for answer in enrollment.signup_answers.all()}
            enrollment.registration_rows = []
            for question in questions:
                value = answer_map.get(question.pk, "")
                enrollment.registration_rows.append({
                    "question": question,
                    "value": ", ".join(value) if isinstance(value, list) else value,
                })

    requested_sort = request.GET.get("sort", "reader")
    direction = request.GET.get("direction", "asc")
    allowed_sorts = {"reader", "team"}
    if planning_access:
        allowed_sorts.update({"average", "last", "completed"})
    sort_key = requested_sort if requested_sort in allowed_sorts else "reader"
    direction = direction if direction in {"asc", "desc"} else "asc"

    def value_for(enrollment):
        if sort_key == "team":
            return enrollment.current_team_name.casefold()
        if sort_key == "average":
            return enrollment.planning.average_pages
        if sort_key == "last":
            return enrollment.planning.last_challenge_pages
        if sort_key == "completed":
            return enrollment.planning.completed_challenges
        return enrollment.participant.display_name.casefold()

    available = [enrollment for enrollment in enrollments if value_for(enrollment) is not None]
    unavailable = [enrollment for enrollment in enrollments if value_for(enrollment) is None]
    available.sort(
        key=lambda enrollment: (value_for(enrollment), enrollment.participant.display_name.casefold(), enrollment.pk),
        reverse=direction == "desc",
    )
    enrollments = available + unavailable
    sort_headers = {}
    for key in ("reader", "average", "last", "completed", "team"):
        next_direction = "desc" if sort_key == key and direction == "asc" else "asc"
        sort_headers[key] = {
            "url": f"?sort={key}&direction={next_direction}",
            "active": sort_key == key,
            "direction": direction if sort_key == key else "",
        }

    return render(request, "core/month_participant_list.html", {
        "month": month,
        "enrollments": enrollments,
        "can_view_planning_data": planning_access,
        "can_view_registration_answers": planning_access,
        "can_view_discord_usernames": planning_access,
        "can_manage_participants": mutable and host_access,
        "can_manage_teams": mutable and host_access,
        "can_remove": mutable and host_access,
        "sort_headers": sort_headers,
    })


@login_required
def month_participant_add(request, group_slug, month_pk):
    month = get_object_or_404(ChallengeMonth.objects.select_related("group"), pk=month_pk, group__slug=group_slug)
    if not can_operate_challenge(request.user, month):
        return HttpResponseForbidden("A current Host assignment is required.")
    if reject_locked_month(request, month, "add a participant"):
        return redirect(month)
    form = MonthEnrollmentForm(request.POST or None, month=month)
    if request.method == "POST" and form.is_valid():
        enrollment = form.save(enrolled_by=request.user)
        team = form.cleaned_data.get("team")
        team_message = f" and was assigned to {team.name}" if team else " without a team assignment"
        messages.success(request, f"{enrollment.participant.display_name} was added to {month.name}{team_message}.")
        return redirect("month-participant-list", group_slug=month.group.slug, month_pk=month.pk)
    return render(request, "core/form_page.html", {"form": form, "title": "Add Participant to Month", "eyebrow": month.name})


@login_required
def month_participant_edit(request, group_slug, month_pk, pk):
    enrollment = get_object_or_404(MonthEnrollment.objects.select_related("month__group", "participant"), pk=pk, month_id=month_pk, month__group__slug=group_slug, is_active=True)
    if not can_operate_challenge(request.user, enrollment.month):
        return HttpResponseForbidden("A current Host assignment is required.")
    if reject_locked_month(request, enrollment.month, "change its team roster"):
        return redirect(enrollment.month)
    form = MonthParticipantEditForm(request.POST or None, enrollment=enrollment)
    if request.method == "POST" and form.is_valid():
        previous_team, new_team = form.save(actor=request.user)
        messages.success(request, f"Updated {enrollment.participant.display_name}'s team assignment.")
        return redirect("month-participant-list", group_slug=group_slug, month_pk=month_pk)
    return render(request, "core/form_page.html", {"form": form, "title": f"Edit {enrollment.participant.display_name}", "eyebrow": enrollment.month.name})


@login_required
def month_participant_remove(request, group_slug, month_pk, pk):
    enrollment = get_object_or_404(MonthEnrollment.objects.select_related("month__group", "participant"), pk=pk, month_id=month_pk, month__group__slug=group_slug, is_active=True)
    if not can_operate_challenge(request.user, enrollment.month):
        return HttpResponseForbidden("A current Host assignment is required.")
    if reject_locked_month(request, enrollment.month, "remove a participant"):
        return redirect(enrollment.month)
    if request.method == "POST":
        participant = enrollment.participant
        deactivate_participation(
            enrollment=enrollment,
            actor=request.user,
            reason=MonthEnrollment.InactiveReason.REMOVED,
            note=request.POST.get("reason", "").strip(),
        )
        messages.success(request, f"{participant.display_name} was removed from active participation in {enrollment.month.name}.")
        return redirect("month-participant-list", group_slug=group_slug, month_pk=month_pk)
    return render(request, "core/confirm_remove.html", {"title": f"Remove {enrollment.participant.display_name} from {enrollment.month.name}?", "description": "They will leave the current roster and can no longer submit. Existing submissions, approved pages, and team history will be preserved.", "cancel_url": reverse("month-participant-list", kwargs={"group_slug": group_slug, "month_pk": month_pk})})


@login_required
def month_participant_reactivate(request, group_slug, month_pk, pk):
    enrollment = get_object_or_404(
        MonthEnrollment.objects.select_related("month__group", "participant"),
        pk=pk,
        month_id=month_pk,
        month__group__slug=group_slug,
        is_active=False,
    )
    if not can_operate_challenge(request.user, enrollment.month):
        return HttpResponseForbidden("A current Host assignment is required.")
    if reject_locked_month(request, enrollment.month, "reactivate a participant"):
        return redirect(enrollment.month)
    if request.method == "POST":
        activate_participation(
            month=enrollment.month,
            participant=enrollment.participant,
            actor=request.user,
            origin=MonthEnrollment.Origin.STAFF,
        )
        messages.success(request, f"{enrollment.participant.display_name} was reactivated without a team assignment.")
        return redirect("month-participant-list", group_slug=group_slug, month_pk=month_pk)
    return render(request, "core/confirm_remove.html", {
        "eyebrow": "Challenge Participation",
        "title": f"Reactivate {enrollment.participant.display_name}?",
        "description": "They will return to active participation without restoring a previous team or Team Leader assignment.",
        "cancel_url": reverse("month-participant-list", kwargs={"group_slug": group_slug, "month_pk": month_pk}),
        "action_label": "Reactivate Reader",
        "hide_reason": True,
    })


@login_required
def challenge_register(request, group_slug, month_pk):
    month = get_object_or_404(ChallengeMonth.objects.select_related("group"), pk=month_pk, group__slug=group_slug)
    participant = membership_for(request.user, month.group)
    if request.user.is_superuser or not participant:
        return HttpResponseForbidden("An active normal Group membership is required to register.")
    if not month.registration_is_open or month.status not in {ChallengeMonth.Status.UPCOMING, ChallengeMonth.Status.ACTIVE}:
        messages.error(request, "Registration is not currently available for this Challenge.")
        return redirect(month)
    existing = MonthEnrollment.objects.filter(month=month, participant=participant).first()
    if existing and existing.is_active:
        messages.info(request, f"You are already registered for {month.name}.")
        return redirect(month)
    if existing and not existing.is_active and existing.inactive_reason == MonthEnrollment.InactiveReason.REMOVED:
        messages.error(request, "A Host must reactivate this participation record.")
        return redirect(month)
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    answers_editable = not existing or existing.can_reader_edit_registration_answers()
    existing_tbr = getattr(existing, "personal_tbr", None) if existing else None
    locked_tbr = existing_tbr if existing_tbr and existing_tbr.confirmed_at else None
    draft_key = f"challenge-registration-draft:{request.user.pk}:{month.pk}"
    stored_draft = request.session.get(draft_key) if request.method == "GET" else None
    form_data = _registration_draft_querydict(stored_draft.get("registration", {})) if stored_draft else None
    form = ChallengeRegistrationForm(
        request.POST if request.method == "POST" else form_data,
        month=month,
        profile=profile,
        enrollment=existing,
        answers_editable=answers_editable,
    )
    tbr_data = None
    if request.method == "POST":
        tbr_data = request.POST
    elif stored_draft and stored_draft.get("tbr_enabled") == month.tbr_enabled:
        tbr_data = _registration_draft_querydict(stored_draft.get("tbr", {}))
    tbr_formset = None
    if month.tbr_enabled and not locked_tbr:
        tbr_formset = PersonalTbrRegistrationBookFormSet(tbr_data, prefix="tbr-books")
    forms_valid = form.is_valid() and (tbr_formset is None or tbr_formset.is_valid())
    if request.method == "POST" and forms_valid:
        request.session[draft_key] = {
            "registration": _registration_draft_values(request.POST, exclude_prefix="tbr-books-"),
            "tbr": _registration_draft_values(request.POST, include_prefix="tbr-books-") if tbr_formset else {},
            "tbr_enabled": month.tbr_enabled or bool(locked_tbr),
            "tbr_feature_enabled": month.tbr_enabled,
            "locked_tbr_id": locked_tbr.pk if locked_tbr else None,
        }
        books = tbr_formset.book_values() if tbr_formset else list(locked_tbr.books.all()) if locked_tbr else []
        return render(request, "core/challenge_registration_confirm.html", {
            "month": month,
            "form": form,
            "books": books,
            "locked_tbr": locked_tbr,
            "tbr_enabled": month.tbr_enabled,
            **_personal_tbr_confirmation_copy(month, len(books)),
        })
    return render(request, "core/challenge_registration.html", {
        "month": month,
        "form": form,
        "tbr_formset": tbr_formset,
        "tbr_enabled": month.tbr_enabled or bool(locked_tbr),
        "tbr_feature_enabled": month.tbr_enabled,
        "locked_tbr": locked_tbr,
        "reader_hardcover_available": _reader_hardcover_is_available(request.user),
        "is_reregistration": bool(existing),
        "answers_editable": answers_editable,
    })


def _registration_draft_values(post_data, *, include_prefix=None, exclude_prefix=None):
    values = {}
    for key in post_data.keys():
        if key == "csrfmiddlewaretoken":
            continue
        if include_prefix and not key.startswith(include_prefix):
            continue
        if exclude_prefix and key.startswith(exclude_prefix):
            continue
        values[key] = post_data.getlist(key)
    return values


def _registration_draft_querydict(values):
    data = QueryDict("", mutable=True)
    for key, items in values.items():
        data.setlist(key, items)
    return data


def _reader_hardcover_is_available(user):
    try:
        get_reader_hardcover_token(user)
    except ReaderHardcoverUnavailable:
        return False
    return True


def _personal_tbr_confirmation_copy(month, count):
    warning = ""
    if month.tbr_enabled and count == 0:
        warning = "You haven't added any Personal TBR books. If you continue, you will not be eligible for Personal TBR bonus pages for this Challenge."
    elif month.tbr_enabled and count < 9:
        parts = [f"You've added {count} of 9 Personal TBR books."]
        if month.tbr_book_bonus_pages > 0:
            parts.append("You may still earn the per-book TBR bonus for qualifying books.")
        if month.tbr_completion_bonus_pages > 0:
            parts.append("You will not be eligible for the full 9-book completion bonus.")
        warning = " ".join(parts)
    return {
        "tbr_count": count,
        "tbr_warning": warning,
        "tbr_book_bonus_copy": (
            f"Each qualifying TBR book is worth +{month.tbr_book_bonus_pages} bonus pages."
            if month.tbr_enabled and month.tbr_book_bonus_pages > 0 else ""
        ),
        "tbr_completion_bonus_copy": (
            f"Complete all 9 books for an additional +{month.tbr_completion_bonus_pages} bonus pages."
            if month.tbr_enabled and month.tbr_completion_bonus_pages > 0 else ""
        ),
    }


def _registration_confirmation_summary(form):
    rows = []
    for name, field in form.fields.items():
        value = form.cleaned_data.get(name, "")
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(item) for item in value)
        rows.append({"label": field.label, "value": value or "—"})
    return rows


@login_required
def challenge_register_confirm(request, group_slug, month_pk):
    if request.method != "POST":
        return redirect("challenge-register", group_slug=group_slug, month_pk=month_pk)
    draft_key = f"challenge-registration-draft:{request.user.pk}:{month_pk}"
    draft = request.session.get(draft_key)
    if not draft:
        messages.error(request, "Your registration draft is unavailable or expired. Review the form and try again.")
        return redirect("challenge-register", group_slug=group_slug, month_pk=month_pk)
    try:
        with transaction.atomic():
            month = ChallengeMonth.objects.select_for_update().select_related("group").get(pk=month_pk, group__slug=group_slug)
            participant = membership_for(request.user, month.group)
            if request.user.is_superuser or not participant:
                raise ValidationError("An active normal Group membership is required to register.")
            if not month.registration_is_open or month.status not in {ChallengeMonth.Status.UPCOMING, ChallengeMonth.Status.ACTIVE}:
                raise ValidationError("Registration is no longer available for this Challenge.")
            existing = MonthEnrollment.objects.select_for_update().filter(month=month, participant=participant).first()
            if existing and existing.is_active:
                raise ValidationError(f"You are already registered for {month.name}.")
            if existing and existing.inactive_reason == MonthEnrollment.InactiveReason.REMOVED:
                raise ValidationError("A Host must reactivate this participation record.")
            existing_tbr = getattr(existing, "personal_tbr", None) if existing else None
            locked_tbr = existing_tbr if existing_tbr and existing_tbr.confirmed_at else None
            if draft.get("tbr_enabled") != month.tbr_enabled and not locked_tbr:
                raise ValidationError("Personal TBR settings changed while you were registering. Review your registration again.")
            profile, _ = UserProfile.objects.get_or_create(user=request.user)
            answers_editable = not existing or existing.can_reader_edit_registration_answers()
            form = ChallengeRegistrationForm(
                _registration_draft_querydict(draft["registration"]),
                month=month,
                profile=profile,
                enrollment=existing,
                answers_editable=answers_editable,
            )
            if not form.is_valid():
                raise ValidationError("Registration questions changed or are no longer valid. Review your registration again.")
            tbr_formset = None
            if month.tbr_enabled and not locked_tbr:
                tbr_formset = PersonalTbrRegistrationBookFormSet(
                    _registration_draft_querydict(draft.get("tbr", {})), prefix="tbr-books"
                )
                if not tbr_formset.is_valid():
                    raise ValidationError("Your Personal TBR is no longer valid. Review it and try again.")
            form.save_profile_discord_username()
            enrollment, created, reactivated = activate_participation(
                month=month,
                participant=participant,
                actor=request.user,
                origin=MonthEnrollment.Origin.SELF,
            )
            form.save_answers(enrollment)
            if month.tbr_enabled and not locked_tbr:
                replace_draft_personal_tbr(enrollment=enrollment, books=tbr_formset.book_values())
                confirm_personal_tbr(enrollment=enrollment)
    except (ChallengeMonth.DoesNotExist, ValidationError) as exc:
        message = "This Challenge is no longer available." if isinstance(exc, ChallengeMonth.DoesNotExist) else "; ".join(exc.messages)
        messages.error(request, message)
        return redirect("challenge-register", group_slug=group_slug, month_pk=month_pk)
    request.session.pop(draft_key, None)
    if created:
        messages.success(request, f"You are registered for {month.name}.")
    elif reactivated:
        messages.success(request, f"You are registered again for {month.name}.")
    return redirect(month)


@login_required
@sensitive_variables("token")
def personal_tbr_catalog(request, group_slug, month_pk):
    month = get_object_or_404(ChallengeMonth.objects.select_related("group"), pk=month_pk, group__slug=group_slug)
    participant = membership_for(request.user, month.group)
    if request.user.is_superuser or not participant:
        return JsonResponse({"ok": False, "message": "Registration membership is required."}, status=403)
    if not month.tbr_enabled or not month.registration_is_open or month.status not in {ChallengeMonth.Status.UPCOMING, ChallengeMonth.Status.ACTIVE}:
        return JsonResponse({"ok": False, "message": "Personal TBR registration is not currently available."}, status=409)
    existing = MonthEnrollment.objects.filter(month=month, participant=participant).first()
    if existing and (existing.is_active or getattr(getattr(existing, "personal_tbr", None), "confirmed_at", None)):
        return JsonResponse({"ok": False, "message": "This Personal TBR is already locked."}, status=409)
    if request.method != "POST":
        return JsonResponse({"ok": False, "message": "A POST request is required."}, status=405)
    try:
        token = get_reader_hardcover_token(request.user)
        action = request.POST.get("action")
        if action == "smart":
            value = request.POST.get("input", "").strip()
            looks_like_url = "://" in value or "hardcover.app" in value.casefold()
            if looks_like_url:
                result, cached = lookup_hardcover_url(token, value)
                if result.get("edition_required"):
                    return JsonResponse({"ok": True, "lookup_type": "book", "result": result, "cached": cached})
                prepared = _prepare_personal_tbr_edition(token, result)
                if prepared.get("manual_required"):
                    return JsonResponse({"ok": True, **prepared})
                return JsonResponse({"ok": True, "lookup_type": "edition", "result": prepared["result"], "cached": cached})
            results, cached = search_books(token, value)
            return JsonResponse({"ok": True, "lookup_type": "search", "results": results, "cached": cached})
        if action == "search":
            results, cached = search_books(token, request.POST.get("query", ""))
            return JsonResponse({"ok": True, "results": results, "cached": cached})
        if action == "editions":
            return JsonResponse({"ok": True, "editions": list_book_editions(token, request.POST.get("book_id", ""))})
        if action == "edition":
            selected, cached = lookup_edition(token, request.POST.get("edition_id", ""))
            prepared = _prepare_personal_tbr_edition(token, selected)
            return JsonResponse({"ok": True, **prepared, "cached": cached})
        return JsonResponse({"ok": False, "message": "Unknown catalog action."}, status=400)
    except ReaderHardcoverUnavailable as exc:
        return JsonResponse({"ok": False, "message": str(exc)}, status=503)
    except (HardcoverConnectionError, HardcoverLinkError, TokenDecryptionError, TypeError, ValueError) as exc:
        return JsonResponse({"ok": False, "message": str(exc)}, status=400)


def _prepare_personal_tbr_edition(token, selected):
    scoring, _method = resolve_scoring_edition(token, selected)
    if not scoring:
        return {"manual_required": True, "message": "Hardcover does not have a usable page count for this edition. Continue with manual entry."}
    selected_record = CatalogEdition.objects.get(provider="hardcover", provider_edition_id=selected["edition_id"])
    scoring_record = CatalogEdition.objects.get(provider="hardcover", provider_edition_id=scoring["edition_id"])
    selected["pages"] = scoring["pages"]
    selected["catalog_selection"] = signing.dumps(
        {"selected": selected_record.pk, "scoring": scoring_record.pk},
        salt="northbound.personal-tbr-selection",
    )
    return {"result": selected}


@login_required
def challenge_registration_edit(request, group_slug, month_pk):
    month = get_object_or_404(ChallengeMonth.objects.select_related("group"), pk=month_pk, group__slug=group_slug)
    participant = membership_for(request.user, month.group)
    if request.user.is_superuser or not participant:
        return HttpResponseForbidden("An active normal Group membership is required.")
    enrollment = get_object_or_404(MonthEnrollment.objects.select_related("month"), month=month, participant=participant, is_active=True)
    if not enrollment.can_reader_edit_registration_answers():
        messages.info(request, "Your registration responses are locked.")
        return redirect(month)
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    form = ChallengeRegistrationForm(request.POST if request.method == "POST" else None, month=month, profile=profile, enrollment=enrollment)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            form.save_profile_discord_username()
            form.save_answers(enrollment)
        messages.success(request, "Your registration responses were updated.")
        return redirect(month)
    return render(request, "core/challenge_registration.html", {
        "month": month,
        "form": form,
        "editing": True,
        "answers_editable": True,
    })


@login_required
def challenge_registration_detail(request, group_slug, month_pk, enrollment_pk):
    enrollment = get_object_or_404(
        MonthEnrollment.objects.select_related("month__group", "participant__user", "participant__user__northbound_profile"),
        pk=enrollment_pk,
        month_id=month_pk,
        month__group__slug=group_slug,
    )
    month = enrollment.month
    if not can_view_challenge_registration_answers(request.user, month):
        return HttpResponseForbidden("Challenge registration-planning visibility is required.")
    questions = list(month.signup_questions.all())
    answer_map = {answer.question_id: answer.value for answer in enrollment.signup_answers.all()}
    rows = []
    for question in questions:
        value = answer_map.get(question.pk, "")
        display_value = ", ".join(value) if isinstance(value, list) else value
        rows.append({"question": question, "value": display_value})
    correction_form = None
    if request.user.is_superuser:
        correction_form = ChallengeRegistrationForm(
            request.POST if request.method == "POST" else None,
            month=month,
            profile=enrollment.participant.user.northbound_profile,
            enrollment=enrollment,
            include_discord=False,
        )
        if request.method == "POST" and correction_form.is_valid():
            with transaction.atomic():
                correction_form.save_answers(enrollment)
                AuditEvent.objects.create(
                    actor=request.user,
                    group=month.group,
                    action="registration.answers_admin_corrected",
                    object_type="MonthEnrollment",
                    object_id=str(enrollment.pk),
                    summary=f"Administratively corrected registration answers for {enrollment.participant.display_name} in {month.name}; answer values were not recorded in audit activity.",
                )
            messages.success(request, "Registration answers were administratively corrected.")
            return redirect("challenge-registration-detail", group_slug=group_slug, month_pk=month.pk, enrollment_pk=enrollment.pk)
    return render(request, "core/challenge_registration_detail.html", {
        "month": month,
        "enrollment": enrollment,
        "rows": rows,
        "correction_form": correction_form,
    })


@login_required
def challenge_withdraw(request, group_slug, month_pk):
    month = get_object_or_404(ChallengeMonth.objects.select_related("group"), pk=month_pk, group__slug=group_slug)
    participant = membership_for(request.user, month.group)
    if request.user.is_superuser or not participant:
        return HttpResponseForbidden("An active normal Group membership is required to withdraw.")
    enrollment = get_object_or_404(MonthEnrollment, month=month, participant=participant, is_active=True)
    if month.status not in {ChallengeMonth.Status.UPCOMING, ChallengeMonth.Status.ACTIVE}:
        messages.error(request, "Self-withdrawal is no longer available. Contact a Host for a roster correction.")
        return redirect(month)
    if request.method == "POST":
        deactivate_participation(
            enrollment=enrollment,
            actor=request.user,
            reason=MonthEnrollment.InactiveReason.WITHDRAWN,
        )
        messages.success(request, f"You withdrew from {month.name}. Your Challenge history was preserved.")
        return redirect(month)
    return render(request, "core/confirm_remove.html", {
        "eyebrow": "Challenge Participation",
        "title": f"Withdraw from {month.name}?",
        "description": "You will leave the current roster. Existing submissions, pages, staffing history, and team history will be preserved.",
        "cancel_url": month.get_absolute_url(),
        "action_label": "Withdraw",
        "hide_reason": True,
    })


@login_required
def submission_create(request, group_slug, month_pk):
    month = get_object_or_404(ChallengeMonth, pk=month_pk, group__slug=group_slug)
    participant = membership_for(request.user, month.group)
    if not participant and not request.user.is_superuser:
        return HttpResponseForbidden("You are not a member of this reading group.")
    if month.status != ChallengeMonth.Status.ACTIVE:
        messages.error(request, "This Challenge is not Active for submissions.")
        return redirect(month)
    if request.user.is_superuser and not participant:
        raise Http404("A Platform Owner must also have a group membership to submit books.")
    if not MonthEnrollment.objects.filter(month=month, participant=participant, is_active=True).exists():
        messages.error(request, "You are not enrolled in this Challenge. Ask a Host to add you to the Challenge or one of its teams.")
        return redirect(month)
    form = BookSubmissionForm(request.POST or None, month=month)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            submission = form.save(commit=False)
            submission.month = month
            submission.participant = participant
            if submission.verification_method != BookSubmission.VerificationMethod.MANUAL:
                submission.status = BookSubmission.Status.APPROVED
                submission.approved_pages = submission.metadata_pages
                submission.reviewed_at = timezone.now()
            submission.full_clean()
            submission.save()
            form.save_theme_claims(submission)
            submission.recalculate_score()
            synchronize_submission(submission)
            AuditEvent.objects.create(actor=request.user, group=month.group, action="submission.created", object_type="BookSubmission", object_id=str(submission.pk), summary=f"Submitted {submission.title}")
        if submission.status == BookSubmission.Status.APPROVED:
            messages.success(request, "Book submitted and verified through Hardcover.")
        else:
            messages.success(request, "Book submitted for moderator review.")
        return redirect(month)
    connection = HardcoverConnection.objects.filter(group=month.group, is_valid=True).exists()
    return render(request, "core/submission_create.html", {"form": form, "month": month, "hardcover_available": connection})


@login_required
def submission_catalog(request, group_slug, month_pk):
    month = get_object_or_404(ChallengeMonth.objects.select_related("group"), pk=month_pk, group__slug=group_slug)
    participant = membership_for(request.user, month.group)
    if month.status != ChallengeMonth.Status.ACTIVE:
        return JsonResponse({"ok": False, "message": "This Challenge is not Active for submissions."}, status=409)
    if not participant or not MonthEnrollment.objects.filter(month=month, participant=participant, is_active=True).exists():
        return JsonResponse({"ok": False, "message": "You are not enrolled in this challenge month."}, status=403)
    if request.method != "POST":
        return JsonResponse({"ok": False, "message": "A POST request is required."}, status=405)
    connection = HardcoverConnection.objects.filter(group=month.group, is_valid=True).first()
    if not connection:
        return JsonResponse({"ok": False, "message": "Hardcover catalog search is not connected for this group. Use manual entry."}, status=503)
    try:
        token = decrypt_token(connection.encrypted_token)
        action = request.POST.get("action")
        if action == "search":
            results, cached = search_books(token, request.POST.get("query", ""))
            return JsonResponse({"ok": True, "results": results, "cached": cached})
        if action == "link":
            result, cached = lookup_hardcover_url(token, request.POST.get("url", ""))
            return JsonResponse({"ok": True, "result": result, "cached": cached})
        if action == "editions":
            editions = list_book_editions(token, request.POST.get("book_id", ""))
            return JsonResponse({"ok": True, "editions": editions})
        if action == "edition":
            selected, cached = lookup_edition(token, request.POST.get("edition_id", ""))
            scoring, method = resolve_scoring_edition(token, selected)
            if not scoring:
                return JsonResponse({"ok": True, "manual_required": True, "message": "Hardcover does not have a usable ebook or print page count for this edition. Continue with manual entry and add a reference link."})
            selected_record = CatalogEdition.objects.get(provider="hardcover", provider_edition_id=selected["edition_id"])
            scoring_record = CatalogEdition.objects.get(provider="hardcover", provider_edition_id=scoring["edition_id"])
            signed_selection = signing.dumps({"selected": selected_record.pk, "scoring": scoring_record.pk, "method": method}, salt="northbound.catalog-selection")
            selected["pages"] = scoring["pages"]
            selected["catalog_selection"] = signed_selection
            selected["scoring_format"] = scoring["format"]
            selected["scoring_source_url"] = scoring["source_url"]
            selected["verification_label"] = "Hardcover audiobook with ebook/print equivalent" if method == BookSubmission.VerificationMethod.HARDCOVER_AUDIO else "Hardcover edition"
            return JsonResponse({"ok": True, "result": selected, "cached": cached})
        return JsonResponse({"ok": False, "message": "Unknown catalog action."}, status=400)
    except (HardcoverConnectionError, HardcoverLinkError, TokenDecryptionError, TypeError, ValueError) as exc:
        return JsonResponse({"ok": False, "message": str(exc)}, status=400)


def _botm_view_context(request, month):
    membership = membership_for(request.user, month.group)
    if not request.user.is_superuser and not membership:
        return None
    if not can_view_challenge(request.user, month):
        raise Http404("Challenge not found.")
    can_manage = can_operate_challenge(request.user, month)
    if not month.botm_enabled and not can_manage:
        return None
    return {"membership": membership, "can_manage": can_manage}


@login_required
def botm_list(request, group_slug, month_pk):
    month = get_object_or_404(ChallengeMonth.objects.select_related("group"), pk=month_pk, group__slug=group_slug)
    access = _botm_view_context(request, month)
    if access is None:
        return HttpResponseForbidden("Book of the Month is not available for this Challenge.")
    books = list(month.botm_books.select_related("catalog_book", "catalog_edition").order_by("position", "pk"))
    active_books = [book for book in books if not book.is_retired]
    botm_reader = build_botm_reader_presentation(
        month=month,
        participant=access["membership"],
        books=active_books,
    )
    return render(request, "core/botm_list.html", {
        "month": month,
        "active_books": active_books,
        "retired_books": [book for book in books if book.is_retired] if access["can_manage"] else [],
        "botm_reader": botm_reader,
        "can_change": access["can_manage"] and month_is_configurable(month),
        **access,
    })


@login_required
def personal_tbr_detail(request, group_slug, month_pk):
    month = get_object_or_404(ChallengeMonth.objects.select_related("group"), pk=month_pk, group__slug=group_slug)
    membership = membership_for(request.user, month.group)
    if request.user.is_superuser or not membership:
        return HttpResponseForbidden("A Reader may view only their own Personal TBR.")
    personal_tbr = get_object_or_404(
        PersonalTBR.objects.select_related("enrollment"),
        enrollment__month=month,
        enrollment__participant=membership,
        confirmed_at__isnull=False,
    )
    presentation = build_personal_tbr_reader_presentation(personal_tbr=personal_tbr)
    return render(request, "core/personal_tbr_detail.html", {
        "month": month,
        "personal_tbr": personal_tbr,
        "personal_tbr_reader": presentation,
        "books": presentation["books"],
    })


@login_required
def botm_book_create(request, group_slug, month_pk):
    month = get_object_or_404(ChallengeMonth.objects.select_related("group"), pk=month_pk, group__slug=group_slug)
    if not can_operate_challenge(request.user, month):
        return HttpResponseForbidden("Only a current Host or Platform Owner may add Book of the Month titles.")
    if reject_locked_month(request, month, "add a Book of the Month title"):
        return redirect("botm-list", group_slug=group_slug, month_pk=month.pk)
    form = BotmBookForm(request.POST or None, month=month)
    if request.method == "POST" and form.is_valid():
        try:
            book = add_botm_book(month=month, actor=request.user, values=form.service_values())
        except ValidationError as exc:
            _add_validation_error(form, exc)
        else:
            messages.success(request, f"{book.title_snapshot} was added to Book of the Month.")
            return redirect("botm-list", group_slug=group_slug, month_pk=month.pk)
    hardcover_available = HardcoverConnection.objects.filter(group=month.group, is_valid=True).exists()
    return render(request, "core/botm_book_form.html", {
        "month": month, "form": form, "title": "Add BOTM Book", "hardcover_available": hardcover_available,
    })


@login_required
def botm_catalog(request, group_slug, month_pk):
    month = get_object_or_404(ChallengeMonth.objects.select_related("group"), pk=month_pk, group__slug=group_slug)
    if not can_operate_challenge(request.user, month):
        return JsonResponse({"ok": False, "message": "BOTM configuration authority is required."}, status=403)
    if not month_is_configurable(month):
        return JsonResponse({"ok": False, "message": "This Challenge is read-only."}, status=409)
    if request.method != "POST":
        return JsonResponse({"ok": False, "message": "A POST request is required."}, status=405)
    connection = HardcoverConnection.objects.filter(group=month.group, is_valid=True).first()
    if not connection:
        return JsonResponse({"ok": False, "message": "Hardcover catalog search is not connected for this Group. Use manual entry."}, status=503)
    try:
        token = decrypt_token(connection.encrypted_token)
        action = request.POST.get("action")
        if action == "search":
            results, cached = search_books(token, request.POST.get("query", ""))
            return JsonResponse({"ok": True, "results": results, "cached": cached})
        if action == "link":
            result, cached = lookup_hardcover_url(token, request.POST.get("url", ""))
            return JsonResponse({"ok": True, "result": result, "cached": cached})
        if action == "editions":
            return JsonResponse({"ok": True, "editions": list_book_editions(token, request.POST.get("book_id", ""))})
        if action == "edition":
            selected, cached = lookup_edition(token, request.POST.get("edition_id", ""))
            scoring, method = resolve_scoring_edition(token, selected)
            if not scoring:
                return JsonResponse({"ok": True, "manual_required": True, "message": "Hardcover does not have a usable page count for this edition. Continue with manual entry."})
            selected_record = CatalogEdition.objects.get(provider="hardcover", provider_edition_id=selected["edition_id"])
            scoring_record = CatalogEdition.objects.get(provider="hardcover", provider_edition_id=scoring["edition_id"])
            selected["pages"] = scoring["pages"]
            selected["catalog_selection"] = signing.dumps(
                {"selected": selected_record.pk, "scoring": scoring_record.pk, "method": method},
                salt="northbound.catalog-selection",
            )
            selected["scoring_format"] = scoring["format"]
            selected["verification_label"] = "Hardcover edition"
            return JsonResponse({"ok": True, "result": selected, "cached": cached})
        return JsonResponse({"ok": False, "message": "Unknown catalog action."}, status=400)
    except (HardcoverConnectionError, HardcoverLinkError, TokenDecryptionError, TypeError, ValueError) as exc:
        return JsonResponse({"ok": False, "message": str(exc)}, status=400)


@login_required
def botm_book_edit(request, group_slug, month_pk, pk):
    book = get_object_or_404(BotmBook.objects.select_related("month__group", "catalog_book"), pk=pk, month_id=month_pk, month__group__slug=group_slug)
    if not can_operate_challenge(request.user, book.month):
        return HttpResponseForbidden("Only a current Host or Platform Owner may edit Book of the Month titles.")
    if reject_locked_month(request, book.month, "edit a Book of the Month title"):
        return redirect("botm-list", group_slug=group_slug, month_pk=month_pk)
    form = BotmBookForm(request.POST or None, instance=book, month=book.month)
    if request.method == "POST" and form.is_valid():
        try:
            update_botm_book(month=book.month, book=book, actor=request.user, values=form.service_values())
        except ValidationError as exc:
            _add_validation_error(form, exc)
        else:
            messages.success(request, "Book of the Month title updated.")
            return redirect("botm-list", group_slug=group_slug, month_pk=month_pk)
    return render(request, "core/botm_book_form.html", {"month": book.month, "book": book, "form": form, "title": "Edit BOTM Book", "hardcover_available": False})


@login_required
def botm_book_retire(request, group_slug, month_pk, pk):
    book = get_object_or_404(BotmBook.objects.select_related("month__group"), pk=pk, month_id=month_pk, month__group__slug=group_slug)
    if not can_operate_challenge(request.user, book.month):
        return HttpResponseForbidden("Only a current Host or Platform Owner may retire Book of the Month titles.")
    if reject_locked_month(request, book.month, "retire a Book of the Month title"):
        return redirect("botm-list", group_slug=group_slug, month_pk=month_pk)
    if request.method == "POST":
        try:
            retire_botm_book(month=book.month, book=book, actor=request.user)
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
        else:
            messages.success(request, f"{book.title_snapshot} was retired.")
        return redirect("botm-list", group_slug=group_slug, month_pk=month_pk)
    return render(request, "core/confirm_remove.html", {"eyebrow": "Book of the Month", "title": f"Retire {book.title_snapshot}?", "description": "The configured snapshots will be preserved and its position will become available.", "cancel_url": reverse("botm-list", args=[group_slug, month_pk]), "action_label": "Retire", "hide_reason": True})


@login_required
def botm_book_reactivate(request, group_slug, month_pk, pk):
    book = get_object_or_404(BotmBook.objects.select_related("month__group"), pk=pk, month_id=month_pk, month__group__slug=group_slug)
    if not can_operate_challenge(request.user, book.month):
        return HttpResponseForbidden("Only a current Host or Platform Owner may reactivate Book of the Month titles.")
    if reject_locked_month(request, book.month, "reactivate a Book of the Month title"):
        return redirect("botm-list", group_slug=group_slug, month_pk=month_pk)
    form = BotmReactivateForm(request.POST or None, initial={"position": book.position})
    if request.method == "POST" and form.is_valid():
        try:
            reactivate_botm_book(month=book.month, book=book, actor=request.user, position=form.cleaned_data["position"])
        except ValidationError as exc:
            _add_validation_error(form, exc)
        else:
            messages.success(request, f"{book.title_snapshot} was reactivated.")
            return redirect("botm-list", group_slug=group_slug, month_pk=month_pk)
    return render(request, "core/botm_reactivate.html", {"month": book.month, "book": book, "form": form})


@login_required
def botm_book_delete(request, group_slug, month_pk, pk):
    book = get_object_or_404(BotmBook.objects.select_related("month__group"), pk=pk, month_id=month_pk, month__group__slug=group_slug)
    if not can_operate_challenge(request.user, book.month):
        return HttpResponseForbidden("Only a current Host or Platform Owner may delete unused Book of the Month titles.")
    if reject_locked_month(request, book.month, "delete a Book of the Month title"):
        return redirect("botm-list", group_slug=group_slug, month_pk=month_pk)
    if request.method == "POST":
        try:
            delete_unused_botm_book(month=book.month, book=book, actor=request.user)
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
        else:
            messages.success(request, f"{book.title_snapshot} was deleted.")
        return redirect("botm-list", group_slug=group_slug, month_pk=month_pk)
    return render(request, "core/confirm_remove.html", {"eyebrow": "Book of the Month", "title": f"Delete {book.title_snapshot}?", "description": "This removes only this unused BOTM configuration. It does not delete its catalog book or edition.", "cancel_url": reverse("botm-list", args=[group_slug, month_pk]), "action_label": "Delete", "hide_reason": True})


@login_required
def botm_match_list(request, group_slug, month_pk):
    month = get_object_or_404(ChallengeMonth.objects.select_related("group"), pk=month_pk, group__slug=group_slug)
    if not can_operate_challenge(request.user, month):
        return HttpResponseForbidden("Only a current Host or Platform Owner may view BOTM match history.")
    matches = month.botm_matches.select_related(
        "botm_book", "participant", "submission", "reviewer"
    ).order_by("status", "botm_book__position", "participant__display_name", "pk")
    completion_awards = list(BotmCompletionAward.objects.filter(month=month).select_related(
        "participant"
    ).prefetch_related("configured_books").order_by("participant__display_name", "-qualified_at", "pk"))
    for award in completion_awards:
        frozen_book_ids = [snapshot.botm_book_id for snapshot in award.configured_books.all()]
        award.current_completed_count = BotmMatch.objects.filter(
            month=month,
            participant=award.participant,
            botm_book_id__in=frozen_book_ids,
            status=BotmMatch.Status.CONFIRMED,
            is_qualifying=True,
        ).count()
    return render(request, "core/botm_match_list.html", {
        "month": month, "matches": matches, "completion_awards": completion_awards,
    })


@login_required
def botm_match_synchronize(request, group_slug, month_pk):
    month = get_object_or_404(ChallengeMonth.objects.select_related("group"), pk=month_pk, group__slug=group_slug)
    if not can_operate_challenge(request.user, month):
        return HttpResponseForbidden("Only a current Host or Platform Owner may synchronize BOTM matches.")
    if request.method != "POST":
        return HttpResponseForbidden("A POST request is required.")
    synchronize_challenge(month)
    messages.success(request, "BOTM match history synchronized.")
    return redirect("botm-match-list", group_slug=group_slug, month_pk=month_pk)


@login_required
def botm_match_review(request, group_slug, month_pk, pk, decision):
    match = get_object_or_404(
        BotmMatch.objects.select_related("month__group", "botm_book", "submission", "participant"),
        pk=pk,
        month_id=month_pk,
        month__group__slug=group_slug,
    )
    if not can_operate_challenge(request.user, match.month):
        return HttpResponseForbidden("Only a current Host or Platform Owner may adjudicate BOTM matches.")
    if request.method != "POST":
        return HttpResponseForbidden("A POST request is required.")
    status = {
        "confirm": BotmMatch.Status.CONFIRMED,
        "reject": BotmMatch.Status.REJECTED,
    }.get(decision)
    if status is None:
        raise Http404("Unknown BOTM match decision.")
    try:
        adjudicate_match(match=match, actor=request.user, decision=status)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    else:
        messages.success(request, f"BOTM match {decision}ed.")
    return redirect("botm-match-list", group_slug=group_slug, month_pk=month_pk)


@login_required
def personal_tbr_match_list(request, group_slug, month_pk):
    month = get_object_or_404(ChallengeMonth.objects.select_related("group"), pk=month_pk, group__slug=group_slug)
    if not challenge_review_scope(request.user, month):
        return HttpResponseForbidden("A current Challenge review staffing assignment is required.")
    visible_submissions = scope_reviewable_submissions(request.user, month, month.submissions.all())
    matches = month.personal_tbr_matches.filter(submission__in=visible_submissions).select_related(
        "personal_tbr_book", "participant", "submission", "reviewer",
    ).order_by("status", "participant__display_name", "personal_tbr_book__position", "pk")
    return render(request, "core/personal_tbr_match_list.html", {"month": month, "matches": matches})


@login_required
def personal_tbr_match_synchronize(request, group_slug, month_pk):
    month = get_object_or_404(ChallengeMonth.objects.select_related("group"), pk=month_pk, group__slug=group_slug)
    if not challenge_review_scope(request.user, month):
        return HttpResponseForbidden("A current Challenge review staffing assignment is required.")
    if request.method != "POST":
        return HttpResponseForbidden("A POST request is required.")
    submissions = scope_reviewable_submissions(request.user, month, month.submissions.all()).order_by("pk")
    for submission in submissions:
        synchronize_personal_tbr_submission(submission)
    messages.success(request, "Personal TBR match history synchronized for your review scope.")
    return redirect("personal-tbr-match-list", group_slug=group_slug, month_pk=month_pk)


@login_required
def personal_tbr_match_review(request, group_slug, month_pk, pk, decision):
    match = get_object_or_404(
        PersonalTBRMatch.objects.select_related("month__group", "personal_tbr_book", "submission", "participant"),
        pk=pk, month_id=month_pk, month__group__slug=group_slug,
    )
    if not can_review_submission(request.user, match.submission):
        return HttpResponseForbidden("You do not have review authority for this Personal TBR match.")
    if request.method != "POST":
        return HttpResponseForbidden("A POST request is required.")
    status = {
        "confirm": PersonalTBRMatch.Status.CONFIRMED,
        "reject": PersonalTBRMatch.Status.REJECTED,
    }.get(decision)
    if status is None:
        raise Http404("Unknown Personal TBR match decision.")
    try:
        adjudicate_personal_tbr_match(match=match, actor=request.user, decision=status)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    else:
        messages.success(request, f"Personal TBR match {decision}ed.")
    return redirect("personal-tbr-match-list", group_slug=group_slug, month_pk=month_pk)


def _games_view_context(request, month):
    membership = membership_for(request.user, month.group)
    if not request.user.is_superuser and not membership:
        return None
    if not can_view_challenge(request.user, month):
        raise Http404("Challenge not found.")
    can_manage = can_operate_challenge(request.user, month)
    if not month.games_enabled and not can_manage:
        return None
    return {"membership": membership, "can_manage": can_manage}


@login_required
def game_list(request, group_slug, month_pk):
    month = get_object_or_404(ChallengeMonth.objects.select_related("group"), pk=month_pk, group__slug=group_slug)
    access = _games_view_context(request, month)
    if access is None:
        return HttpResponseForbidden("Games are not available for this Challenge.")
    games = month.games.annotate(application_count=Count("reward_applications"))
    return render(request, "core/game_list.html", {
        "month": month,
        "active_games": games.filter(is_active=True),
        "retired_games": games.filter(is_active=False),
        "can_change": access["can_manage"] and month_is_configurable(month),
        **access,
    })


@login_required
def game_create(request, group_slug, month_pk):
    month = get_object_or_404(ChallengeMonth.objects.select_related("group"), pk=month_pk, group__slug=group_slug)
    if not can_operate_challenge(request.user, month):
        return HttpResponseForbidden("Only a current Host or Platform Owner may add Games.")
    if reject_locked_month(request, month, "add a Game"):
        return redirect("game-list", group_slug=group_slug, month_pk=month.pk)
    with timezone.override(ZoneInfo(month.group.timezone)):
        form = GameForm(request.POST or None, month=month)
        if request.method == "POST" and form.is_valid():
            game = form.save(commit=False)
            game.month = month
            game.full_clean()
            game.save()
            AuditEvent.objects.create(
                actor=request.user, group=month.group, action="game.created",
                object_type="Game", object_id=str(game.pk), summary=f"Created Game {game.name} for {month.name}.",
            )
            messages.success(request, f"{game.name} was added.")
            return redirect("game-detail", group_slug=group_slug, month_pk=month.pk, pk=game.pk)
        return render(request, "core/game_form.html", {"month": month, "form": form, "title": "Add Game"})


@login_required
def game_detail(request, group_slug, month_pk, pk):
    game = get_object_or_404(Game.objects.select_related("month__group"), pk=pk, month_id=month_pk, month__group__slug=group_slug)
    access = _games_view_context(request, game.month)
    if access is None:
        return HttpResponseForbidden("Games are not available for this Challenge.")
    applications = []
    if access["can_manage"]:
        applications = game.reward_applications.select_related("applied_by", "voided_by").prefetch_related(
            "recipients__participant"
        ).order_by("-applied_at", "-pk")
    return render(request, "core/game_detail.html", {
        "month": game.month,
        "game": game,
        "applications": applications,
        "can_change": access["can_manage"] and month_is_configurable(game.month),
        "apply_unavailable_reason": (
            game_reward_application_unavailable_reason(game) if access["can_manage"] else ""
        ),
        **access,
    })


def _add_validation_error(form, exc):
    for message in exc.messages:
        form.add_error(None, message)


@login_required
def game_reward_apply(request, group_slug, month_pk, pk):
    game = get_object_or_404(
        Game.objects.select_related("month__group"),
        pk=pk,
        month_id=month_pk,
        month__group__slug=group_slug,
    )
    if not can_operate_challenge(request.user, game.month):
        return HttpResponseForbidden("Only a current Host or Platform Owner may apply Game rewards.")
    unavailable_reason = game_reward_application_unavailable_reason(game)
    initial = {"idempotency_key": uuid4()}
    form = GameRewardApplyForm(
        request.POST or None,
        game=game,
        final_apply=request.method == "POST" and request.POST.get("step") == "apply",
        initial=initial,
    )
    if request.method == "POST" and form.is_valid():
        payload = {
            "game": game,
            "actor": request.user,
            "target_type": form.cleaned_data["target_type"],
            "target_participant": form.cleaned_data.get("target_participant"),
            "target_team": form.cleaned_data.get("target_team"),
        }
        if request.POST.get("step") == "apply":
            try:
                application, _ = apply_game_reward(
                    **payload,
                    amount=form.cleaned_data["amount"],
                    reason=form.cleaned_data["reason"],
                    idempotency_key=form.cleaned_data["idempotency_key"],
                )
            except ValidationError as exc:
                _add_validation_error(form, exc)
            else:
                messages.success(
                    request,
                    f"Applied {application.amount} reward pages to {application.target_label}.",
                )
                return redirect(
                    "game-detail", group_slug=group_slug, month_pk=month_pk, pk=game.pk
                )
        else:
            try:
                recipients, target_label = preview_game_reward(**payload)
            except ValidationError as exc:
                _add_validation_error(form, exc)
            else:
                return render(request, "core/game_reward_confirm.html", {
                    "month": game.month,
                    "game": game,
                    "form": form,
                    "recipients": recipients,
                    "target_label": target_label,
                    "target_type_label": dict(form.fields["target_type"].choices)[form.cleaned_data["target_type"]],
                })
    return render(request, "core/game_reward_form.html", {
        "month": game.month,
        "game": game,
        "form": form,
        "unavailable_reason": unavailable_reason,
    })


@login_required
def game_reward_void(request, group_slug, month_pk, pk, application_pk):
    application = get_object_or_404(
        GameRewardApplication.objects.select_related(
            "game__month__group", "applied_by", "voided_by"
        ).prefetch_related("recipients__participant"),
        pk=application_pk,
        game_id=pk,
        game__month_id=month_pk,
        game__month__group__slug=group_slug,
    )
    if not can_operate_challenge(request.user, application.game.month):
        return HttpResponseForbidden("Only a current Host or Platform Owner may void Game rewards.")
    form = GameRewardVoidForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            voided = void_game_reward(
                application=application,
                actor=request.user,
                reason=form.cleaned_data["reason"],
            )
        except ValidationError as exc:
            _add_validation_error(form, exc)
        else:
            messages.success(request, f"Voided the reward for {voided.target_label}.")
            return redirect(
                "game-detail", group_slug=group_slug, month_pk=month_pk, pk=application.game.pk
            )
    return render(request, "core/game_reward_void.html", {
        "month": application.game.month,
        "game": application.game,
        "application": application,
        "form": form,
    })


@login_required
def game_edit(request, group_slug, month_pk, pk):
    game = get_object_or_404(Game.objects.select_related("month__group"), pk=pk, month_id=month_pk, month__group__slug=group_slug)
    if not can_operate_challenge(request.user, game.month):
        return HttpResponseForbidden("Only a current Host or Platform Owner may edit Games.")
    if reject_locked_month(request, game.month, "edit a Game"):
        return redirect("game-detail", group_slug=group_slug, month_pk=month_pk, pk=game.pk)
    with timezone.override(ZoneInfo(game.month.group.timezone)):
        form = GameForm(request.POST or None, instance=game, month=game.month)
        if request.method == "POST" and form.is_valid():
            changed_fields = list(form.changed_data)
            game = form.save(commit=False)
            game.full_clean()
            game.save()
            if changed_fields:
                AuditEvent.objects.create(
                    actor=request.user, group=game.month.group, action="game.updated",
                    object_type="Game", object_id=str(game.pk),
                    summary=f"Updated Game {game.name}; changed: {', '.join(changed_fields)}.",
                )
            messages.success(request, f"{game.name} was updated.")
            return redirect("game-detail", group_slug=group_slug, month_pk=month_pk, pk=game.pk)
        return render(request, "core/game_form.html", {"month": game.month, "game": game, "form": form, "title": f"Edit {game.name}"})


@login_required
def game_active_toggle(request, group_slug, month_pk, pk):
    game = get_object_or_404(Game.objects.select_related("month__group"), pk=pk, month_id=month_pk, month__group__slug=group_slug)
    if not can_operate_challenge(request.user, game.month):
        return HttpResponseForbidden("Only a current Host or Platform Owner may change Game status.")
    if reject_locked_month(request, game.month, "change a Game's status"):
        return redirect("game-detail", group_slug=group_slug, month_pk=month_pk, pk=game.pk)
    action = "reactivated" if not game.is_active else "retired"
    if request.method == "POST":
        game.is_active = not game.is_active
        game.save(update_fields=["is_active", "updated_at"])
        AuditEvent.objects.create(
            actor=request.user, group=game.month.group, action=f"game.{action}",
            object_type="Game", object_id=str(game.pk), summary=f"{action.title()} Game {game.name}.",
        )
        messages.success(request, f"{game.name} was {action}.")
        return redirect("game-detail", group_slug=group_slug, month_pk=month_pk, pk=game.pk)
    return render(request, "core/confirm_remove.html", {
        "eyebrow": "Game Status", "title": f"{action.title()} {game.name}?",
        "description": (
            "Reactivation allows future reward application without changing historical rewards."
            if action == "reactivated" else
            "Retirement prevents future reward application but preserves all rewards, recipients, provenance, and scores."
        ),
        "cancel_url": reverse("game-detail", kwargs={"group_slug": group_slug, "month_pk": month_pk, "pk": game.pk}),
        "action_label": action.title(), "hide_reason": True,
    })


@login_required
def game_delete(request, group_slug, month_pk, pk):
    game = get_object_or_404(Game.objects.select_related("month__group"), pk=pk, month_id=month_pk, month__group__slug=group_slug)
    if not can_operate_challenge(request.user, game.month):
        return HttpResponseForbidden("Only a current Host or Platform Owner may delete unused Games.")
    if reject_locked_month(request, game.month, "delete a Game"):
        return redirect("game-detail", group_slug=group_slug, month_pk=month_pk, pk=game.pk)
    if game.reward_applications.exists():
        messages.error(request, "Games with reward history cannot be deleted. Retire this Game instead.")
        return redirect("game-detail", group_slug=group_slug, month_pk=month_pk, pk=game.pk)
    if request.method == "POST":
        month = game.month
        game_name = game.name
        try:
            game.delete()
        except ProtectedError:
            messages.error(request, "This Game now has reward history and cannot be deleted.")
            return redirect("game-detail", group_slug=group_slug, month_pk=month_pk, pk=pk)
        AuditEvent.objects.create(
            actor=request.user, group=month.group, action="game.deleted",
            object_type="Game", object_id=str(pk), summary=f"Deleted unused Game {game_name} from {month.name}.",
        )
        messages.success(request, f"Unused Game {game_name} was deleted.")
        return redirect("game-list", group_slug=group_slug, month_pk=month.pk)
    return render(request, "core/confirm_remove.html", {
        "eyebrow": "Unused Game", "title": f"Delete {game.name}?",
        "description": "This permanently deletes the unused Game. Games with reward history cannot be deleted.",
        "cancel_url": reverse("game-detail", kwargs={"group_slug": group_slug, "month_pk": month_pk, "pk": game.pk}),
        "action_label": "Confirm Delete", "hide_reason": True,
    })


@login_required
def review_queue(request, group_slug, month_pk):
    month = get_object_or_404(ChallengeMonth, pk=month_pk, group__slug=group_slug)
    review_scope = challenge_review_scope(request.user, month)
    if not review_scope:
        return HttpResponseForbidden("A current Challenge review staffing assignment is required.")
    if month.status not in REVIEWABLE_MONTH_STATUSES:
        messages.error(request, f"{month.get_status_display()} Challenges cannot be reviewed.")
        return redirect(month)
    submissions = scope_reviewable_submissions(
        request.user,
        month,
        month.submissions.filter(
            Q(status=BookSubmission.Status.PENDING) | Q(theme_claims__status=ThemeClaim.Status.PENDING),
            is_removed=False,
        ),
    ).select_related(
        "participant", "catalog_book", "catalog_edition"
    ).prefetch_related("theme_claims__theme").distinct()
    scope_name, team_ids = review_scope
    scope_label = "Entire Challenge" if scope_name == "challenge" else ", ".join(month.teams.filter(pk__in=team_ids).values_list("name", flat=True))
    return render(request, "core/review_queue.html", {"month": month, "submissions": submissions, "review_scope_label": scope_label})


@login_required
def submission_review(request, group_slug, month_pk, pk):
    submission = get_object_or_404(BookSubmission.objects.select_related(
        "month__group", "participant", "catalog_book", "catalog_edition"
    ), pk=pk, month_id=month_pk, month__group__slug=group_slug)
    if not can_review_submission(request.user, submission):
        return HttpResponseForbidden("You do not have review authority for this submission.")
    if submission.month.status not in REVIEWABLE_MONTH_STATUSES:
        messages.error(request, f"{submission.month.get_status_display()} Challenges cannot be reviewed.")
        return redirect(submission.month)
    form = SubmissionReviewForm(request.POST or None, instance=submission)
    ClaimFormSet = inlineformset_factory(BookSubmission, ThemeClaim, form=ThemeClaimReviewForm, extra=0, can_delete=False)
    claim_formset = ClaimFormSet(request.POST or None, instance=submission, prefix="claims")
    claims_are_valid = claim_formset.is_valid() if submission.theme_claims.exists() else True
    if request.method == "POST" and form.is_valid() and claims_are_valid:
        with transaction.atomic():
            reviewed = form.save(commit=False)
            claims = claim_formset.save(commit=False)
            reviewed = apply_submission_review(reviewed, claims, request.user, timezone.now())
            AuditEvent.objects.create(actor=request.user, group=submission.month.group, action=f"submission.{reviewed.status}", object_type="BookSubmission", object_id=str(reviewed.pk), summary=f"{reviewed.get_status_display()}: {reviewed.title}; approved pages: {reviewed.approved_pages or 'none'}")
        messages.success(request, "Review saved.")
        return redirect("review-queue", group_slug=group_slug, month_pk=month_pk)
    return render(request, "core/submission_review.html", {"form": form, "claim_formset": claim_formset, "submission": submission})


@login_required
def theme_list(request, group_slug, month_pk):
    month = get_object_or_404(ChallengeMonth.objects.select_related("group"), pk=month_pk, group__slug=group_slug)
    membership = membership_for(request.user, month.group)
    if not request.user.is_superuser and not membership:
        return HttpResponseForbidden("You are not a member of this reading group.")
    if not can_view_challenge(request.user, month):
        raise Http404("Challenge not found.")
    host_access = can_operate_challenge(request.user, month)
    themes = month.themes.all() if host_access else month.themes.filter(is_active=True, is_visible=True)
    return render(request, "core/theme_list.html", {"month": month, "themes": themes, "can_manage": month_is_configurable(month) and host_access})


@login_required
def theme_create(request, group_slug, month_pk):
    month = get_object_or_404(ChallengeMonth.objects.select_related("group"), pk=month_pk, group__slug=group_slug)
    if not can_operate_challenge(request.user, month):
        return HttpResponseForbidden("A current Host assignment is required.")
    if reject_locked_month(request, month, "add a theme"):
        return redirect(month)
    form = MonthThemeForm(request.POST or None, month=month, initial={"starts_on": month.starts_on, "ends_on": month.ends_on})
    if request.method == "POST" and form.is_valid():
        theme = form.save(commit=False)
        theme.month = month
        theme.full_clean()
        theme.save()
        messages.success(request, f"{theme.name} was added.")
        return redirect("theme-list", group_slug=group_slug, month_pk=month_pk)
    return render(request, "core/form_page.html", {"form": form, "title": "Add Theme", "eyebrow": month.name})


@login_required
def theme_edit(request, group_slug, month_pk, pk):
    theme = get_object_or_404(MonthTheme.objects.select_related("month__group"), pk=pk, month_id=month_pk, month__group__slug=group_slug)
    if not can_operate_challenge(request.user, theme.month):
        return HttpResponseForbidden("A current Host assignment is required.")
    if reject_locked_month(request, theme.month, "edit a theme"):
        return redirect(theme.month)
    form = MonthThemeForm(request.POST or None, instance=theme)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f"{theme.name} was updated.")
        return redirect("theme-list", group_slug=group_slug, month_pk=month_pk)
    return render(request, "core/form_page.html", {"form": form, "title": f"Edit {theme.name}", "eyebrow": theme.month.name})
