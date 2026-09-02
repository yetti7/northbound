from django.db import DatabaseError, connection
from django.http import HttpResponse


def health_response():
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except DatabaseError:
        return HttpResponse("UNAVAILABLE", status=503, content_type="text/plain")
    return HttpResponse("OK", content_type="text/plain")
