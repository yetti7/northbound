from django.urls import NoReverseMatch, reverse

from .platform_config import get_platform_settings
from .review_attention import needs_attention_summary


def platform_configuration(request):
    platform_settings = get_platform_settings()
    return {
        "platform_display_name": platform_settings.display_name,
        "public_registration_enabled": platform_settings.allow_public_registration,
        "user_group_creation_enabled": platform_settings.allow_user_group_creation,
        "logical_parent_label": logical_parent_label(request),
    }


def needs_attention(request):
    return {"needs_attention": needs_attention_summary(request.user)}


def logical_parent_label(request):
    """Return destination-aware copy for the existing logical parent link."""
    match = request.resolver_match
    if not match:
        return ""

    view_name = match.view_name
    if view_name in {
        "platform-owner-list", "config-user-list", "config-group-list",
        "config-audit", "advanced-recovery", "platform-settings",
        "platform-general-settings", "platform-hardcover-oauth",
        "platform-system-status", "platform-storage-maintenance", "platform-backups",
    }:
        return "Back to Platform Administration"
    if view_name in {
        "recovery-history", "recovery-challenge-list", "recovery-group-list",
        "recovery-account-list", "recovery-submission-list", "recovery-theme-list",
        "recovery-provenance-list", "recovery-botm-list", "recovery-tbr-list",
        "recovery-game-list", "recovery-checkpoint-list", "recovery-hardcover-list",
    }:
        return "Back to Advanced Recovery"
    if view_name in {"recovery-reader-hardcover-clear", "recovery-group-hardcover-clear"}:
        return "Back to Hardcover Credential Recovery"
    if view_name == "recovery-theme-claim":
        return "Back to Submission Recovery"
    recovery_domain_labels = {
        "challenge": "Back to Challenge Recovery",
        "group": "Back to Group Recovery",
        "account": "Back to Account Recovery",
        "submission": "Back to Submission Recovery",
        "theme": "Back to Theme Recovery",
        "provenance": "Back to Modifier Recovery",
        "botm": "Back to BOTM Recovery",
        "tbr": "Back to Personal TBR Recovery",
        "game": "Back to Game Reward Recovery",
        "checkpoint": "Back to Checkpoint Recovery",
        "hardcover": "Back to Hardcover Credential Recovery",
    }
    for domain, label in recovery_domain_labels.items():
        if view_name.startswith(f"recovery-{domain}-"):
            return label
    if view_name in {
        "platform-cache-cleanup", "platform-audit-prune", "platform-sqlite-optimize",
    }:
        return "Back to Storage & Maintenance"
    if view_name in {
        "platform-backup-create", "stored-backup-download", "stored-backup-delete",
        "stored-backup-restore", "platform-backup-restore", "platform-restore-restart",
    }:
        return "Back to Backups"
    if view_name.startswith("platform-owner-"):
        return "Back to Platform Owners"
    if view_name == "config-user-detail":
        return "Back to Accounts"
    if view_name.startswith("config-user-"):
        return "Back to Account"
    if view_name == "config-group-detail":
        return "Back to Groups"
    if view_name.startswith("config-group-"):
        return "Back to Group Overview"
    if view_name in {"group-create", "group-join", "account", "my-stats", "needs-attention"}:
        return "Back to Home"
    if view_name == "password-change":
        return "Back to My Account"
    if view_name == "group-detail":
        return "Back to Reading Groups"
    if view_name in {
        "group-edit", "group-access-code", "group-hardcover-connection",
        "member-create", "participant-list", "month-list",
    }:
        return "Back to Group"
    if view_name == "group-hardcover-disconnect":
        return "Back to Group Settings"
    if view_name == "participant-detail" or view_name.startswith("participant-"):
        return "Back to Members"
    if view_name in {"month-detail", "month-create"}:
        return "Back to Challenges"
    if view_name in {
        "challenge-settings", "challenge-host-list", "challenge-floater-list",
        "team-list", "month-participant-list", "theme-list", "game-list",
        "review-queue", "team-stats-settings", "challenge-register",
        "challenge-withdraw", "submission-create", "submission-catalog",
        "submission-remove", "personal-tbr-detail", "botm-list",
    }:
        return "Back to Challenge"
    if view_name in {
        "challenge-general-settings", "challenge-schedule-settings",
        "challenge-games-settings", "challenge-botm-settings",
        "challenge-visibility-settings", "challenge-signup-settings",
        "challenge-progress-checkpoints", "month-announcement-update", "month-edit",
        "challenge-lifecycle-transition",
    }:
        return "Back to Challenge Settings"
    if view_name == "month-delete":
        return "Back to Challenge"
    if view_name == "team-detail" or view_name in {
        "team-create", "team-edit", "team-archive-toggle", "team-delete",
        "team-assignment-create", "team-assignment-remove", "team-leader-list",
    }:
        return "Back to Teams"
    if view_name == "team-leader-end":
        return "Back to Team Leaders"
    if view_name in {"challenge-host-end", "challenge-floater-end"}:
        return "Back to Challenge Staff"
    if view_name.startswith("month-participant-"):
        return "Back to Challenge Participants"
    if view_name in {"theme-create", "theme-edit"}:
        return "Back to Themes"
    if view_name == "game-detail" or view_name == "game-create":
        return "Back to Games"
    if view_name.startswith("game-"):
        return "Back to Game"
    if view_name == "submission-review":
        return "Back to Review Queue"
    return "Back"


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
        "advanced-recovery": "config-dashboard",
        "recovery-history": "advanced-recovery",
        "recovery-challenge-list": "advanced-recovery",
        "recovery-group-list": "advanced-recovery",
        "recovery-account-list": "advanced-recovery",
        "recovery-submission-list": "advanced-recovery",
        "recovery-theme-list": "advanced-recovery",
        "recovery-provenance-list": "advanced-recovery",
        "recovery-botm-list": "advanced-recovery",
        "recovery-tbr-list": "advanced-recovery",
        "recovery-game-list": "advanced-recovery",
        "recovery-checkpoint-list": "advanced-recovery",
        "recovery-hardcover-list": "advanced-recovery",
        "platform-settings": "config-dashboard",
        "platform-general-settings": "config-dashboard",
        "platform-hardcover-oauth": "config-dashboard",
        "platform-system-status": "config-dashboard",
        "platform-storage-maintenance": "config-dashboard",
        "platform-backups": "config-dashboard",
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

    if view_name in {"recovery-challenge-detail", "recovery-challenge-purge"}:
        return {"logical_parent_url": reverse("recovery-challenge-list")}
    if view_name in {"recovery-group-detail", "recovery-group-status", "recovery-group-transfer-owner"}:
        return {"logical_parent_url": reverse("recovery-group-list")}
    if view_name == "recovery-account-status":
        return {"logical_parent_url": reverse("recovery-account-list")}
    if view_name in {"recovery-submission-detail", "recovery-submission-status", "recovery-submission-purge"}:
        return {"logical_parent_url": reverse("recovery-submission-list")}
    if view_name in {"recovery-theme-detail", "recovery-theme-status", "recovery-theme-correct"}:
        return {"logical_parent_url": reverse("recovery-theme-list")}
    if view_name == "recovery-theme-claim":
        return {"logical_parent_url": reverse("recovery-submission-list")}
    if view_name in {"recovery-provenance-detail", "recovery-provenance-void", "recovery-provenance-rebuild", "recovery-provenance-purge"}:
        return {"logical_parent_url": reverse("recovery-provenance-list")}
    if view_name in {"recovery-botm-book-detail", "recovery-botm-book-status", "recovery-botm-book-correct", "recovery-botm-match", "recovery-botm-match-purge"}:
        return {"logical_parent_url": reverse("recovery-botm-list")}
    if view_name in {"recovery-tbr-detail", "recovery-tbr-entry", "recovery-tbr-rebuild", "recovery-tbr-match", "recovery-tbr-match-purge"}:
        return {"logical_parent_url": reverse("recovery-tbr-list")}
    if view_name in {"recovery-game-detail", "recovery-game-status", "recovery-game-application", "recovery-game-recreate"}:
        return {"logical_parent_url": reverse("recovery-game-list")}
    if view_name in {"recovery-checkpoint-detail", "recovery-checkpoint-reset", "recovery-checkpoint-release"}:
        return {"logical_parent_url": reverse("recovery-checkpoint-list")}
    if view_name in {"recovery-reader-hardcover-clear", "recovery-group-hardcover-clear"}:
        return {"logical_parent_url": reverse("recovery-hardcover-list")}

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
        if view_name in {"challenge-general-settings", "challenge-schedule-settings", "challenge-games-settings", "challenge-visibility-settings", "month-announcement-update"}:
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
            "game-list",
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
        if view_name == "game-create":
            return {
                "logical_parent_url": reverse(
                    "game-list", kwargs={"group_slug": group_slug, "month_pk": kwargs["month_pk"]}
                )
            }
        if view_name == "game-detail":
            return {
                "logical_parent_url": reverse(
                    "game-list", kwargs={"group_slug": group_slug, "month_pk": kwargs["month_pk"]}
                )
            }
        if view_name in {"game-edit", "game-active-toggle", "game-delete", "game-reward-apply", "game-reward-void"}:
            return {
                "logical_parent_url": reverse(
                    "game-detail",
                    kwargs={"group_slug": group_slug, "month_pk": kwargs["month_pk"], "pk": kwargs["pk"]},
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
