FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN groupadd --system --gid 10001 northbound \
    && useradd --system --uid 10001 --gid northbound northbound

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN python manage.py collectstatic --noinput

RUN mkdir -p /data/media \
    && chown -R northbound:northbound /app /data

USER northbound

EXPOSE 8000

CMD ["sh", "-c", "python manage.py restore_pending_backup && python manage.py migrate --noinput && exec gunicorn reading_challenge.wsgi:application --bind 0.0.0.0:${PORT:-8000} --workers ${WEB_CONCURRENCY:-2} --access-logfile - --error-logfile -"]
