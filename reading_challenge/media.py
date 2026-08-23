from pathlib import Path

from django.conf import settings
from django.core.exceptions import SuspiciousFileOperation
from django.core.files.storage import FileSystemStorage
from django.http import FileResponse, Http404, HttpResponseNotAllowed


def serve_media(request, path):
    """Serve small-install filesystem media without enabling Django's debug server."""
    if not settings.NORTHBOUND_SERVE_MEDIA:
        raise Http404
    if request.method not in {"GET", "HEAD"}:
        return HttpResponseNotAllowed(["GET", "HEAD"])

    storage = FileSystemStorage(location=settings.MEDIA_ROOT, base_url=settings.MEDIA_URL)
    try:
        media_file = storage.open(path, "rb")
    except (FileNotFoundError, IsADirectoryError, SuspiciousFileOperation, ValueError):
        raise Http404 from None

    response = FileResponse(media_file, filename=Path(path).name)
    response.headers["Cache-Control"] = "public, max-age=3600"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response
