#!/usr/bin/env bash
set -euo pipefail

project_name="${RESTORE_TEST_PROJECT:-northbound-restore-e2e}"
host_port="${NORTHBOUND_PORT:-18060}"
base_url="http://127.0.0.1:${host_port}"
work_dir="$(mktemp -d)"
cookie_jar="${work_dir}/cookies.txt"
page_file="${work_dir}/page.html"
compose=(docker compose -p "$project_name" -f compose.yaml -f compose.restore-test.yaml)

cleanup() {
  "${compose[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$work_dir"
}
trap cleanup EXIT

wait_for_health() {
  for _ in $(seq 1 90); do
    if curl --silent --fail "${base_url}/health/" | grep -q '"ok": true'; then
      return 0
    fi
    sleep 1
  done
  "${compose[@]}" logs web
  return 1
}

csrf_from_page() {
  sed -n 's/.*name="csrfmiddlewaretoken" value="\([^"]*\)".*/\1/p' "$page_file" | head -n 1
}

"${compose[@]}" up -d --build
wait_for_health

"${compose[@]}" exec -T web python manage.py shell -c '
from pathlib import Path
from django.conf import settings
from django.contrib.auth import get_user_model
from core.models import ReadingGroup
owner = get_user_model().objects.create_superuser("restore-owner", "restore@example.com", "restore-owner-password-482!")
ReadingGroup.objects.create(name="Backed Up State", slug="restore-sentinel")
media = Path(settings.MEDIA_ROOT) / "restore-check.txt"
media.parent.mkdir(parents=True, exist_ok=True)
media.write_text("media-before-backup")
Path("/data/.env").write_text("DJANGO_SECRET_KEY=must-not-enter-backup")
'

curl --silent --show-error --cookie-jar "$cookie_jar" "${base_url}/config/login/" --output "$page_file"
csrf_token="$(csrf_from_page)"
login_status="$(curl --silent --show-error --cookie "$cookie_jar" --cookie-jar "$cookie_jar" --output /dev/null --write-out '%{http_code}' \
  --header "Referer: ${base_url}/config/login/" \
  --data-urlencode "csrfmiddlewaretoken=${csrf_token}" \
  --data-urlencode "username=restore-owner" \
  --data-urlencode "password=restore-owner-password-482!" \
  "${base_url}/config/login/")"
test "$login_status" = "302"

curl --silent --show-error --cookie "$cookie_jar" "${base_url}/config/settings/backups/" --output "$page_file"
csrf_token="$(csrf_from_page)"
create_status="$(curl --silent --show-error --cookie "$cookie_jar" --cookie-jar "$cookie_jar" --output /dev/null --write-out '%{http_code}' \
  --header "Referer: ${base_url}/config/settings/backups/" \
  --data-urlencode "csrfmiddlewaretoken=${csrf_token}" \
  "${base_url}/config/settings/backups/create/")"
test "$create_status" = "302"

backup_name="$("${compose[@]}" exec -T web sh -c 'basename "$(ls -1t /data/backups/northbound-manual-*.zip | head -n 1)"')"
test -n "$backup_name"
"${compose[@]}" exec -T -e BACKUP_NAME="$backup_name" web python -c '
import json, os, zipfile
path = "/data/backups/" + os.environ["BACKUP_NAME"]
with zipfile.ZipFile(path) as archive:
    names = archive.namelist()
    assert "northbound.sqlite3" in names
    assert "media/restore-check.txt" in names
    assert "northbound-backup.json" in names
    assert ".env" not in names
    metadata = json.loads(archive.read("northbound-backup.json"))
    assert metadata["database"] == "sqlite"
    assert metadata["automatic"] is False
'

"${compose[@]}" exec -T web python manage.py shell -c '
from pathlib import Path
from django.conf import settings
from core.models import ReadingGroup
group = ReadingGroup.objects.get(slug="restore-sentinel")
group.name = "Mutated State"
group.save(update_fields=["name"])
ReadingGroup.objects.create(name="Post Backup Only", slug="post-backup-only")
(Path(settings.MEDIA_ROOT) / "restore-check.txt").write_text("media-after-backup")
'

restart_count_before="$(docker inspect --format '{{.RestartCount}}' "$("${compose[@]}" ps -q web)")"
curl --silent --show-error --cookie "$cookie_jar" "${base_url}/config/settings/backups/${backup_name}/restore/" --output "$page_file"
csrf_token="$(csrf_from_page)"
restore_status="$(curl --silent --show-error --cookie "$cookie_jar" --cookie-jar "$cookie_jar" --output "$page_file" --write-out '%{http_code}' \
  --header "Referer: ${base_url}/config/settings/backups/${backup_name}/restore/" \
  --data-urlencode "csrfmiddlewaretoken=${csrf_token}" \
  --data-urlencode "current_password=restore-owner-password-482!" \
  --data-urlencode "confirmation=RESTORE" \
  "${base_url}/config/settings/backups/${backup_name}/restore/")"
test "$restore_status" = "200"
grep -q "Northbound Is Restarting" "$page_file"

for _ in $(seq 1 60); do
  restart_count_after="$(docker inspect --format '{{.RestartCount}}' "$("${compose[@]}" ps -q web)" 2>/dev/null || echo 0)"
  if [ "$restart_count_after" -gt "$restart_count_before" ]; then
    break
  fi
  sleep 1
done
test "$restart_count_after" -gt "$restart_count_before"
wait_for_health
curl --silent --fail "${base_url}/health/" | grep -q '"restore_pending": false'

"${compose[@]}" exec -T web python manage.py migrate --check
"${compose[@]}" exec -T web python manage.py shell -c '
import json, sqlite3
from pathlib import Path
from django.conf import settings
from core.models import ReadingGroup
assert ReadingGroup.objects.filter(slug="restore-sentinel", name="Backed Up State").exists()
assert not ReadingGroup.objects.filter(slug="post-backup-only").exists()
assert (Path(settings.MEDIA_ROOT) / "restore-check.txt").read_text() == "media-before-backup"
rollbacks = sorted(Path("/data").glob("pre-restore-*"))
assert rollbacks
rollback = rollbacks[-1]
assert (rollback / "media" / "restore-check.txt").read_text() == "media-after-backup"
database = sqlite3.connect(rollback / "northbound.sqlite3")
try:
    names = {row[0] for row in database.execute("SELECT name FROM core_readinggroup")}
finally:
    database.close()
assert "Mutated State" in names
assert "Post Backup Only" in names
restore_record = json.loads(Path("/data/last-restore.json").read_text())
assert restore_record["rollback_directory"] == str(rollback)
'

echo "Docker restore lifecycle validation passed."
