FROM python:3.13-slim

ARG NORTHBOUND_VERSION=development
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NORTHBOUND_VERSION=${NORTHBOUND_VERSION}

WORKDIR /app

RUN groupadd --system --gid 10001 northbound \
    && useradd --system --uid 10001 --gid northbound northbound

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN python manage.py collectstatic --noinput

RUN mkdir -p /data/media \
    && chmod +x /app/deploy/start.sh \
    && chown -R northbound:northbound /app /data

USER northbound

EXPOSE 8000

CMD ["./deploy/start.sh"]
