from django.contrib import admin

from .models import AuditEvent, RecoveryOperation


class PermanentReadOnlyAdmin(admin.ModelAdmin):
    actions = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AuditEvent)
class AuditEventAdmin(PermanentReadOnlyAdmin):
    list_display = ("created_at", "actor", "group", "action", "summary")
    readonly_fields = ("actor", "group", "action", "object_type", "object_id", "summary", "created_at")
    list_filter = ("group", "action")


@admin.register(RecoveryOperation)
class RecoveryOperationAdmin(PermanentReadOnlyAdmin):
    list_display = ("created_at", "operation_id", "actor", "tier", "action", "target_label", "result")
    readonly_fields = (
        "operation_id", "actor", "created_at", "tier", "action", "target_type", "target_id",
        "target_label", "group", "challenge", "reason", "confirmation_method", "impact",
        "before_state", "after_state", "result",
    )
    list_filter = ("tier", "result", "action")
    search_fields = ("operation_id", "action", "target_label", "reason")
