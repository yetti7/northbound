from django.contrib import messages
from django.conf import settings
from django.core import signing
from django.contrib.auth import get_user_model, login
from django.contrib.auth.views import LoginView, PasswordChangeView
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db import connection
from django.db.models import Count, Prefetch, Q, Sum
from django.forms import inlineformset_factory
from django.http import FileResponse, Http404, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
import secrets
import json
import sqlite3
import tempfile
import zipfile
import os
import signal
import threading
from pathlib import Path
from datetime import datetime

from .forms import AccountProfileForm, BookSubmissionForm, ChallengeMonthForm, FirstRunSetupForm, GroupAccessCodeForm, GroupCreateForm, GroupEditForm, GroupJoinForm, HardcoverConnectionForm, MemberCreateForm, MembershipPermissionsForm, MembershipRoleForm, MonthEnrollmentForm, MonthParticipantEditForm, MonthThemeForm, PlatformBackupSettingsForm, PlatformOwnerAcceptanceForm, PlatformOwnerInvitationForm, PublicRegistrationForm, RootAuthenticationForm, SubmissionReviewForm, TeamAssignmentForm, TeamForm, TeamStatsVisibilityForm, ThemeClaimReviewForm
from .integrations.hardcover import HardcoverConnectionError, HardcoverLinkError, list_book_editions, lookup_edition, lookup_hardcover_url, resolve_scoring_edition, search_books, test_catalog_connection
from .integrations.secrets import TokenDecryptionError, decrypt_token, encrypt_token
from .models import AuditEvent, BookSubmission, CatalogEdition, ChallengeMonth, HardcoverConnection, Membership, MonthEnrollment, MonthTheme, PlatformBackupSettings, PlatformOwnerInvitation, ReadingGroup, Team, TeamAssignment, ThemeClaim, UserProfile, hash_platform_owner_invitation_token
from .permissions import can_manage_announcements, can_manage_group, can_manage_months, can_manage_participants, can_manage_permissions, can_manage_teams, can_remove, can_review, can_view_access_code, can_view_team_stats, membership_for
from .backups import automatic_backup_directory, list_automatic_backups, pending_restore_path, stage_restore


CONFIGURABLE_MONTH_STATUSES = {ChallengeMonth.Status.DRAFT, ChallengeMonth.Status.OPEN}
REVIEWABLE_MONTH_STATUSES = {ChallengeMonth.Status.OPEN, ChallengeMonth.Status.CLOSED}


def month_is_configurable(month):
    return month.status in CONFIGURABLE_MONTH_STATUSES


def reject_locked_month(request, month, action="change this month"):
    if month_is_configurable(month):
        return False
    messages.error(request, f"{month.get_status_display()} months are read-only. You cannot {action}.")
    return True


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
    assignments = TeamAssignment.objects.filter(month_id__in=month_ids, participant__user=request.user).select_related("team")
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
    }
    return render(request, "core/config_dashboard.html", context)


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
    memberships = account_user.reading_memberships.select_related("group").order_by("group__name")
    return render(request, "core/config_user_detail.html", {
        "account_user": account_user,
        "profile": profile,
        "memberships": memberships,
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


@login_required(login_url="config-login")
def config_audit(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Platform owner access is required.")
    events = AuditEvent.objects.select_related("actor", "group")[:200]
    return render(request, "core/config_audit.html", {"events": events})


@login_required(login_url="config-login")
def platform_settings(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Platform owner access is required.")
    return render(request, "core/platform_settings.html")


@login_required(login_url="config-login")
def platform_backups(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Platform owner access is required.")
    is_sqlite = connection.vendor == "sqlite"
    backup_settings = PlatformBackupSettings.load()
    backup_settings_form = PlatformBackupSettingsForm(request.POST or None, instance=backup_settings)
    if request.method == "POST" and backup_settings_form.is_valid():
        backup_settings_form.save()
        for expired_backup in list_automatic_backups()[backup_settings.retention_count:]:
            expired_backup.unlink(missing_ok=True)
        selected_days = ", ".join(dict(PlatformBackupSettings.Weekday.choices)[day] for day in backup_settings.weekdays)
        AuditEvent.objects.create(actor=request.user, action="platform.backup_settings_updated", object_type="PlatformBackupSettings", object_id=str(backup_settings.pk), summary=f"Updated automatic backups to {selected_days} at {backup_settings.backup_time}; retaining {backup_settings.retention_count}.")
        messages.success(request, "Automatic backup settings were updated.")
        return redirect("platform-backups")
    automatic_backups = [{"name": path.name, "size": path.stat().st_size, "modified": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.get_current_timezone())} for path in list_automatic_backups()]
    return render(request, "core/platform_backups.html", {
        "is_sqlite": is_sqlite,
        "restore_pending": pending_restore_path().exists() if is_sqlite else False,
        "web_restart_enabled": settings.NORTHBOUND_WEB_RESTART,
        "backup_settings_form": backup_settings_form,
        "automatic_backups": automatic_backups,
        "platform_timezone": settings.TIME_ZONE,
    })


@login_required(login_url="config-login")
def platform_backup_download(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Platform owner access is required.")
    if request.method != "POST":
        return redirect("platform-backups")
    if connection.vendor != "sqlite":
        messages.error(request, "In-app backup downloads currently support the standard SQLite deployment. Back up PostgreSQL with its native tools.")
        return redirect("platform-backups")

    created_at = timezone.now()
    temporary_directory = tempfile.TemporaryDirectory(prefix="northbound-backup-")
    sqlite_backup_path = Path(temporary_directory.name) / "northbound.sqlite3"
    connection.ensure_connection()
    destination = sqlite3.connect(sqlite_backup_path)
    try:
        connection.connection.backup(destination)
    finally:
        destination.close()

    archive = tempfile.TemporaryFile()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as backup_zip:
        backup_zip.write(sqlite_backup_path, "northbound.sqlite3")
        media_root = Path(settings.MEDIA_ROOT)
        if media_root.exists():
            for media_file in media_root.rglob("*"):
                if media_file.is_file():
                    backup_zip.write(media_file, Path("media") / media_file.relative_to(media_root))
        backup_zip.writestr("northbound-backup.json", json.dumps({
            "created_at": created_at.isoformat(),
            "database": "sqlite",
            "contents": ["northbound.sqlite3", "media/"],
        }, indent=2))
    temporary_directory.cleanup()
    archive.seek(0)
    AuditEvent.objects.create(
        actor=request.user,
        action="platform.backup_downloaded",
        object_type="PlatformBackup",
        summary="Generated and downloaded a platform backup containing SQLite data and uploaded media.",
    )
    filename = f"northbound-backup-{created_at:%Y%m%d-%H%M%S}.zip"
    return FileResponse(archive, as_attachment=True, filename=filename, content_type="application/zip")


@login_required(login_url="config-login")
def automatic_backup_download(request, filename):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Platform owner access is required.")
    if Path(filename).name != filename or not filename.startswith("northbound-automatic-") or not filename.endswith(".zip"):
        raise Http404
    backup_path = automatic_backup_directory() / filename
    if not backup_path.is_file():
        raise Http404
    return FileResponse(backup_path.open("rb"), as_attachment=True, filename=filename, content_type="application/zip")


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


@login_required(login_url="config-login")
def platform_restore_restart(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Platform owner access is required.")
    if not settings.NORTHBOUND_WEB_RESTART or connection.vendor != "sqlite" or not pending_restore_path().exists():
        messages.error(request, "A web-controlled restore restart is not available.")
        return redirect("platform-backups")
    error = ""
    if request.method == "POST":
        if not request.user.check_password(request.POST.get("current_password", "")):
            error = "Your current password is incorrect."
        elif request.POST.get("confirmation") != "RESTORE":
            error = "Enter RESTORE exactly to confirm."
        else:
            AuditEvent.objects.create(actor=request.user, action="platform.restore_restart_requested", object_type="PlatformBackup", summary="Requested a graceful application restart to apply the staged restore.")

            def stop_gunicorn():
                try:
                    master_pid = int(Path("/tmp/northbound-gunicorn.pid").read_text().strip())
                    os.kill(master_pid, signal.SIGTERM)
                except (OSError, ValueError):
                    pass

            threading.Timer(2, stop_gunicorn).start()
            return render(request, "core/platform_restore_restarting.html")
    return render(request, "core/platform_restore_restart.html", {"error": error})


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
    form = PublicRegistrationForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        login(request, user)
        messages.success(request, "Account created. You can create a reading group or join one with its access code.")
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
        return redirect("group-detail", group_slug=group.slug)
    return render(request, "core/group_create.html", {"form": form})


@login_required
def group_edit(request, group_slug):
    group = get_object_or_404(ReadingGroup, slug=group_slug, is_active=True)
    if not can_remove(request.user, group):
        return HttpResponseForbidden("Group owner or platform root access is required.")
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
            defaults={"role": Membership.Role.READER, "display_name": request.user.get_full_name() or request.user.username, "is_active": True},
        )
        if not created and membership.is_active:
            messages.info(request, "You are already a member of that reading group.")
        else:
            if not created:
                membership.is_active = True
                membership.role = Membership.Role.READER
                membership.save(update_fields=["is_active", "role"])
            AuditEvent.objects.create(actor=request.user, group=group, action="membership.joined", object_type="Membership", object_id=str(membership.pk), summary=f"{membership.display_name} joined using the group access code")
            messages.success(request, f"You joined {group.name} as a reader.")
        return redirect("group-detail", group_slug=group.slug)
    return render(request, "core/form_page.html", {"form": form, "title": "Join a Reading Group", "eyebrow": "Invitation Access"})


@login_required
def group_access_code(request, group_slug):
    group = get_object_or_404(ReadingGroup, slug=group_slug)
    if not can_view_access_code(request.user, group):
        return HttpResponseForbidden("You do not have permission to view this group access code.")
    can_manage_access_code = can_remove(request.user, group)
    if request.method == "POST" and not can_manage_access_code:
        return HttpResponseForbidden("Group owner or platform root access is required.")
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
    if not can_remove(request.user, group):
        return HttpResponseForbidden("Group owner or platform root access is required.")
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
    if not can_remove(request.user, group):
        return HttpResponseForbidden("Group owner or platform root access is required.")
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
    return render(request, "core/group_detail.html", {
        "group": group,
        "membership": membership,
        "can_manage_participants": can_manage_participants(request.user, group),
        "can_manage_months": can_manage_months(request.user, group),
        "can_edit_group": can_remove(request.user, group),
        "can_manage_announcements": can_manage_announcements(request.user, group),
        "can_view_access_code": can_view_access_code(request.user, group),
        "participant_count": group.memberships.filter(is_active=True, user__is_superuser=False).count(),
        "active_months": group.challenge_months.exclude(status=ChallengeMonth.Status.ARCHIVED),
        "active_month_count": group.challenge_months.exclude(status=ChallengeMonth.Status.ARCHIVED).count(),
    })


@login_required
def group_announcement_update(request, group_slug):
    group = get_object_or_404(ReadingGroup, slug=group_slug, is_active=True)
    if not can_manage_announcements(request.user, group):
        return HttpResponseForbidden("Announcement management permission is required.")
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
    return render(request, "core/participant_list.html", {"group": group, "participants": participants, "can_manage": can_manage_participants(request.user, group), "can_manage_permissions": can_manage_permissions(request.user, group), "can_remove": can_remove(request.user, group)})


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
        for assignment in TeamAssignment.objects.filter(month__group=group, participant=participant).select_related("team")
    }
    for month in months:
        month.participant_team = assignments.get(month.pk)
    detailed_access = request.user.is_superuser or participant.user_id == request.user.id or can_review(request.user, group)
    return render(request, "core/participant_detail.html", {
        "group": group,
        "participant": participant,
        "approved_books": totals["approved_books"] or 0,
        "approved_pages": totals["approved_pages"] or 0,
        "months_participated": len(months),
        "months": months if detailed_access else [],
        "detailed_access": detailed_access,
    })


@login_required
def participant_role_edit(request, group_slug, pk):
    group = get_object_or_404(ReadingGroup, slug=group_slug)
    if not can_remove(request.user, group):
        return HttpResponseForbidden("Group owner or platform root access is required.")
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
    viewing_archive = request.GET.get("archive") == "1"
    teams = month.teams.filter(is_archived=viewing_archive).prefetch_related("assignments__participant")
    mutable = month_is_configurable(month)
    return render(request, "core/team_list.html", {"group": group, "month": month, "teams": teams, "can_manage": mutable and can_manage_teams(request.user, group), "can_remove": mutable and can_remove(request.user, group), "can_view_team_stats": can_view_team_stats(request.user, month), "viewing_archive": viewing_archive, "archived_count": month.teams.filter(is_archived=True).count()})


@login_required
def team_stats_settings(request, group_slug, month_pk):
    month = get_object_or_404(ChallengeMonth.objects.select_related("group"), pk=month_pk, group__slug=group_slug)
    if not can_remove(request.user, month.group):
        return HttpResponseForbidden("Group owner or platform root access is required.")
    if reject_locked_month(request, month, "change its visibility"):
        return redirect(month)
    form = TeamStatsVisibilityForm(request.POST or None, instance=month)
    if request.method == "POST" and form.is_valid():
        previous = month.team_stats_visibility
        updated = form.save()
        AuditEvent.objects.create(actor=request.user, group=month.group, action="month.team_stats_visibility_changed", object_type="ChallengeMonth", object_id=str(month.pk), summary=f"Changed team-stat visibility from {previous} to {updated.team_stats_visibility} for {month.name}")
        messages.success(request, "Team comparison visibility updated.")
        return redirect(month)
    return render(request, "core/form_page.html", {"form": form, "title": "Visibility", "eyebrow": month.name})


@login_required
def participant_deactivate(request, group_slug, pk):
    group = get_object_or_404(ReadingGroup, slug=group_slug)
    if not can_remove(request.user, group):
        return HttpResponseForbidden("Group owner or platform root access is required.")
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
    assignment = get_object_or_404(TeamAssignment.objects.select_related("month__group", "participant", "team"), pk=pk, month_id=month_pk, month__group__slug=group_slug)
    if not can_remove(request.user, assignment.month.group):
        return HttpResponseForbidden("Group owner or platform root access is required.")
    if reject_locked_month(request, assignment.month, "change its team roster"):
        return redirect(assignment.month)
    if request.method == "POST":
        reason = request.POST.get("reason", "").strip()
        summary = f"Removed {assignment.participant.display_name} from {assignment.team.name} for {assignment.month.name}. Reason: {reason or 'Not provided'}"
        group = assignment.month.group
        month = assignment.month
        object_id = assignment.pk
        assignment.delete()
        AuditEvent.objects.create(actor=request.user, group=group, action="team_assignment.removed", object_type="TeamAssignment", object_id=str(object_id), summary=summary)
        messages.success(request, "Participant removed from the team. Their reading history was not deleted.")
        return redirect("team-list", group_slug=group.slug, month_pk=month.pk)
    return render(request, "core/confirm_remove.html", {"title": f"Remove {assignment.participant.display_name} from {assignment.team.name}?", "description": "The reader remains in the participant database and can be assigned to another team.", "cancel_url": reverse("team-list", kwargs={"group_slug": group_slug, "month_pk": month_pk})})


@login_required
def submission_remove(request, group_slug, month_pk, pk):
    submission = get_object_or_404(BookSubmission.objects.select_related("month__group", "participant"), pk=pk, month_id=month_pk, month__group__slug=group_slug, is_removed=False)
    if not can_remove(request.user, submission.month.group):
        return HttpResponseForbidden("Group owner or platform root access is required.")
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
        return HttpResponseForbidden("Group administrator access is required.")
    form = ChallengeMonthForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        month = form.save(commit=False)
        month.group = group
        month.save()
        AuditEvent.objects.create(actor=request.user, group=group, action="month.created", object_type="ChallengeMonth", object_id=str(month.pk), summary=f"Created {month.name}")
        return redirect(month)
    return render(request, "core/form_page.html", {"form": form, "title": "Create Challenge Month", "eyebrow": group.name})


@login_required
def month_edit(request, group_slug, pk):
    month = get_object_or_404(ChallengeMonth.objects.select_related("group"), pk=pk, group__slug=group_slug)
    if not can_manage_months(request.user, month.group):
        return HttpResponseForbidden("Group administrator access is required.")
    form = ChallengeMonthForm(request.POST or None, instance=month)
    if request.method == "POST" and form.is_valid():
        previous_status = month.status
        updated = form.save()
        AuditEvent.objects.create(actor=request.user, group=month.group, action="month.updated", object_type="ChallengeMonth", object_id=str(month.pk), summary=f"Updated {month.name}; status changed from {previous_status} to {updated.status}.")
        messages.success(request, "Challenge month updated.")
        return redirect(updated)
    return render(request, "core/month_edit.html", {"form": form, "month": month, "can_delete_draft": can_remove(request.user, month.group) and month.status == ChallengeMonth.Status.DRAFT})


@login_required
def month_delete(request, group_slug, pk):
    month = get_object_or_404(ChallengeMonth.objects.select_related("group"), pk=pk, group__slug=group_slug)
    if not can_remove(request.user, month.group):
        return HttpResponseForbidden("Group owner or platform root access is required.")
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
        return HttpResponseForbidden("Group administrator access is required.")
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
    approved_pages = month.submissions.filter(status=BookSubmission.Status.APPROVED, is_removed=False).aggregate(total=Sum("final_scored_pages"))["total"] or 0
    teams = list(month.teams.filter(is_archived=False).prefetch_related("assignments__participant"))
    max_team_pages = max((team.approved_pages for team in teams), default=0)
    for team in teams:
        team.chart_percent = round((team.approved_pages / max_team_pages) * 100, 1) if max_team_pages else 0
    reviewer_access = can_review(request.user, month.group)
    visible_submissions = month.submissions.filter(is_removed=False).select_related("participant", "participant__user", "participant__user__northbound_profile").prefetch_related("theme_claims__theme")
    if not reviewer_access:
        visible_submissions = visible_submissions.filter(participant=membership)
    context = {
        "month": month,
        "membership": membership,
        "approved_pages": approved_pages,
        "book_count": month.submissions.filter(is_removed=False).count(),
        "can_manage_months": can_manage_months(request.user, month.group),
        "can_manage_announcements": month_is_configurable(month) and can_manage_announcements(request.user, month.group),
        "can_manage_teams": month_is_configurable(month) and can_manage_teams(request.user, month.group),
        "can_review": month.status in REVIEWABLE_MONTH_STATUSES and can_review(request.user, month.group),
        "can_remove": month_is_configurable(month) and can_remove(request.user, month.group),
        "can_view_team_stats": can_view_team_stats(request.user, month),
        "is_enrolled": bool(membership and MonthEnrollment.objects.filter(month=month, participant=membership).exists()),
        "pending_count": month.submissions.filter(Q(status=BookSubmission.Status.PENDING) | Q(theme_claims__status=ThemeClaim.Status.PENDING), is_removed=False).distinct().count(),
        "teams": teams,
        "active_team_count": month.teams.filter(is_archived=False).count(),
        "visible_submissions": visible_submissions,
        "submission_heading": "Recent Submissions" if reviewer_access else "My Submissions",
    }
    return render(request, "core/month_detail.html", context)


@login_required
def month_announcement_update(request, group_slug, month_pk):
    month = get_object_or_404(ChallengeMonth.objects.select_related("group"), pk=month_pk, group__slug=group_slug)
    if not can_manage_announcements(request.user, month.group):
        return HttpResponseForbidden("Announcement management permission is required.")
    if reject_locked_month(request, month, "change its announcement"):
        return redirect(month)
    if request.method != "POST":
        return redirect(month)
    if month.announcement_mode != ChallengeMonth.AnnouncementMode.CUSTOM:
        messages.error(request, "Select Custom Announcement in Edit Month before editing it here.")
        return redirect(month)
    announcement = request.POST.get("announcement", "").strip()
    if not announcement:
        messages.error(request, "A custom month announcement cannot be blank.")
        return redirect(month)
    month.announcement = announcement
    month.save(update_fields=["announcement"])
    AuditEvent.objects.create(actor=request.user, group=month.group, action="month.announcement_updated", object_type="ChallengeMonth", object_id=str(month.pk), summary=f"Updated the announcement for {month.name}.")
    messages.success(request, "Month announcement updated.")
    return redirect(month)


@login_required
def team_create(request, group_slug, month_pk):
    month = get_object_or_404(ChallengeMonth, pk=month_pk, group__slug=group_slug)
    if not can_manage_teams(request.user, month.group):
        return HttpResponseForbidden("Group administrator access is required.")
    if reject_locked_month(request, month, "add a team"):
        return redirect(month)
    form = TeamForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        team = form.save(commit=False)
        team.month = month
        team.save()
        return redirect(month)
    return render(request, "core/form_page.html", {"form": form, "title": "Add Team", "eyebrow": month.name})


@login_required
def team_edit(request, group_slug, month_pk, pk):
    team = get_object_or_404(Team.objects.select_related("month__group"), pk=pk, month_id=month_pk, month__group__slug=group_slug)
    if not can_manage_teams(request.user, team.month.group):
        return HttpResponseForbidden("Group administrator access is required.")
    if reject_locked_month(request, team.month, "edit a team"):
        return redirect(team.month)
    form = TeamForm(request.POST or None, instance=team)
    if request.method == "POST" and form.is_valid():
        previous_name = team.name
        updated = form.save()
        AuditEvent.objects.create(actor=request.user, group=team.month.group, action="team.updated", object_type="Team", object_id=str(team.pk), summary=f"Updated team {previous_name} to {updated.name} for {team.month.name}.")
        messages.success(request, "Team updated.")
        return redirect("team-list", group_slug=group_slug, month_pk=month_pk)
    can_delete_team = can_remove(request.user, team.month.group) and team.month.status == ChallengeMonth.Status.DRAFT and not team.assignments.exists()
    return render(request, "core/team_edit.html", {"form": form, "team": team, "can_delete_team": can_delete_team})


@login_required
def team_archive_toggle(request, group_slug, month_pk, pk):
    team = get_object_or_404(Team.objects.select_related("month__group"), pk=pk, month_id=month_pk, month__group__slug=group_slug)
    if not can_manage_teams(request.user, team.month.group):
        return HttpResponseForbidden("Group administrator access is required.")
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
    if not can_remove(request.user, team.month.group):
        return HttpResponseForbidden("Group owner or platform root access is required.")
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
    if not can_manage_teams(request.user, month.group):
        return HttpResponseForbidden("Group administrator access is required.")
    if reject_locked_month(request, month, "change its team roster"):
        return redirect(month)
    form = TeamAssignmentForm(request.POST or None, month=month)
    if request.method == "POST" and form.is_valid():
        assignment = form.save()
        MonthEnrollment.objects.filter(
            month=month,
            participant=assignment.participant,
            enrolled_by__isnull=True,
        ).update(enrolled_by=request.user)
        AuditEvent.objects.create(actor=request.user, group=month.group, action="team_assignment.created", object_type="TeamAssignment", object_id=str(assignment.pk), summary=f"Assigned {assignment.participant.display_name} to {assignment.team.name} for {month.name}")
        messages.success(request, "Participant assigned to the team.")
        return redirect(month)
    return render(request, "core/form_page.html", {"form": form, "title": "Assign Participant", "eyebrow": month.name})


@login_required
def month_participant_list(request, group_slug, month_pk):
    month = get_object_or_404(ChallengeMonth.objects.select_related("group"), pk=month_pk, group__slug=group_slug)
    membership = membership_for(request.user, month.group)
    if not request.user.is_superuser and not membership:
        return HttpResponseForbidden("You are not a member of this reading group.")
    current_month_assignments = TeamAssignment.objects.filter(month=month).select_related("team")
    enrollments = month.enrollments.select_related("participant__user", "participant__user__northbound_profile").prefetch_related(
        Prefetch(
            "participant__team_assignments",
            queryset=current_month_assignments,
            to_attr="current_month_team_assignments",
        )
    )
    mutable = month_is_configurable(month)
    return render(request, "core/month_participant_list.html", {"month": month, "enrollments": enrollments, "can_manage_participants": mutable and can_manage_participants(request.user, month.group), "can_manage_teams": mutable and can_manage_teams(request.user, month.group), "can_remove": mutable and can_remove(request.user, month.group)})


@login_required
def month_participant_add(request, group_slug, month_pk):
    month = get_object_or_404(ChallengeMonth.objects.select_related("group"), pk=month_pk, group__slug=group_slug)
    if not can_manage_participants(request.user, month.group):
        return HttpResponseForbidden("Group administrator access is required.")
    if reject_locked_month(request, month, "add a participant"):
        return redirect(month)
    form = MonthEnrollmentForm(request.POST or None, month=month)
    if request.method == "POST" and form.is_valid():
        enrollment = form.save(enrolled_by=request.user)
        AuditEvent.objects.create(actor=request.user, group=month.group, action="month.participant_enrolled", object_type="MonthEnrollment", object_id=str(enrollment.pk), summary=f"Added {enrollment.participant.display_name} to {month.name}")
        team = form.cleaned_data.get("team")
        team_message = f" and was assigned to {team.name}" if team else " without a team assignment"
        messages.success(request, f"{enrollment.participant.display_name} was added to {month.name}{team_message}.")
        return redirect("month-participant-list", group_slug=month.group.slug, month_pk=month.pk)
    return render(request, "core/form_page.html", {"form": form, "title": "Add Participant to Month", "eyebrow": month.name})


@login_required
def month_participant_edit(request, group_slug, month_pk, pk):
    enrollment = get_object_or_404(MonthEnrollment.objects.select_related("month__group", "participant"), pk=pk, month_id=month_pk, month__group__slug=group_slug)
    if not can_manage_teams(request.user, enrollment.month.group):
        return HttpResponseForbidden("Group administrator access is required.")
    if reject_locked_month(request, enrollment.month, "change its team roster"):
        return redirect(enrollment.month)
    form = MonthParticipantEditForm(request.POST or None, enrollment=enrollment)
    if request.method == "POST" and form.is_valid():
        previous_team, new_team = form.save()
        previous_name = previous_team.name if previous_team else "Unassigned"
        new_name = new_team.name if new_team else "Unassigned"
        AuditEvent.objects.create(actor=request.user, group=enrollment.month.group, action="month.participant_team_changed", object_type="MonthEnrollment", object_id=str(enrollment.pk), summary=f"Changed {enrollment.participant.display_name} from {previous_name} to {new_name} for {enrollment.month.name}.")
        messages.success(request, f"Updated {enrollment.participant.display_name}'s team assignment.")
        return redirect("month-participant-list", group_slug=group_slug, month_pk=month_pk)
    return render(request, "core/form_page.html", {"form": form, "title": f"Edit {enrollment.participant.display_name}", "eyebrow": enrollment.month.name})


@login_required
def month_participant_remove(request, group_slug, month_pk, pk):
    enrollment = get_object_or_404(MonthEnrollment.objects.select_related("month__group", "participant"), pk=pk, month_id=month_pk, month__group__slug=group_slug)
    if not can_remove(request.user, enrollment.month.group):
        return HttpResponseForbidden("Group owner or platform root access is required.")
    if reject_locked_month(request, enrollment.month, "remove a participant"):
        return redirect(enrollment.month)
    if request.method == "POST":
        with transaction.atomic():
            group = enrollment.month.group
            month = enrollment.month
            participant = enrollment.participant
            TeamAssignment.objects.filter(month=month, participant=participant).delete()
            enrollment.delete()
            AuditEvent.objects.create(actor=request.user, group=group, action="month.participant_removed", object_type="MonthEnrollment", object_id=str(pk), summary=f"Removed {participant.display_name} from {month.name}; historical submissions were preserved.")
        messages.success(request, f"{participant.display_name} was removed from {month.name}.")
        return redirect("month-participant-list", group_slug=group_slug, month_pk=month_pk)
    return render(request, "core/confirm_remove.html", {"title": f"Remove {enrollment.participant.display_name} from {enrollment.month.name}?", "description": "They will no longer be able to submit to this month. Existing submissions and statistics will be preserved.", "cancel_url": reverse("month-participant-list", kwargs={"group_slug": group_slug, "month_pk": month_pk})})


@login_required
def submission_create(request, group_slug, month_pk):
    month = get_object_or_404(ChallengeMonth, pk=month_pk, group__slug=group_slug)
    participant = membership_for(request.user, month.group)
    if not participant and not request.user.is_superuser:
        return HttpResponseForbidden("You are not a member of this reading group.")
    if month.status != ChallengeMonth.Status.OPEN:
        messages.error(request, "This challenge month is not open for submissions.")
        return redirect(month)
    if request.user.is_superuser and not participant:
        raise Http404("A Platform Owner must also have a group membership to submit books.")
    if not MonthEnrollment.objects.filter(month=month, participant=participant).exists():
        messages.error(request, "You are not enrolled in this challenge month. Ask a group administrator to add you to the month or one of its teams.")
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
    if month.status != ChallengeMonth.Status.OPEN:
        return JsonResponse({"ok": False, "message": "This challenge month is not open for submissions."}, status=409)
    if not participant or not MonthEnrollment.objects.filter(month=month, participant=participant).exists():
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
    if not can_review(request.user, month.group):
        return HttpResponseForbidden("Moderator access is required.")
    if month.status not in REVIEWABLE_MONTH_STATUSES:
        messages.error(request, f"{month.get_status_display()} months are read-only and cannot be reviewed.")
        return redirect(month)
    submissions = month.submissions.filter(Q(status=BookSubmission.Status.PENDING) | Q(theme_claims__status=ThemeClaim.Status.PENDING), is_removed=False).select_related("participant").prefetch_related("theme_claims__theme").distinct()
    return render(request, "core/review_queue.html", {"month": month, "submissions": submissions})


@login_required
def submission_review(request, group_slug, month_pk, pk):
    submission = get_object_or_404(BookSubmission.objects.select_related("month__group", "participant"), pk=pk, month_id=month_pk, month__group__slug=group_slug)
    if not can_review(request.user, submission.month.group):
        return HttpResponseForbidden("Moderator access is required.")
    if submission.month.status not in REVIEWABLE_MONTH_STATUSES:
        messages.error(request, f"{submission.month.get_status_display()} months are read-only and cannot be reviewed.")
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
    themes = month.themes.all() if can_manage_months(request.user, month.group) else month.themes.filter(is_active=True, is_visible=True)
    return render(request, "core/theme_list.html", {"month": month, "themes": themes, "can_manage": month_is_configurable(month) and can_manage_months(request.user, month.group)})


@login_required
def theme_create(request, group_slug, month_pk):
    month = get_object_or_404(ChallengeMonth.objects.select_related("group"), pk=month_pk, group__slug=group_slug)
    if not can_manage_months(request.user, month.group):
        return HttpResponseForbidden("Group administrator access is required.")
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
    if not can_manage_months(request.user, theme.month.group):
        return HttpResponseForbidden("Group administrator access is required.")
    if reject_locked_month(request, theme.month, "edit a theme"):
        return redirect(theme.month)
    form = MonthThemeForm(request.POST or None, instance=theme)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f"{theme.name} was updated.")
        return redirect("theme-list", group_slug=group_slug, month_pk=month_pk)
    return render(request, "core/form_page.html", {"form": form, "title": f"Edit {theme.name}", "eyebrow": theme.month.name})
