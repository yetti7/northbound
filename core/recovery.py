from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable
from uuid import UUID, uuid4

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from .backups import list_stored_backups
from .models import RecoveryOperation, safe_audit_summary


RECOVERY_REASON_MAX_LENGTH = 1000
RECENT_BACKUP_WINDOW = timedelta(hours=24)
SECRET_KEY_PARTS = ("password", "secret", "token", "ciphertext", "encryption_key", "session", "invitation", "credential", "token_hint", "registration_answer")


@dataclass(frozen=True)
class RecoveryImpactItem:
    label: str
    count: int

    def __post_init__(self):
        if not self.label.strip():
            raise ValidationError("Recovery impact labels cannot be blank.")
        if not isinstance(self.count, int) or isinstance(self.count, bool) or self.count < 0:
            raise ValidationError("Recovery impact counts must be nonnegative integers.")

    def as_dict(self):
        return {"label": self.label, "count": self.count}


@dataclass(frozen=True)
class RecoveryImpactPreview:
    target_label: str
    items: tuple[RecoveryImpactItem, ...] = ()
    warnings: tuple[str, ...] = ()

    def as_dict(self):
        return {"target_label": self.target_label, "items": [item.as_dict() for item in self.items], "warnings": list(self.warnings)}


@dataclass(frozen=True)
class RecoveryRequest:
    actor: object
    tier: int
    action: str
    target_type: str
    target_id: str
    target_label: str
    reason: str = ""
    required_confirmation: str = ""
    supplied_confirmation: str = ""
    current_password: str = ""
    confirmation_method: str = ""
    group: object = None
    challenge: object = None
    impact: RecoveryImpactPreview | dict = field(default_factory=dict)
    before_state: dict = field(default_factory=dict)
    operation_id: UUID = field(default_factory=uuid4)


@dataclass(frozen=True)
class RecoveryMutationResult:
    after_state: dict = field(default_factory=dict)
    impact: RecoveryImpactPreview | dict | None = None


def require_platform_owner(actor):
    if not getattr(actor, "is_authenticated", False) or not actor.is_active or not actor.is_superuser:
        raise PermissionDenied("Active Platform Owner access is required.")


def validate_reason(reason, *, required):
    value = (reason or "").strip()
    if required and not value:
        raise ValidationError({"reason": "Enter a recovery reason."})
    if len(value) > RECOVERY_REASON_MAX_LENGTH:
        raise ValidationError({"reason": f"Recovery reasons must be {RECOVERY_REASON_MAX_LENGTH} characters or fewer."})
    return value


def validate_typed_confirmation(*, required, supplied):
    if not required:
        return ""
    if supplied != required:
        raise ValidationError({"confirmation": f'Type "{required}" exactly to confirm.'})
    return supplied


def safe_recovery_data(value):
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            normalized = str(key).casefold().replace("-", "_")
            cleaned[str(key)] = "[REDACTED]" if any(part in normalized for part in SECRET_KEY_PARTS) else safe_recovery_data(item)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [safe_recovery_data(item) for item in value]
    if isinstance(value, str):
        return safe_audit_summary(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def validate_recovery_request(request):
    require_platform_owner(request.actor)
    if request.tier not in RecoveryOperation.Tier.values:
        raise ValidationError({"tier": "Choose a supported recovery tier."})
    for field_name in ("action", "target_type", "target_label"):
        if not str(getattr(request, field_name) or "").strip():
            raise ValidationError({field_name: "This recovery operation field is required."})
    reason = validate_reason(request.reason, required=request.tier in {RecoveryOperation.Tier.DESTRUCTIVE, RecoveryOperation.Tier.EMERGENCY})
    confirmation = validate_typed_confirmation(required=request.required_confirmation, supplied=request.supplied_confirmation)
    if request.tier == RecoveryOperation.Tier.EMERGENCY:
        if not request.required_confirmation:
            raise ValidationError({"confirmation": "Tier 3 recovery requires exact typed confirmation."})
        if not request.current_password or not request.actor.check_password(request.current_password):
            raise ValidationError({"current_password": "Enter your current Platform Owner password."})
    return reason, confirmation


def _ledger_payload(request, *, reason, result, after_state=None, impact_override=None):
    impact_source = request.impact if impact_override is None else impact_override
    impact = impact_source.as_dict() if hasattr(impact_source, "as_dict") else impact_source
    return {
        "operation_id": request.operation_id, "actor": request.actor, "tier": request.tier,
        "action": request.action, "target_type": request.target_type, "target_id": str(request.target_id or ""),
        "target_label": request.target_label, "group": request.group, "challenge": request.challenge,
        "reason": reason, "confirmation_method": request.confirmation_method,
        "impact": safe_recovery_data(impact or {}), "before_state": safe_recovery_data(request.before_state or {}),
        "after_state": safe_recovery_data(after_state or {}), "result": result,
    }


def execute_recovery_operation(request: RecoveryRequest, mutation: Callable[[], dict | None]):
    reason, _ = validate_recovery_request(request)
    try:
        with transaction.atomic():
            mutation_result = mutation() or {}
            if isinstance(mutation_result, RecoveryMutationResult):
                after_state = mutation_result.after_state
                impact_override = mutation_result.impact
            else:
                after_state = mutation_result
                impact_override = None
            operation = RecoveryOperation.objects.create(**_ledger_payload(
                request, reason=reason, result=RecoveryOperation.Result.SUCCEEDED,
                after_state=after_state, impact_override=impact_override,
            ))
    except Exception:
        RecoveryOperation.objects.create(**_ledger_payload(request, reason=reason, result=RecoveryOperation.Result.FAILED))
        raise
    return operation


def stored_backup_advisory(*, now=None):
    current_time = now or timezone.now()
    paths = list_stored_backups()
    if not paths:
        return {"exists": False, "newest_at": None, "is_recent": False, "window_hours": 24}
    newest_at = datetime.fromtimestamp(paths[0].stat().st_mtime, tz=timezone.get_current_timezone())
    return {"exists": True, "newest_at": newest_at, "is_recent": current_time - newest_at <= RECENT_BACKUP_WINDOW, "window_hours": 24}
