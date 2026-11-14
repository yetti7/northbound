"""Opaque media names; never use the supplied filename as a storage path."""
from uuid import uuid4


def branding_path(instance, filename):
    return f'branding/{instance._meta.model_name}/{uuid4().hex}.png'
