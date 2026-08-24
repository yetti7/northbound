import json
from datetime import date, datetime, time, timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import Group as AuthGroup
from django.core.management.base import CommandError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import (
    AuditEvent,
    BookSubmission,
    CatalogBook,
    CatalogEdition,
    ChallengeMonth,
    ChallengeStaffAssignment,
    Membership,
    MonthEnrollment,
    MonthTheme,
    PlatformOwnerInvitation,
    ReadingGroup,
    Team,
    TeamAssignment,
    ThemeClaim,
    UserProfile,
)


DATASET_KEY = "northbound-canonical-demo-v1"
DEMO_AUTH_GROUP = "__northbound_canonical_demo_v1__"
DEMO_CATALOG_PROVIDER = "northbound-demo-v1"
DEMO_PASSWORD = "NorthboundDemo!2026"
MANIFEST_ACTION = "demo.dataset_seeded"
MANIFEST_OBJECT_TYPE = "DemoDatasetManifest"


ACCOUNT_SPECS = (
    ("maren.holt", "Maren", "Holt"),
    ("theo.bennett", "Theo", "Bennett"),
    ("priya.shah", "Priya", "Shah"),
    ("caleb.ross", "Caleb", "Ross"),
    ("lena.ortiz", "Lena", "Ortiz"),
    ("nora.kim", "Nora", "Kim"),
    ("elliot.price", "Elliot", "Price"),
    ("jasmine.cole", "Jasmine", "Cole"),
    ("wesley.grant", "Wesley", "Grant"),
    ("fiona.brooks", "Fiona", "Brooks"),
    ("celeste.rowan", "Celeste", "Rowan"),
    ("jonah.vale", "Jonah", "Vale"),
    ("amara.quinn", "Amara", "Quinn"),
    ("miles.arden", "Miles", "Arden"),
    ("ivy.mercer", "Ivy", "Mercer"),
    ("dante.frost", "Dante", "Frost"),
    ("soraya.bell", "Soraya", "Bell"),
    ("lucas.wren", "Lucas", "Wren"),
    ("opal.rivera", "Opal", "Rivera"),
    ("henry.sloane", "Henry", "Sloane"),
)

AVATARS = tuple([f"memo_{number}.png" for number in range(1, 16)] + [f"notion_{number}.png" for number in range(1, 6)])

GROUP_SPECS = {
    "lantern": {
        "name": "Lantern & Leaf Society",
        "slug": "lantern-leaf-society",
        "join_code": "LAMP26",
        "announcement": "Sunday check-in is open—share one passage that stayed with you this week.",
        "members": {
            "maren.holt": Membership.Role.OWNER,
            "theo.bennett": Membership.Role.MODERATOR,
            "priya.shah": Membership.Role.MODERATOR,
            "caleb.ross": Membership.Role.MEMBER,
            "lena.ortiz": Membership.Role.MEMBER,
            "nora.kim": Membership.Role.MEMBER,
            "elliot.price": Membership.Role.MEMBER,
            "jasmine.cole": Membership.Role.MEMBER,
            "wesley.grant": Membership.Role.MEMBER,
            "fiona.brooks": Membership.Role.MEMBER,
            "jonah.vale": Membership.Role.MEMBER,
        },
        "reviewer": "priya.shah",
    },
    "midnight": {
        "name": "Midnight Quill Guild",
        "slug": "midnight-quill-guild",
        "join_code": "RAVEN7",
        "announcement": "The lantern stays lit after midnight—bring your strangest atmospheric read to Friday's chat.",
        "members": {
            "celeste.rowan": Membership.Role.OWNER,
            "jonah.vale": Membership.Role.MODERATOR,
            "amara.quinn": Membership.Role.MODERATOR,
            "miles.arden": Membership.Role.MEMBER,
            "ivy.mercer": Membership.Role.MEMBER,
            "dante.frost": Membership.Role.MEMBER,
            "soraya.bell": Membership.Role.MEMBER,
            "lucas.wren": Membership.Role.MEMBER,
            "opal.rivera": Membership.Role.MEMBER,
            "henry.sloane": Membership.Role.MEMBER,
            "nora.kim": Membership.Role.MEMBER,
        },
        "reviewer": "amara.quinn",
    },
}

MONTH_SPECS = {
    "lantern-history": {
        "group": "lantern",
        "name": "Summer Pages",
        "starts_on": date(2026, 7, 1),
        "ends_on": date(2026, 7, 31),
        "status": ChallengeMonth.Status.ARCHIVED,
        "announcement_mode": ChallengeMonth.AnnouncementMode.CUSTOM,
        "announcement": "Summer Pages is complete—browse the final team totals and celebrate every finish.",
        "visibility": ChallengeMonth.TeamStatsVisibility.EVERYONE,
    },
    "lantern-current": {
        "group": "lantern",
        "name": "Stories Under the Stars",
        "starts_on": date(2026, 8, 1),
        "ends_on": date(2026, 8, 31),
        "status": ChallengeMonth.Status.OPEN,
        "announcement_mode": ChallengeMonth.AnnouncementMode.INHERIT,
        "announcement": "",
        "visibility": ChallengeMonth.TeamStatsVisibility.EVERYONE,
    },
    "midnight-history": {
        "group": "midnight",
        "name": "Gothic Echoes",
        "starts_on": date(2026, 7, 1),
        "ends_on": date(2026, 7, 31),
        "status": ChallengeMonth.Status.FINALIZED,
        "announcement_mode": ChallengeMonth.AnnouncementMode.NONE,
        "announcement": "",
        "visibility": ChallengeMonth.TeamStatsVisibility.STAFF,
    },
    "midnight-current": {
        "group": "midnight",
        "name": "Secrets After Sundown",
        "starts_on": date(2026, 8, 1),
        "ends_on": date(2026, 8, 31),
        "status": ChallengeMonth.Status.OPEN,
        "announcement_mode": ChallengeMonth.AnnouncementMode.CUSTOM,
        "announcement": "Midmonth mystery swap begins Friday. Keep your recommendation spoiler-free.",
        "visibility": ChallengeMonth.TeamStatsVisibility.STAFF,
    },
}

TEAM_SPECS = {
    "lantern-history": (("golden-hour", "Golden Hour", "#E9A23B"), ("moonlit-pages", "Moonlit Pages", "#7251B5")),
    "lantern-current": (("aurora-readers", "Aurora Readers", "#E76F51"), ("comet-chasers", "Comet Chasers", "#3A86FF")),
    "midnight-history": (("thorn-velvet", "Thorn & Velvet", "#9B2226"), ("silver-specters", "Silver Specters", "#577590")),
    "midnight-current": (("raven-ink", "Raven Ink", "#6D597A"), ("lantern-wraiths", "Lantern Wraiths", "#2A9D8F")),
}

ASSIGNMENT_SPECS = {
    "lantern-history": (
        ("golden-hour", ("maren.holt", "theo.bennett", "priya.shah", "lena.ortiz", "jasmine.cole", "jonah.vale")),
        ("moonlit-pages", ("caleb.ross", "nora.kim", "elliot.price", "wesley.grant", "fiona.brooks")),
    ),
    "lantern-current": (
        ("aurora-readers", ("maren.holt", "priya.shah", "nora.kim", "jasmine.cole", "wesley.grant")),
        ("comet-chasers", ("theo.bennett", "caleb.ross", "lena.ortiz", "elliot.price", "jonah.vale")),
    ),
    "midnight-history": (
        ("thorn-velvet", ("celeste.rowan", "amara.quinn", "ivy.mercer", "soraya.bell", "opal.rivera", "nora.kim")),
        ("silver-specters", ("jonah.vale", "miles.arden", "dante.frost", "lucas.wren", "henry.sloane")),
    ),
    "midnight-current": (
        ("raven-ink", ("celeste.rowan", "jonah.vale", "dante.frost", "soraya.bell", "nora.kim")),
        ("lantern-wraiths", ("amara.quinn", "miles.arden", "ivy.mercer", "lucas.wren", "opal.rivera")),
    ),
}

THEME_SPECS = {
    "lantern-history": (
        ("found-family", "Found Family Favorite", "A story where chosen family changes the characters' path.", 50, False, "Who becomes family in this story?"),
        ("short-stack", "Short-Stack Spark", "Finish a compact book with fewer than 320 pages.", 20, True, ""),
    ),
    "lantern-current": (
        ("passport-pages", "Passport Pages", "Travel somewhere memorable without leaving your reading chair.", 40, True, "Where did this book take you?"),
        ("golden-hour", "Golden Hour Finish", "Complete a book during the final light of a summer week.", 25, True, ""),
    ),
    "midnight-history": (
        ("gothic-heirloom", "Gothic Heirloom", "An inherited object carries a secret through generations.", 45, False, "What history does the object hold?"),
        ("stormbound", "Stormbound", "Weather traps the cast somewhere they would rather escape.", 25, True, ""),
    ),
    "midnight-current": (
        ("haunted-house", "Haunted House Rules", "The setting has a will—or a warning—of its own.", 60, False, "What makes the setting feel alive?"),
        ("moonlit-cover", "Moonlit Cover", "Choose a cover featuring a moon, stars, or a night sky.", 30, True, ""),
    ),
}

BOOK_SPECS = (
    ("cartographers-lantern", "The Cartographer's Lantern", "Elian Marrow", BookSubmission.Format.HARDCOVER, 384),
    ("garden-borrowed-stars", "A Garden of Borrowed Stars", "Mira Vale", BookSubmission.Format.PAPERBACK, 312),
    ("last-tea-shop", "The Last Tea Shop on Alder Street", "June Bellweather", BookSubmission.Format.PAPERBACK, 276),
    ("glass-orchard", "Whispers in the Glass Orchard", "Soren Ash", BookSubmission.Format.EBOOK, 348),
    ("clockmakers-summer", "The Clockmaker's Summer", "Ada Rook", BookSubmission.Format.HARDCOVER, 416),
    ("rivers-remember", "Rivers Remember Our Names", "Nia Solace", BookSubmission.Format.EBOOK, 296),
    ("paper-moon-society", "The Paper Moon Society", "Felix Arden", BookSubmission.Format.PAPERBACK, 336),
    ("wildflower-line", "North of the Wildflower Line", "Cora Finch", BookSubmission.Format.AUDIO, 368),
    ("quiet-storms", "The Museum of Quiet Storms", "Rowan Dusk", BookSubmission.Format.HARDCOVER, 402),
    ("saltwater-letters", "Saltwater Letters", "Elise Harbor", BookSubmission.Format.PAPERBACK, 288),
    ("briar-house", "The Briar House Guestbook", "Lydia Wren", BookSubmission.Format.HARDCOVER, 360),
    ("belladonna-hall", "Midnight at Belladonna Hall", "Silas Crowe", BookSubmission.Format.EBOOK, 328),
    ("ghostlight-conservatory", "The Ghostlight Conservatory", "Maeve Hollow", BookSubmission.Format.PAPERBACK, 304),
    ("violet-smoke", "A Study in Violet Smoke", "Orrin Blackwood", BookSubmission.Format.HARDCOVER, 392),
    ("juniper-station", "The Witches of Juniper Station", "Tamsin Reed", BookSubmission.Format.EBOOK, 344),
    ("hollow-crown", "The Hollow Crown Society", "Cassian Voss", BookSubmission.Format.HARDCOVER, 448),
    ("secret-history-rain", "The Secret History of Rain", "Imogen Frost", BookSubmission.Format.PAPERBACK, 320),
    ("lanterns-lost", "Lanterns for the Lost", "Daphne Graves", BookSubmission.Format.EBOOK, 272),
    ("ravens-shadow", "The Raven's Second Shadow", "Tobias Vale", BookSubmission.Format.HARDCOVER, 376),
    ("velvet-rooms", "Velvet Rooms and Vanishing Doors", "Celia Night", BookSubmission.Format.PAPERBACK, 352),
)


def _submission(month, username, book, day, *, verified=False, status="approved", theme=None, claim_status=None):
    return {
        "month": month,
        "username": username,
        "book": book,
        "day": day,
        "verified": verified,
        "status": status,
        "theme": theme,
        "claim_status": claim_status,
    }


SUBMISSION_SPECS = (
    _submission("lantern-history", "maren.holt", "quiet-storms", 3, verified=True, theme="found-family", claim_status="approved"),
    _submission("lantern-history", "theo.bennett", "saltwater-letters", 6, theme="short-stack", claim_status="approved"),
    _submission("lantern-history", "priya.shah", "briar-house", 9, theme="found-family", claim_status="approved"),
    _submission("lantern-history", "caleb.ross", "belladonna-hall", 12, verified=True),
    _submission("lantern-history", "lena.ortiz", "ghostlight-conservatory", 16, theme="short-stack", claim_status="approved"),
    _submission("lantern-history", "nora.kim", "violet-smoke", 20, verified=True, theme="found-family", claim_status="approved"),
    _submission("lantern-history", "elliot.price", "juniper-station", 24),
    _submission("lantern-history", "jasmine.cole", "hollow-crown", 28, verified=True),
    _submission("lantern-current", "lena.ortiz", "cartographers-lantern", 3, verified=True, theme="passport-pages", claim_status="approved"),
    _submission("lantern-current", "nora.kim", "garden-borrowed-stars", 5, theme="golden-hour", claim_status="approved"),
    _submission("lantern-current", "elliot.price", "last-tea-shop", 7, status="pending", theme="passport-pages", claim_status="pending"),
    _submission("lantern-current", "jasmine.cole", "glass-orchard", 9, verified=True),
    _submission("lantern-current", "wesley.grant", "clockmakers-summer", 11, theme="passport-pages", claim_status="pending"),
    _submission("lantern-current", "theo.bennett", "rivers-remember", 13, verified=True, theme="golden-hour", claim_status="approved"),
    _submission("lantern-current", "caleb.ross", "paper-moon-society", 15, status="pending"),
    _submission("lantern-current", "maren.holt", "wildflower-line", 17),
    _submission("lantern-current", "fiona.brooks", "saltwater-letters", 19, theme="golden-hour", claim_status="approved"),
    _submission("midnight-history", "celeste.rowan", "briar-house", 4, verified=True, theme="gothic-heirloom", claim_status="approved"),
    _submission("midnight-history", "jonah.vale", "belladonna-hall", 7, theme="stormbound", claim_status="approved"),
    _submission("midnight-history", "amara.quinn", "ghostlight-conservatory", 10),
    _submission("midnight-history", "miles.arden", "violet-smoke", 13, verified=True, theme="gothic-heirloom", claim_status="approved"),
    _submission("midnight-history", "ivy.mercer", "juniper-station", 17, theme="stormbound", claim_status="approved"),
    _submission("midnight-history", "dante.frost", "hollow-crown", 21, verified=True),
    _submission("midnight-history", "soraya.bell", "secret-history-rain", 25, theme="gothic-heirloom", claim_status="approved"),
    _submission("midnight-history", "lucas.wren", "lanterns-lost", 29, verified=True),
    _submission("midnight-current", "ivy.mercer", "briar-house", 2, verified=True, theme="haunted-house", claim_status="approved"),
    _submission("midnight-current", "dante.frost", "belladonna-hall", 5, status="pending", theme="moonlit-cover", claim_status="pending"),
    _submission("midnight-current", "soraya.bell", "ghostlight-conservatory", 8, theme="moonlit-cover", claim_status="approved"),
    _submission("midnight-current", "lucas.wren", "violet-smoke", 10, verified=True),
    _submission("midnight-current", "opal.rivera", "juniper-station", 12, theme="haunted-house", claim_status="pending"),
    _submission("midnight-current", "henry.sloane", "hollow-crown", 14, status="pending"),
    _submission("midnight-current", "celeste.rowan", "secret-history-rain", 16, theme="moonlit-cover", claim_status="approved"),
    _submission("midnight-current", "jonah.vale", "lanterns-lost", 18, verified=True),
    _submission("midnight-current", "nora.kim", "ravens-shadow", 20, theme="haunted-house", claim_status="approved"),
)


def _timestamp(day, hour=12):
    return timezone.make_aware(datetime.combine(day, time(hour=hour)))


class DemoDataSeeder:
    def __init__(self):
        self.users = {}
        self.groups = {}
        self.memberships = {}
        self.months = {}
        self.teams = {}
        self.themes = {}
        self.books = {}
        self.editions = {}

    @transaction.atomic
    def seed(self, *, reset=False):
        if reset:
            self.remove_existing()
        elif self._is_complete():
            return self.summary(created=False)
        self._assert_seed_targets_available()
        self.marker_group = AuthGroup.objects.create(name=DEMO_AUTH_GROUP)
        self._create_accounts()
        self._create_groups_and_memberships()
        self._create_months_teams_and_themes()
        self._create_catalog()
        self._create_submissions()
        manifest = {
            "dataset": DATASET_KEY,
            "accounts": {username: user.pk for username, user in self.users.items()},
            "groups": {spec["slug"]: self.groups[key].pk for key, spec in GROUP_SPECS.items()},
            "catalog_books": {key: book.pk for key, book in self.books.items()},
        }
        AuditEvent.objects.create(
            action=MANIFEST_ACTION,
            object_type=MANIFEST_OBJECT_TYPE,
            object_id=DATASET_KEY,
            summary=json.dumps(manifest, sort_keys=True),
        )
        return self.summary(created=True)

    def _manifest_event(self):
        return AuditEvent.objects.filter(
            action=MANIFEST_ACTION,
            object_type=MANIFEST_OBJECT_TYPE,
            object_id=DATASET_KEY,
        ).first()

    def _manifest(self):
        event = self._manifest_event()
        if not event:
            return None
        try:
            manifest = json.loads(event.summary)
        except (TypeError, ValueError) as exc:
            raise CommandError("The demo ownership manifest is invalid; no records were changed.") from exc
        if manifest.get("dataset") != DATASET_KEY:
            raise CommandError("The demo ownership manifest does not match this dataset; no records were changed.")
        return manifest

    def _is_complete(self):
        marker_exists = AuthGroup.objects.filter(name=DEMO_AUTH_GROUP).exists()
        manifest = self._manifest()
        if not marker_exists and not manifest:
            return False
        if not marker_exists or not manifest:
            raise CommandError("The demo dataset markers are incomplete. Run with --reset after inspecting the local database.")
        marker = AuthGroup.objects.get(name=DEMO_AUTH_GROUP)
        if marker.user_set.filter(is_superuser=True).exists():
            raise CommandError("A Platform Owner is associated with the demo marker; seeding was refused without changing any owner.")
        account_map = manifest.get("accounts", {})
        group_map = manifest.get("groups", {})
        catalog_map = manifest.get("catalog_books", {})
        expected_users = {username for username, _, _ in ACCOUNT_SPECS}
        marker_users = marker.user_set.filter(is_superuser=False)
        accounts_complete = (
            set(account_map) == expected_users
            and set(marker_users.values_list("pk", flat=True)) == set(account_map.values())
            and all(
                marker_users.filter(pk=object_id, username=username).exists()
                for username, object_id in account_map.items()
            )
        )
        expected_groups = {spec["slug"] for spec in GROUP_SPECS.values()}
        groups_complete = (
            set(group_map) == expected_groups
            and all(ReadingGroup.objects.filter(pk=object_id, slug=slug).exists() for slug, object_id in group_map.items())
        )
        expected_books = {key for key, *_ in BOOK_SPECS}
        catalog_complete = (
            set(catalog_map) == expected_books
            and all(
                CatalogBook.objects.filter(
                    pk=object_id,
                    provider=DEMO_CATALOG_PROVIDER,
                    provider_book_id=key,
                ).exists()
                for key, object_id in catalog_map.items()
            )
        )
        group_ids = list(group_map.values())
        relationships_complete = (
            Membership.objects.filter(group_id__in=group_ids).count() == sum(len(spec["members"]) for spec in GROUP_SPECS.values())
            and ChallengeMonth.objects.filter(group_id__in=group_ids).count() == len(MONTH_SPECS)
            and Team.objects.filter(month__group_id__in=group_ids).count() == sum(len(teams) for teams in TEAM_SPECS.values())
            and MonthTheme.objects.filter(month__group_id__in=group_ids).count() == sum(len(themes) for themes in THEME_SPECS.values())
            and BookSubmission.objects.filter(month__group_id__in=group_ids).count() == len(SUBMISSION_SPECS)
            and ThemeClaim.objects.filter(submission__month__group_id__in=group_ids).count()
            == sum(1 for spec in SUBMISSION_SPECS if spec["theme"])
        )
        if accounts_complete and groups_complete and catalog_complete and relationships_complete:
            return True
        raise CommandError("The marked demo dataset is incomplete or changed. Run with --reset to safely recreate it.")

    def _assert_seed_targets_available(self):
        User = get_user_model()
        usernames = [username for username, _, _ in ACCOUNT_SPECS]
        emails = [f"{username}@demo.northbound.invalid" for username in usernames]
        collisions = list(User.objects.filter(Q(username__in=usernames) | Q(email__in=emails)).values_list("username", flat=True))
        if collisions:
            raise CommandError(f"Demo account identifiers already exist without valid ownership markers: {', '.join(sorted(collisions))}.")
        slugs = [spec["slug"] for spec in GROUP_SPECS.values()]
        group_collisions = list(ReadingGroup.objects.filter(slug__in=slugs).values_list("slug", flat=True))
        if group_collisions:
            raise CommandError(f"Demo group slugs already exist without valid ownership markers: {', '.join(sorted(group_collisions))}.")
        if CatalogBook.objects.filter(provider=DEMO_CATALOG_PROVIDER).exists():
            raise CommandError("Demo catalog records exist without valid ownership markers.")
        if AuthGroup.objects.filter(name=DEMO_AUTH_GROUP).exists() or self._manifest_event():
            raise CommandError("Demo ownership markers already exist but are not complete.")

    @transaction.atomic
    def remove_existing(self):
        marker = AuthGroup.objects.filter(name=DEMO_AUTH_GROUP).first()
        manifest = self._manifest()
        if not marker and not manifest:
            return
        if not marker or not manifest:
            raise CommandError("The demo ownership markers are incomplete; reset was refused.")
        if marker.user_set.filter(is_superuser=True).exists():
            raise CommandError("A Platform Owner is associated with the demo marker; reset was refused without changing any owner.")

        account_map = manifest.get("accounts", {})
        group_map = manifest.get("groups", {})
        catalog_map = manifest.get("catalog_books", {})
        manifest_user_ids = set(account_map.values())
        marker_user_ids = set(marker.user_set.filter(is_superuser=False).values_list("pk", flat=True))
        if not marker_user_ids.issubset(manifest_user_ids):
            raise CommandError("The demo account marker contains an unowned account; reset was refused.")
        demo_users = []
        for username, object_id in account_map.items():
            user = get_user_model().objects.filter(pk=object_id).first()
            if not user:
                continue
            if user.is_superuser or user.username != username or object_id not in marker_user_ids:
                raise CommandError("A demo account fingerprint does not match its manifest; reset was refused.")
            demo_users.append(user)

        demo_groups = []
        for slug, object_id in group_map.items():
            group = ReadingGroup.objects.filter(pk=object_id).first()
            if group and group.slug != slug:
                raise CommandError("A demo group fingerprint does not match its manifest; reset was refused.")
            if group:
                demo_groups.append(group)

        for key, object_id in catalog_map.items():
            book = CatalogBook.objects.filter(pk=object_id).first()
            if book and (book.provider != DEMO_CATALOG_PROVIDER or book.provider_book_id != key):
                raise CommandError("A demo catalog fingerprint does not match its manifest; reset was refused.")

        group_ids = [group.pk for group in demo_groups]
        user_ids = [user.pk for user in demo_users]
        AuditEvent.objects.filter(actor_id__in=user_ids).delete()
        BookSubmission.objects.filter(month__group_id__in=group_ids).delete()
        ChallengeMonth.objects.filter(group_id__in=group_ids).delete()
        Membership.objects.filter(group_id__in=group_ids).delete()
        ReadingGroup.objects.filter(pk__in=group_ids).delete()
        get_user_model().objects.filter(pk__in=user_ids, is_superuser=False).delete()
        CatalogBook.objects.filter(pk__in=catalog_map.values(), provider=DEMO_CATALOG_PROVIDER).delete()
        self._manifest_event().delete()
        marker.delete()

    def _create_accounts(self):
        User = get_user_model()
        joined_at = _timestamp(date(2026, 6, 15), 10)
        for index, ((username, first_name, last_name), avatar) in enumerate(zip(ACCOUNT_SPECS, AVATARS)):
            user = User.objects.create_user(
                username=username,
                email=f"{username}@demo.northbound.invalid",
                password=DEMO_PASSWORD,
                first_name=first_name,
                last_name=last_name,
            )
            User.objects.filter(pk=user.pk).update(date_joined=joined_at + timedelta(minutes=index))
            self.marker_group.user_set.add(user)
            UserProfile.objects.create(user=user, selected_avatar=avatar)
            self.users[username] = user

    def _create_groups_and_memberships(self):
        for group_key, spec in GROUP_SPECS.items():
            group = ReadingGroup(
                name=spec["name"],
                slug=spec["slug"],
                timezone="America/New_York",
                announcement_enabled=True,
                announcement=spec["announcement"],
                access_code_visibility=ReadingGroup.AccessCodeVisibility.MEMBERS,
                join_code=spec["join_code"],
                join_code_hint=spec["join_code"][-4:],
                join_code_hash=make_password(spec["join_code"]),
            )
            group.full_clean()
            group.save()
            self.groups[group_key] = group
            owner = self.users[next(username for username, role in spec["members"].items() if role == Membership.Role.OWNER)]
            self._audit(owner, group, "group.created", "ReadingGroup", group.pk, f"Created reading group {group.name}.", date(2026, 6, 15))
            for username, role in spec["members"].items():
                user = self.users[username]
                membership = Membership.objects.create(
                    group=group,
                    user=user,
                    role=role,
                    display_name=user.get_full_name(),
                )
                self.memberships[(group_key, username)] = membership

    def _create_months_teams_and_themes(self):
        for month_key, spec in MONTH_SPECS.items():
            group = self.groups[spec["group"]]
            month = ChallengeMonth(
                group=group,
                name=spec["name"],
                starts_on=spec["starts_on"],
                ends_on=spec["ends_on"],
                late_entry_deadline=spec["starts_on"] + timedelta(days=10),
                status=spec["status"],
                team_stats_visibility=spec["visibility"],
                announcement_mode=spec["announcement_mode"],
                announcement=spec["announcement"],
            )
            month.full_clean()
            month.save()
            self.months[month_key] = month
            actor = self.users[next(username for username, role in GROUP_SPECS[spec["group"]]["members"].items() if role == Membership.Role.OWNER)]
            self._audit(actor, group, "month.created", "ChallengeMonth", month.pk, f"Created {month.name}.", spec["starts_on"])

            if spec["status"] == ChallengeMonth.Status.OPEN:
                reviewer_username = GROUP_SPECS[spec["group"]]["reviewer"]
                host = ChallengeStaffAssignment.objects.create(
                    month=month,
                    membership=self.memberships[(spec["group"], reviewer_username)],
                    role=ChallengeStaffAssignment.Role.HOST,
                    assigned_by=actor,
                )
                self._audit(
                    actor,
                    group,
                    "challenge.host_assigned",
                    "ChallengeStaffAssignment",
                    host.pk,
                    f"Assigned {host.membership.display_name} as a Host for {month.name}.",
                    spec["starts_on"],
                )

            for team_key, name, color in TEAM_SPECS[month_key]:
                self.teams[(month_key, team_key)] = Team.objects.create(month=month, name=name, color=color)

            for username in GROUP_SPECS[spec["group"]]["members"]:
                enrollment = MonthEnrollment(
                    month=month,
                    participant=self.memberships[(spec["group"], username)],
                    enrolled_by=actor,
                )
                enrollment.full_clean()
                enrollment.save()

            for team_key, usernames in ASSIGNMENT_SPECS[month_key]:
                team = self.teams[(month_key, team_key)]
                for username in usernames:
                    TeamAssignment.objects.create(
                        month=month,
                        team=team,
                        participant=self.memberships[(spec["group"], username)],
                    )

            for theme_key, name, description, bonus, stacking, prompt in THEME_SPECS[month_key]:
                theme = MonthTheme(
                    month=month,
                    name=name,
                    description=description,
                    starts_on=month.starts_on,
                    ends_on=month.ends_on,
                    bonus_pages=bonus,
                    allow_stacking=stacking,
                    prompt=prompt,
                    is_active=True,
                    is_visible=True,
                )
                theme.full_clean()
                theme.save()
                self.themes[(month_key, theme_key)] = theme

    def _create_catalog(self):
        format_names = {
            BookSubmission.Format.PAPERBACK: "Paperback",
            BookSubmission.Format.HARDCOVER: "Hardcover",
            BookSubmission.Format.EBOOK: "E-book",
            BookSubmission.Format.AUDIO: "Audiobook",
        }
        for key, title, author, book_format, pages in BOOK_SPECS:
            book = CatalogBook.objects.create(
                provider=DEMO_CATALOG_PROVIDER,
                provider_book_id=key,
                title=title,
                author=author,
            )
            selected = CatalogEdition.objects.create(
                book=book,
                provider=DEMO_CATALOG_PROVIDER,
                provider_edition_id=f"{key}-selected",
                format_name=format_names[book_format],
                page_count=None if book_format == BookSubmission.Format.AUDIO else pages,
                audio_seconds=39600 if book_format == BookSubmission.Format.AUDIO else None,
            )
            scoring = selected
            if book_format == BookSubmission.Format.AUDIO:
                scoring = CatalogEdition.objects.create(
                    book=book,
                    provider=DEMO_CATALOG_PROVIDER,
                    provider_edition_id=f"{key}-scoring",
                    format_name="E-book",
                    page_count=pages,
                )
            self.books[key] = book
            self.editions[key] = (selected, scoring)

    def _create_submissions(self):
        book_specs = {key: (title, author, book_format, pages) for key, title, author, book_format, pages in BOOK_SPECS}
        for index, spec in enumerate(SUBMISSION_SPECS):
            month = self.months[spec["month"]]
            group_key = MONTH_SPECS[spec["month"]]["group"]
            participant = self.memberships[(group_key, spec["username"])]
            title, author, book_format, pages = book_specs[spec["book"]]
            approved = spec["status"] == "approved"
            selected, scoring = self.editions[spec["book"]]
            submission = BookSubmission(
                month=month,
                participant=participant,
                title=title,
                author=author,
                book_format=book_format,
                started_on=max(month.starts_on, date(month.starts_on.year, month.starts_on.month, max(1, spec["day"] - 8))),
                completed_on=date(month.starts_on.year, month.starts_on.month, spec["day"]),
                submitted_pages=pages,
                approved_pages=pages if approved else None,
                status=BookSubmission.Status.APPROVED if approved else BookSubmission.Status.PENDING,
                verification_method=BookSubmission.VerificationMethod.MANUAL,
                notes="Demo reading entry with fictional catalog data.",
            )
            if spec["verified"]:
                submission.catalog_book = self.books[spec["book"]]
                submission.catalog_edition = selected
                submission.scoring_catalog_edition = scoring
                submission.metadata_pages = pages
                submission.verification_method = (
                    BookSubmission.VerificationMethod.HARDCOVER_AUDIO
                    if book_format == BookSubmission.Format.AUDIO
                    else BookSubmission.VerificationMethod.HARDCOVER
                )
                submission.reviewed_at = _timestamp(submission.completed_on, 18)
                submission.review_notes = "Automatically approved from the canonical demo catalog edition."
            elif approved:
                reviewer = self.users[GROUP_SPECS[group_key]["reviewer"]]
                submission.reviewed_by = reviewer
                submission.reviewed_at = _timestamp(submission.completed_on, 19)
                submission.review_notes = "Page count confirmed during demo moderation review."
            submission.full_clean()
            submission.save()
            BookSubmission.objects.filter(pk=submission.pk).update(
                submitted_at=_timestamp(submission.completed_on, 17) + timedelta(minutes=index)
            )

            if spec["theme"]:
                theme = self.themes[(spec["month"], spec["theme"])]
                claim_status = ThemeClaim.Status.APPROVED if spec["claim_status"] == "approved" else ThemeClaim.Status.PENDING
                reviewer = self.users[GROUP_SPECS[group_key]["reviewer"]] if claim_status == ThemeClaim.Status.APPROVED else None
                claim = ThemeClaim(
                    submission=submission,
                    theme=theme,
                    response=(
                        "The setting and relationships made this theme a natural fit."
                        if theme.prompt
                        else ""
                    ),
                    status=claim_status,
                    approved_bonus_pages=theme.bonus_pages if claim_status == ThemeClaim.Status.APPROVED and approved else 0,
                    reviewed_by=reviewer,
                    reviewed_at=_timestamp(submission.completed_on, 20) if reviewer else None,
                )
                claim.full_clean()
                claim.save()
            submission.recalculate_score()
            self._audit(
                participant.user,
                month.group,
                "submission.created",
                "BookSubmission",
                submission.pk,
                f"Submitted {submission.title}.",
                submission.completed_on,
            )
            if approved and not spec["verified"]:
                self._audit(
                    self.users[GROUP_SPECS[group_key]["reviewer"]],
                    month.group,
                    "submission.approved",
                    "BookSubmission",
                    submission.pk,
                    f"Approved: {submission.title}; approved pages: {pages}.",
                    submission.completed_on,
                    hour=20,
                )

    def _audit(self, actor, group, action, object_type, object_id, summary, day, hour=12):
        event = AuditEvent.objects.create(
            actor=actor,
            group=group,
            action=action,
            object_type=object_type,
            object_id=str(object_id),
            summary=summary,
        )
        AuditEvent.objects.filter(pk=event.pk).update(created_at=_timestamp(day, hour))
        return event

    def summary(self, *, created):
        return {
            "created": created,
            "accounts": len(ACCOUNT_SPECS),
            "groups": len(GROUP_SPECS),
            "memberships": sum(len(spec["members"]) for spec in GROUP_SPECS.values()),
            "months": len(MONTH_SPECS),
            "teams": sum(len(teams) for teams in TEAM_SPECS.values()),
            "themes": sum(len(themes) for themes in THEME_SPECS.values()),
            "books": len(BOOK_SPECS),
            "submissions": len(SUBMISSION_SPECS),
            "claims": sum(1 for spec in SUBMISSION_SPECS if spec["theme"]),
            "platform_owners": get_user_model().objects.filter(is_superuser=True).count(),
            "platform_owner_invitations": PlatformOwnerInvitation.objects.count(),
        }
