from django import forms
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.forms import UserChangeForm, UserCreationForm
from django.db import transaction

from .forms import IdentifierAuthenticationForm, NewAccountIdentityMixin
from .identity import IdentifierValidationMixin

from .models import AuditEvent, RecoveryOperation, UserProfile


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


# Keep the default backend/session path and Django's staff-only admin boundary.
class NorthboundAdminAuthenticationForm(IdentifierAuthenticationForm):
    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        if not user.is_staff:
            raise self.get_invalid_login_error()


class AdminAccountCreationForm(NewAccountIdentityMixin, UserCreationForm):
    email = forms.EmailField()

    class Meta(UserCreationForm.Meta):
        model = get_user_model()
        fields = ('username', 'email')

    @transaction.atomic
    def save(self, commit=True):
        user = super().save(commit=commit)
        if commit:
            self.save_identity(user)
        return user


class AdminAccountChangeForm(IdentifierValidationMixin, UserChangeForm):
    email = forms.EmailField()
    display_name = forms.CharField(max_length=100, label='Display Name')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        profile = getattr(self.instance, 'northbound_profile', None)
        self.fields['display_name'].initial = profile.discord_username if profile else ''

    @transaction.atomic
    def save(self, commit=True):
        user = super().save(commit=commit)
        if commit:
            profile, _ = UserProfile.objects.get_or_create(user=user)
            profile.discord_username = self.cleaned_data['display_name']
            profile.save(update_fields=['discord_username'])
        return user


class NorthboundUserAdmin(UserAdmin):
    form = AdminAccountChangeForm
    add_form = AdminAccountCreationForm
    fieldsets = (
        (None, {'fields': ('username',)}),
        ('Account identity', {'fields': ('display_name', 'first_name', 'last_name', 'email')}),
        *UserAdmin.fieldsets[2:],
    )
    add_fieldsets = ((None, {'classes': ('wide',), 'fields': ('username', 'display_name', 'email', 'password1', 'password2')}),)

    def save_model(self, request, obj, form, change):
        # Django admin calls form.save(commit=False), then save_model.
        with transaction.atomic():
            from .identity import lock_identifier_writes, validate_identifiers
            lock_identifier_writes()
            validate_identifiers(obj.username, obj.email, user_id=obj.pk)
            super().save_model(request, obj, form, change)
            profile, _ = UserProfile.objects.get_or_create(user=obj)
            profile.discord_username = form.cleaned_data['display_name']
            if not change:
                from django.utils import timezone
                profile.display_name_confirmed_at = timezone.now()
            profile.save()


admin.site.login_form = NorthboundAdminAuthenticationForm
admin.site.unregister(get_user_model())
admin.site.register(get_user_model(), NorthboundUserAdmin)
