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
from django.db.models import Count, OuterRef, Prefetch, Q, Subquery, Sum
from django.forms import inlineformset_factory
from django.http import FileResponse, Http404, HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.defaultfilters import filesizeformat
from django.urls import reverse
from django.utils import timezone
import secrets
import csv
import json
import sqlite3
import zipfile
import os
import signal
import threading
from pathlib import Path
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .forms import AccountProfileForm, BookSubmissionForm, ChallengeAnnouncementForm, ChallengeCreateForm, ChallengeFloaterAssignmentForm, ChallengeGeneralSettingsForm, ChallengeHostAssignmentForm, ChallengeMonthForm, ChallengeRegistrationForm, ChallengeRegistrationSettingsForm, ChallengeScheduleForm, ChallengeSignupQuestionFormSet, ChallengeTeamLeaderAssignmentForm, CompetitionVisibilityForm, FirstRunSetupForm, GroupAccessCodeForm, GroupCreateForm, GroupEditForm, GroupJoinForm, HardcoverConnectionForm, MemberCreateForm, MembershipPermissionsForm, MembershipRoleForm, MonthEnrollmentForm, MonthParticipantEditForm, MonthThemeForm, PlatformAccountIdentityForm, PlatformBackupSettingsForm, PlatformOwnerAcceptanceForm, PlatformOwnerInvitationForm, PlatformOwnerStatusForm, PlatformSettingsForm, ProgressCheckpointFormSet, PublicRegistrationForm, RootAuthenticationForm, SubmissionReviewForm, TeamAssignmentForm, TeamForm, ThemeClaimReviewForm
from .integrations.hardcover import HardcoverConnectionError, HardcoverLinkError, list_book_editions, lookup_edition, lookup_hardcover_url, resolve_scoring_edition, search_books, test_catalog_connection
from .integrations.secrets import TokenDecryptionError, decrypt_token, encrypt_token
from .models import AuditEvent, BookSubmission, CatalogEdition, ChallengeMonth, ChallengeSignupAnswer, ChallengeSignupQuestion, ChallengeStaffAssignment, HardcoverConnection, Membership, MonthEnrollment, MonthTheme, PlatformBackupSettings, PlatformOwnerInvitation, ProgressCheckpoint, ProgressCheckpointResult, ReadingGroup, Team, TeamAssignment, ThemeClaim, UserProfile, audit_action_label, hash_platform_owner_invitation_token, safe_audit_summary
from .permissions import can_configure_competition_visibility, can_manage_challenge_announcements, can_manage_challenge_hosts, can_manage_group, can_manage_group_announcements, can_manage_months, can_manage_participants, can_manage_permissions, can_operate_challenge, can_review_challenge, can_review_submission, can_transition_challenge, can_view_access_code, can_view_challenge, can_view_reader_scores, can_view_team_standings, challenge_review_scope, membership_for, scope_reviewable_submissions, visible_challenges_for
from .backups import automatic_backup_directory, create_stored_backup, list_automatic_backups, list_stored_backups, next_scheduled_backup, pending_restore_path, stage_restore, stage_stored_restore, stored_backup_path
from .platform_config import get_platform_settings, get_platform_timezone
from .maintenance import AUDIT_RETENTION_YEARS, audit_prune_preview, cleanup_disposable_cache, disposable_cache_usage, optimize_sqlite_database, prune_audit_history, storage_overview
from .maintenance_lock import MaintenanceBusyError
from .system_status import build_system_status
from .review_attention import needs_attention_summary
from .participation import activate_participation, assign_participant_to_team, deactivate_participation, end_team_assignment
from .reader_planning import historical_reader_planning_data


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
    return render(request, "core/account.html", {"form": form})


@login_required
def my_stats(request):
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    group_approved_filter = Q(challenge_months__submissions__participant__user=request.user, challenge_months__submissions__status=BookSubmission.Status.APPROVED, challenge_months__submissions__is_removed=False)
    approved_submissions = BookSubmission.objects.filter(
        participant__user=request.user,
        status=BookSubmission.Status.APPROVED,
        is_removed=False,
    )
    totals = approved_submissions.aggregate(books=Count("id"), pages=Sum("final_scored_pages"))
    groups = ReadingGroup.objects.filter(memberships__user=request.user).distinct().annotate(
        reader_books=Count("challenge_months__submissions", filter=group_approved_filter, distinct=True),
        reader_pages=Sum("challenge_months__submissions__final_scored_pages", filter=group_approved_filter),
    ).order_by("name")

    month_ids = set(MonthEnrollment.objects.filter(participant__user=request.user).values_list("month_id", flat=True))
    month_ids.update(BookSubmission.objects.filter(participant__user=request.user, is_removed=False).values_list("month_id", flat=True))
    months = list(ChallengeMonth.objects.filter(pk__in=month_ids).select_related("group").annotate(
        reader_books=Count("submissions", filter=Q(submissions__participant__user=request.user, submissions__status=BookSubmission.Status.APPROVED, submissions__is_removed=False), distinct=True),
        reader_pages=Sum("submissions__final_scored_pages", filter=Q(submissions__participant__user=request.user, submissions__status=BookSubmission.Status.APPROVED, submissions__is_removed=False)),
    ).order_by("-starts_on", "group__name"))
    assignments = TeamAssignment.objects.filter(
        month_id__in=month_ids,
        participant__user=request.user,
        ended_at__isnull=True,
    ).select_related("team")
    team_by_month = {assignment.month_id: assignment.team for assignment in assignments}
    for month in months:
        month.reader_team = team_by_month.get(month.pk)

    submissions = BookSubmission.objects.filter(participant__user=request.user, is_removed=False).select_related(
        "month__group", "catalog_edition", "scoring_catalog_edition"
    ).order_by("-completed_on", "-submitted_at")
    return render(request, "core/my_stats.html", {
        "approved_books": totals["books"] or 0,
        "approved_pages": totals["pages"] or 0,
        "group_count": groups.count(),
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
    action = "Deactivate" if group.is_active else "Reactivate"
    if request.method == "POST":
        group.is_active = not group.is_active
        group.save(update_fields=["is_active"])
        new_state = "reactivated" if group.is_active else "deactivated"
        reason = request.POST.get("reason", "").strip()
        summary = f"{new_state.capitalize()} reading group {group.name}."
        if reason:
            summary += f" Reason: {reason}"
        AuditEvent.objects.create(
            actor=request.user,
            group=group,
            action=f"group.{new_state}",
            object_type="ReadingGroup",
            object_id=str(group.pk),
            summary=summary,
        )
        messages.success(request, f"{group.name} was {new_state}. Its URL and history were preserved.")
        return redirect("config-group-detail", group_slug=group.slug)
    return render(request, "core/confirm_remove.html", {
        "eyebrow": "Group Lifecycle",
        "title": f"{action} {group.name}?",
        "description": (
            "The group will be hidden from normal account access, while its stable URL, memberships, "
            "challenge months, submissions, teams, scoring, and audit history remain stored."
            if group.is_active else
            "The group will return to normal account access with its stable URL, memberships, and history unchanged."
        ),
        "cancel_url": reverse("config-group-detail", kwargs={"group_slug": group.slug}),
        "action_label": action,
    })


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
    if owner.pk == request.user.pk:
        messages.error(request, "You cannot deactivate or reactivate your own Platform Owner account.")
        return redirect("platform-owner-list")

    action = "Deactivate" if owner.is_active else "Reactivate"
    form = PlatformOwnerStatusForm(request.POST or None, owner=request.user)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            locked_owners = list(
                get_user_model().objects.select_for_update().filter(is_superuser=True).only("pk", "is_active")
            )
            owner = next((locked_owner for locked_owner in locked_owners if locked_owner.pk == pk), None)
            if owner is None:
                raise Http404
            if owner.is_active:
                active_owner_count = sum(locked_owner.is_active for locked_owner in locked_owners)
                if active_owner_count <= 1:
                    messages.error(request, "Northbound must retain at least one active Platform Owner.")
                    return redirect("platform-owner-list")
            owner.is_active = not owner.is_active
            owner.save(update_fields=["is_active"])
            new_state = "reactivated" if owner.is_active else "deactivated"
            AuditEvent.objects.create(
                actor=request.user,
                action=f"platform.owner_{new_state}",
                object_type="User",
                object_id=str(owner.pk),
                summary=f"{new_state.capitalize()} Platform Owner {owner.username}.",
            )
        messages.success(request, f"{owner.username} was {new_state} as a Platform Owner.")
        return redirect("platform-owner-list")
    return render(request, "core/platform_owner_status.html", {
        "owner": owner,
        "form": form,
        "action": action,
    })


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
    action = "deactivate" if account_user.is_active else "reactivate"
    if request.method == "POST":
        account_user.is_active = not account_user.is_active
        account_user.save(update_fields=["is_active"])
        AuditEvent.objects.create(
            actor=request.user,
            action=f"account.{action}d",
            object_type="User",
            object_id=str(account_user.pk),
            summary=f"{action.title()}d account {account_user.username}; group history was preserved.",
        )
        messages.success(request, f"{account_user.username} was {action}d.")
        return redirect("config-user-detail", pk=account_user.pk)
    return render(request, "core/confirm_remove.html", {
        "eyebrow": "Platform User Management",
        "title": f"{action.title()} {account_user.username}?",
        "description": "The account's groups, submissions, and history will be preserved.",
        "cancel_url": reverse("config-user-detail", kwargs={"pk": account_user.pk}),
        "action_label": f"Confirm {action.title()}",
        "hide_reason": True,
    })


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
    return render(request, "core/platform_settings.html")


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


def health(request):
    return JsonResponse({"ok": True, "restore_pending": pending_restore_path().exists()})


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
    participants = group.memberships.filter(user__is_superuser=False).select_related("user").annotate(
        approved_books=Count("submissions", filter=Q(submissions__status=BookSubmission.Status.APPROVED, submissions__is_removed=False), distinct=True),
        approved_pages=Sum("submissions__final_scored_pages", filter=Q(submissions__status=BookSubmission.Status.APPROVED, submissions__is_removed=False)),
    )
    return render(request, "core/participant_list.html", {"group": group, "participants": participants, "can_manage": can_manage_participants(request.user, group), "can_manage_permissions": can_manage_permissions(request.user, group), "can_remove": can_manage_participants(request.user, group)})


@login_required
def participant_detail(request, group_slug, pk):
    group = get_object_or_404(ReadingGroup, slug=group_slug, is_active=True)
    viewer_membership = membership_for(request.user, group)
    if not request.user.is_superuser and not viewer_membership:
        return HttpResponseForbidden("You are not a member of this reading group.")
    participant = get_object_or_404(Membership.objects.select_related("user"), pk=pk, group=group, user__is_superuser=False)
    approved_submissions = participant.submissions.filter(status=BookSubmission.Status.APPROVED, is_removed=False)
    totals = approved_submissions.aggregate(approved_books=Count("id"), approved_pages=Sum("final_scored_pages"))
    participated_month_ids = set(participant.month_enrollments.filter(month__group=group).values_list("month_id", flat=True))
    participated_month_ids.update(participant.submissions.filter(month__group=group, is_removed=False).values_list("month_id", flat=True))
    months = list(group.challenge_months.filter(pk__in=participated_month_ids).annotate(
        participant_books=Count("submissions", filter=Q(submissions__participant=participant, submissions__status=BookSubmission.Status.APPROVED, submissions__is_removed=False), distinct=True),
        participant_pages=Sum("submissions__final_scored_pages", filter=Q(submissions__participant=participant, submissions__status=BookSubmission.Status.APPROVED, submissions__is_removed=False)),
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
        "approved_pages": totals["approved_pages"] or 0,
        "months_participated": len(months),
        "months": months if detailed_access else [],
        "detailed_access": detailed_access,
        "can_view_discord_username": can_view_discord_username,
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
    months = months.annotate(
        approved_books=Count("submissions", filter=Q(submissions__status=BookSubmission.Status.APPROVED, submissions__is_removed=False), distinct=True),
        approved_pages=Sum("submissions__final_scored_pages", filter=Q(submissions__status=BookSubmission.Status.APPROVED, submissions__is_removed=False)),
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
    for team in teams:
        team.can_view_standings = can_view_team_standings(request.user, month, team=team)
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
        role=ChallengeStaffAssignment.Role.TEAM_LEADER,
        ended_at__isnull=True,
    ).select_related("membership").order_by("membership__display_name", "membership_id"))
    leader_ids = {assignment.membership_id for assignment in current_leaders}
    roster = list(team.assignments.filter(
        ended_at__isnull=True,
        participant__month_enrollments__month=month,
        participant__month_enrollments__is_active=True,
    ).select_related("participant", "participant__user", "participant__user__northbound_profile").distinct())
    participant_ids = [assignment.participant_id for assignment in roster]
    planning_access = can_view_challenge_registration_answers(request.user, month)
    standings_access = can_view_team_standings(request.user, month, team=team)
    score_access = can_view_reader_scores(request.user, month, team=team)
    planning_by_participant = historical_reader_planning_data(
        month=month,
        participant_ids=participant_ids,
    ) if planning_access else {}
    score_by_participant = {}
    if score_access:
        score_by_participant = {
            row["participant_id"]: row
            for row in BookSubmission.objects.filter(
                month=month,
                participant_id__in=participant_ids,
                status=BookSubmission.Status.APPROVED,
                is_removed=False,
            ).values("participant_id").annotate(
                base_pages=Sum("approved_pages"),
                modifier_pages=Sum("bonus_pages"),
                total_pages=Sum("final_scored_pages"),
            )
        }
    discord_staff_access = (
        request.user.is_superuser
        or can_operate_challenge(request.user, month)
        or (membership and membership.role in {Membership.Role.OWNER, Membership.Role.MODERATOR})
    )
    for assignment in roster:
        assignment.is_team_leader = assignment.participant_id in leader_ids
        assignment.planning = planning_by_participant.get(assignment.participant_id)
        if score_access:
            scores = score_by_participant.get(assignment.participant_id, {})
            assignment.base_pages = scores.get("base_pages") or 0
            assignment.modifier_pages = scores.get("modifier_pages") or 0
            assignment.total_pages = scores.get("total_pages") or 0
        profile = getattr(assignment.participant.user, "northbound_profile", None)
        assignment.can_view_discord_username = (
            discord_staff_access
            or assignment.participant.user_id == request.user.id
            or bool(profile and profile.discord_username_is_public)
        )
    allowed_sorts = {"reader"}
    if planning_access:
        allowed_sorts.update({"average", "last", "completed"})
    if score_access:
        allowed_sorts.update({"base", "modifier", "total"})
    requested_sort = request.GET.get("sort", "reader")
    sort_key = requested_sort if requested_sort in allowed_sorts else "reader"
    direction = request.GET.get("direction", "asc")
    direction = direction if direction in {"asc", "desc"} else "asc"

    def sort_value(assignment):
        if sort_key == "average":
            return assignment.planning.average_pages
        if sort_key == "last":
            return assignment.planning.last_challenge_pages
        if sort_key == "completed":
            return assignment.planning.completed_challenges
        if sort_key == "base":
            return assignment.base_pages
        if sort_key == "modifier":
            return assignment.modifier_pages
        if sort_key == "total":
            return assignment.total_pages
        return assignment.participant.display_name.casefold()

    def sorted_role_group(assignments):
        if sort_key == "reader":
            return sorted(
                assignments,
                key=lambda assignment: (assignment.participant.display_name.casefold(), assignment.participant_id),
                reverse=direction == "desc",
            )
        available = [assignment for assignment in assignments if sort_value(assignment) is not None]
        unavailable = [assignment for assignment in assignments if sort_value(assignment) is None]
        available.sort(key=lambda assignment: (assignment.participant.display_name.casefold(), assignment.participant_id))
        available.sort(key=sort_value, reverse=direction == "desc")
        unavailable.sort(key=lambda assignment: (assignment.participant.display_name.casefold(), assignment.participant_id))
        return available + unavailable

    roster = sorted_role_group([assignment for assignment in roster if assignment.is_team_leader]) + sorted_role_group(
        [assignment for assignment in roster if not assignment.is_team_leader]
    )
    sort_labels = {
        "reader": "Reader",
        "average": "Avg Pages",
        "last": "Last Challenge",
        "completed": "Completed",
        "base": "Base",
        "modifier": "Modifier",
        "total": "Total",
    }
    sort_headers = {}
    for key in sort_labels:
        if key not in allowed_sorts:
            continue
        next_direction = "desc" if sort_key == key and direction == "asc" else "asc"
        sort_headers[key] = {
            "label": sort_labels[key],
            "url": f"?sort={key}&direction={next_direction}",
            "active": sort_key == key,
            "direction": direction if sort_key == key else "",
        }
    sort_options = [{"value": key, "label": sort_labels[key]} for key in sort_labels if key in allowed_sorts]

    host_access = can_operate_challenge(request.user, month)
    mutable = month_is_configurable(month)
    return render(request, "core/team_detail.html", {
        "group": month.group,
        "month": month,
        "team": team,
        "current_leaders": current_leaders,
        "roster": roster,
        "team_total": team.approved_pages if standings_access else None,
        "team_base_total": sum(assignment.base_pages for assignment in roster) if score_access else None,
        "can_view_team_standings": standings_access,
        "can_view_reader_scores": score_access,
        "can_view_planning_data": planning_access,
        "sort_key": sort_key,
        "sort_direction": direction,
        "sort_headers": sort_headers,
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
        "lifecycle_targets": lifecycle_transition_targets(month),
        "signup_question_count": month.signup_questions.count(),
        "checkpoint_count": month.progress_checkpoints.count(),
    })


@login_required
def challenge_signup_settings(request, group_slug, month_pk):
    month = get_object_or_404(ChallengeMonth.objects.select_related("group"), pk=month_pk, group__slug=group_slug)
    if not can_configure_challenge_registration(request.user, month):
        return HttpResponseForbidden("Challenge registration configuration authority is required.")
    questions = list(month.signup_questions.all())
    schema_locked = month.signup_schema_is_locked
    lifecycle_locked = not month_is_configurable(month)
    if schema_locked or lifecycle_locked:
        return render(request, "core/challenge_signup_settings.html", {
            "month": month,
            "questions": questions,
            "schema_locked": schema_locked,
            "lifecycle_locked": lifecycle_locked,
            "configuration_read_only": True,
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
    settings_form = ChallengeRegistrationSettingsForm(request.POST or None, instance=month, prefix="settings")
    question_formset = ChallengeSignupQuestionFormSet(
        request.POST or None,
        initial=initial,
        prefix="questions",
    )
    if request.method == "POST" and settings_form.is_valid() and question_formset.is_valid():
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
                if locked_month.progress_checkpoints.exclude(
                    evaluation_state=ProgressCheckpoint.EvaluationState.PENDING
                ).exists():
                    messages.error(request, "Checkpoint configuration was locked because evaluation has begun.")
                    return redirect("challenge-progress-checkpoints", group_slug=group_slug, month_pk=month.pk)
                active_forms = [form for form in formset.forms if form.cleaned_data and not form.cleaned_data.get("DELETE")]
                active_forms.sort(key=lambda form: form.cleaned_data.get("ORDER") or 0)
                locked_month.progress_checkpoints.all().delete()
                for position, checkpoint_form in enumerate(active_forms, start=1):
                    ProgressCheckpoint.objects.create(
                        month=locked_month,
                        scheduled_at=checkpoint_form.cleaned_data["scheduled_at"],
                        threshold_percentage=checkpoint_form.cleaned_data["threshold_percentage"],
                        progress_basis=checkpoint_form.cleaned_data["progress_basis"],
                        target_basis=checkpoint_form.cleaned_data["target_basis"],
                        fixed_target_pages=checkpoint_form.cleaned_data["fixed_target_pages"],
                        position=position,
                    )
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
        return HttpResponseForbidden("Participant management permission is required.")
    form = MemberCreateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        membership = form.save(group)
        AuditEvent.objects.create(actor=request.user, group=group, action="membership.created", object_type="Membership", object_id=str(membership.pk), summary=f"Added {membership.display_name} as {membership.get_role_display()}")
        messages.success(request, f"{membership.display_name} can now sign in with the temporary password.")
        return redirect("group-detail", group_slug=group.slug)
    return render(request, "core/form_page.html", {"form": form, "title": "Add Participant", "eyebrow": group.name})


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
    comparison_teams = []
    for team in teams:
        team.can_view_standings = can_view_team_standings(request.user, month, team=team)
        if team.can_view_standings:
            team.visible_approved_pages = team.approved_pages
            comparison_teams.append(team)
    max_team_pages = max((team.visible_approved_pages for team in comparison_teams), default=0)
    for team in comparison_teams:
        team.chart_percent = round((team.visible_approved_pages / max_team_pages) * 100, 1) if max_team_pages else 0
    theme_access = can_operate_challenge(request.user, month)
    theme_queryset = month.themes.all() if theme_access else month.themes.filter(is_active=True, is_visible=True)
    theme_preview = list(theme_queryset[:3])
    theme_more_count = max(theme_queryset.count() - len(theme_preview), 0)
    visible_submissions = month.submissions.filter(is_removed=False).select_related("participant", "participant__user", "participant__user__northbound_profile").prefetch_related("theme_claims__theme")
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
    form = ChallengeRegistrationForm(
        request.POST if request.method == "POST" else None,
        month=month,
        profile=profile,
        enrollment=existing,
        answers_editable=answers_editable,
    )
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                form.save_profile_discord_username()
                enrollment, created, reactivated = activate_participation(
                    month=month,
                    participant=participant,
                    actor=request.user,
                    origin=MonthEnrollment.Origin.SELF,
                )
                form.save_answers(enrollment)
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
            return redirect(month)
        if created:
            messages.success(request, f"You are registered for {month.name}.")
        elif reactivated:
            messages.success(request, f"You are registered again for {month.name}.")
        return redirect(month)
    return render(request, "core/challenge_registration.html", {
        "month": month,
        "form": form,
        "is_reregistration": bool(existing),
        "answers_editable": answers_editable,
    })


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
    ).select_related("participant").prefetch_related("theme_claims__theme").distinct()
    scope_name, team_ids = review_scope
    scope_label = "Entire Challenge" if scope_name == "challenge" else ", ".join(month.teams.filter(pk__in=team_ids).values_list("name", flat=True))
    return render(request, "core/review_queue.html", {"month": month, "submissions": submissions, "review_scope_label": scope_label})


@login_required
def submission_review(request, group_slug, month_pk, pk):
    submission = get_object_or_404(BookSubmission.objects.select_related("month__group", "participant"), pk=pk, month_id=month_pk, month__group__slug=group_slug)
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
            reviewed.reviewed_by = request.user
            reviewed.reviewed_at = timezone.now()
            reviewed.full_clean()
            reviewed.save()
            claims = claim_formset.save(commit=False)
            for claim in claims:
                claim.reviewed_by = request.user
                claim.reviewed_at = timezone.now()
                claim.approved_bonus_pages = claim.theme.bonus_pages if claim.status == ThemeClaim.Status.APPROVED and reviewed.status == BookSubmission.Status.APPROVED else 0
                claim.full_clean()
                claim.save()
            if reviewed.status != BookSubmission.Status.APPROVED:
                reviewed.theme_claims.update(status=ThemeClaim.Status.REJECTED, approved_bonus_pages=0, reviewed_by=request.user, reviewed_at=timezone.now())
            reviewed.recalculate_score()
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
