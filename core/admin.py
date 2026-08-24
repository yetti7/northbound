from django.contrib import admin
from .models import AuditEvent, BookSubmission, CatalogBook, CatalogEdition, CatalogSearchCache, ChallengeMonth, ChallengeStaffAssignment, HardcoverConnection, Membership, MonthEnrollment, ReadingGroup, Team, TeamAssignment


@admin.register(ReadingGroup)
class ReadingGroupAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "timezone", "is_active")
    search_fields = ("name", "slug")


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ("display_name", "group", "role", "is_active")
    list_filter = ("group", "role", "is_active")


admin.site.register(ChallengeMonth)
admin.site.register(ChallengeStaffAssignment)
admin.site.register(Team)
admin.site.register(TeamAssignment)
admin.site.register(MonthEnrollment)
admin.site.register(BookSubmission)
admin.site.register(CatalogBook)
admin.site.register(CatalogEdition)
admin.site.register(CatalogSearchCache)
admin.site.register(HardcoverConnection)


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ("created_at", "actor", "group", "action", "summary")
    readonly_fields = ("actor", "group", "action", "object_type", "object_id", "summary", "created_at")
    list_filter = ("group", "action")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
