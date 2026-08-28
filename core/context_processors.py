from django.urls import NoReverseMatch, reverse

from .platform_config import get_platform_settings
from .review_attention import needs_attention_summary


def platform_configuration(request):
    platform_settings = get_platform_settings()
    return {
        "platform_display_name": platform_settings.display_name,
        "public_registration_enabled": platform_settings.allow_public_registration,
        "user_group_creation_enabled": platform_settings.allow_user_group_creation,
    }


def needs_attention(request):
    return {"needs_attention": needs_attention_summary(request.user)}


def logical_navigation(request):
    """Return the explicit application parent for the current page."""
    match = request.resolver_match
    if not match:
        return {"logical_parent_url": None}

    view_name = match.view_name
    kwargs = match.kwargs
    group_slug = kwargs.get("group_slug")

    static_parents = {
        "platform-owner-list": "config-dashboard",
        "config-user-list": "config-dashboard",
        "config-group-list": "config-dashboard",
        "config-audit": "config-dashboard",
        "platform-settings": "config-dashboard",
        "platform-general-settings": "platform-settings",
        "platform-system-status": "platform-settings",
        "platform-storage-maintenance": "platform-settings",
        "platform-backups": "platform-settings",
        "platform-owner-create": "platform-owner-list",
        "platform-owner-status-toggle": "platform-owner-list",
        "platform-owner-invitation-revoke": "platform-owner-list",
        "config-user-detail": "config-user-list",
        "config-group-detail": "config-group-list",
        "group-create": "dashboard",
        "group-join": "dashboard",
        "account": "config-dashboard" if request.user.is_superuser else "dashboard",
        "my-stats": "dashboard",
        "needs-attention": "dashboard",
        "password-change": "account",
    }
    if view_name in static_parents:
        return {"logical_parent_url": reverse(static_parents[view_name])}

    try:
        if view_name in {"config-user-edit", "config-user-password-reset", "config-user-status-toggle"}:
            return {"logical_parent_url": reverse("config-user-detail", kwargs={"pk": kwargs["pk"]})}
        if view_name == "config-group-status-toggle":
            return {"logical_parent_url": reverse("config-group-detail", kwargs={"group_slug": group_slug})}
        if view_name in {
            "platform-cache-cleanup",
            "platform-audit-prune",
            "platform-sqlite-optimize",
        }:
            return {"logical_parent_url": reverse("platform-storage-maintenance")}
        if view_name in {
            "platform-backup-create",
            "stored-backup-download",
            "stored-backup-delete",
            "stored-backup-restore",
            "platform-backup-restore",
            "platform-restore-restart",
        }:
            return {"logical_parent_url": reverse("platform-backups")}
        if view_name == "group-detail":
            return {"logical_parent_url": reverse("dashboard")}
        if view_name in {
            "group-edit",
            "group-access-code",
            "group-hardcover-connection",
            "member-create",
            "participant-list",
            "month-list",
        }:
            return {"logical_parent_url": reverse("group-detail", kwargs={"group_slug": group_slug})}
        if view_name == "participant-detail":
            return {"logical_parent_url": reverse("participant-list", kwargs={"group_slug": group_slug})}
        if view_name in {"participant-role-edit", "participant-permissions-edit", "participant-deactivate"}:
            return {"logical_parent_url": reverse("participant-list", kwargs={"group_slug": group_slug})}
        if view_name == "group-hardcover-disconnect":
            return {"logical_parent_url": reverse("group-edit", kwargs={"group_slug": group_slug})}
        if view_name == "month-detail":
            return {"logical_parent_url": reverse("month-list", kwargs={"group_slug": group_slug})}
        if view_name == "month-create":
            return {"logical_parent_url": reverse("month-list", kwargs={"group_slug": group_slug})}
        if view_name == "challenge-settings":
            return {
                "logical_parent_url": reverse(
                    "month-detail", kwargs={"group_slug": group_slug, "pk": kwargs["month_pk"]}
                )
            }
        if view_name in {"challenge-general-settings", "challenge-schedule-settings", "challenge-visibility-settings", "month-announcement-update"}:
            return {
                "logical_parent_url": reverse(
                    "challenge-settings",
                    kwargs={"group_slug": group_slug, "month_pk": kwargs["month_pk"]},
                )
            }
        if view_name in {"month-edit", "challenge-lifecycle-transition"}:
            return {
                "logical_parent_url": reverse(
                    "challenge-settings",
                    kwargs={"group_slug": group_slug, "month_pk": kwargs["pk"]},
                )
            }
        if view_name == "month-delete":
            return {
                "logical_parent_url": reverse(
                    "month-detail", kwargs={"group_slug": group_slug, "pk": kwargs["pk"]}
                )
            }
        if view_name in {"challenge-signup-settings", "challenge-progress-checkpoints"}:
            return {
                "logical_parent_url": reverse(
                    "challenge-settings",
                    kwargs={"group_slug": group_slug, "month_pk": kwargs["month_pk"]},
                )
            }
        if view_name in {
            "challenge-host-list",
            "challenge-floater-list",
            "team-list",
            "month-participant-list",
            "theme-list",
            "review-queue",
            "team-stats-settings",
        }:
            return {
                "logical_parent_url": reverse(
                    "month-detail", kwargs={"group_slug": group_slug, "pk": kwargs["month_pk"]}
                )
            }
        if view_name == "team-detail":
            return {
                "logical_parent_url": reverse(
                    "team-list", kwargs={"group_slug": group_slug, "month_pk": kwargs["month_pk"]}
                )
            }
        if view_name == "challenge-host-end":
            return {
                "logical_parent_url": reverse(
                    "challenge-host-list", kwargs={"group_slug": group_slug, "month_pk": kwargs["month_pk"]}
                )
            }
        if view_name == "challenge-floater-end":
            return {
                "logical_parent_url": reverse(
                    "challenge-floater-list", kwargs={"group_slug": group_slug, "month_pk": kwargs["month_pk"]}
                )
            }
        if view_name in {
            "team-create",
            "team-edit",
            "team-archive-toggle",
            "team-assignment-create",
            "team-assignment-remove",
        }:
            return {
                "logical_parent_url": reverse(
                    "team-list", kwargs={"group_slug": group_slug, "month_pk": kwargs["month_pk"]}
                )
            }
        if view_name == "team-delete":
            return {
                "logical_parent_url": reverse(
                    "team-edit",
                    kwargs={"group_slug": group_slug, "month_pk": kwargs["month_pk"], "pk": kwargs["pk"]},
                )
            }
        if view_name == "team-leader-list":
            return {
                "logical_parent_url": reverse(
                    "team-list", kwargs={"group_slug": group_slug, "month_pk": kwargs["month_pk"]}
                )
            }
        if view_name == "team-leader-end":
            return {
                "logical_parent_url": reverse(
                    "team-leader-list",
                    kwargs={
                        "group_slug": group_slug,
                        "month_pk": kwargs["month_pk"],
                        "team_pk": kwargs["team_pk"],
                    },
                )
            }
        if view_name in {
            "month-participant-add",
            "month-participant-edit",
            "month-participant-remove",
            "month-participant-reactivate",
        }:
            return {
                "logical_parent_url": reverse(
                    "month-participant-list", kwargs={"group_slug": group_slug, "month_pk": kwargs["month_pk"]}
                )
            }
        if view_name in {"challenge-register", "challenge-withdraw"}:
            return {
                "logical_parent_url": reverse(
                    "month-detail", kwargs={"group_slug": group_slug, "pk": kwargs["month_pk"]}
                )
            }
        if view_name in {"theme-create", "theme-edit"}:
            return {
                "logical_parent_url": reverse(
                    "theme-list", kwargs={"group_slug": group_slug, "month_pk": kwargs["month_pk"]}
                )
            }
        if view_name in {"submission-create", "submission-catalog", "submission-remove"}:
            return {
                "logical_parent_url": reverse(
                    "month-detail", kwargs={"group_slug": group_slug, "pk": kwargs["month_pk"]}
                )
            }
        if view_name == "submission-review":
            return {
                "logical_parent_url": reverse(
                    "review-queue", kwargs={"group_slug": group_slug, "month_pk": kwargs["month_pk"]}
                )
            }
    except (KeyError, NoReverseMatch):
        return {"logical_parent_url": None}

    return {"logical_parent_url": None}
