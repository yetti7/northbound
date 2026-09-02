import hashlib
import json
import re
from datetime import timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request
from .http import urlopen

from django.conf import settings
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from core.models import CatalogBook, CatalogEdition, CatalogSearchCache


class HardcoverConnectionError(Exception):
    def __init__(self, message, *, classification="provider_rejected", retryable=False, reconnect_required=False):
        super().__init__(message)
        self.classification = classification
        self.retryable = retryable
        self.reconnect_required = reconnect_required


class HardcoverLinkError(ValueError):
    pass


TEST_QUERY = 'query { search(query: "northbound", query_type: "Book", per_page: 1, page: 1) { ids } }'
SEARCH_QUERY = 'query Search($query: String!, $perPage: Int!) { search(query: $query, query_type: "Book", per_page: $perPage, page: 1) { ids results } }'
EDITION_QUERY = 'query Edition($id: Int!) { editions_by_pk(id: $id) { id book_id title subtitle isbn_10 isbn_13 pages audio_seconds edition_format physical_format release_date cached_contributors image { url } book { id title slug } } }'
BOOK_BY_SLUG_QUERY = 'query BookBySlug($slug: String!) { books(where: {slug: {_eq: $slug}}, limit: 1) { id title subtitle slug pages cached_contributors image { url } } }'
BOOK_EDITIONS_QUERY = 'query BookEditions($bookId: Int!) { editions(where: {book_id: {_eq: $bookId}}, order_by: [{users_count: desc}], limit: 20) { id title isbn_10 isbn_13 pages audio_seconds users_count edition_format physical_format release_date } }'

HARDCOVER_LINK_PATTERN = re.compile(r"^/books/(?P<slug>[^/]+)(?:/editions/(?P<edition_id>\d+))?/?$")
SEARCH_CACHE_TTL = timedelta(hours=24)
EDITION_CACHE_TTL = timedelta(days=30)


@sensitive_variables()
def execute_graphql(token, query, variables=None):
    authorization = token if token.lower().startswith("bearer ") else f"Bearer {token}"
    request = Request(
        settings.HARDCOVER_GRAPHQL_URL,
        data=json.dumps({"query": query, "variables": variables or {}}).encode("utf-8"),
        headers={"Authorization": authorization, "Content-Type": "application/json", "User-Agent": "Northbound Reading Challenges/0.1"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code == 401:
            raise HardcoverConnectionError("Hardcover rejected the token.", classification="credential_rejected", reconnect_required=True) from exc
        if exc.code == 403:
            raise HardcoverConnectionError("The token does not have the required permissions.", classification="insufficient_permission", reconnect_required=True) from exc
        if exc.code == 429:
            raise HardcoverConnectionError("Hardcover's rate limit was reached. Try again later.", classification="rate_limited", retryable=True) from exc
        if exc.code >= 500:
            raise HardcoverConnectionError("Hardcover is temporarily unavailable.", classification="provider_unavailable", retryable=True) from exc
        raise HardcoverConnectionError(f"Hardcover returned HTTP {exc.code}.", classification="provider_rejected") from exc
    except (URLError, TimeoutError) as exc:
        raise HardcoverConnectionError("Hardcover could not be reached.", classification="temporary_network", retryable=True) from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HardcoverConnectionError("Hardcover returned an unreadable response.", classification="unreadable_response", retryable=True) from exc
    if not isinstance(payload, dict):
        raise HardcoverConnectionError("Hardcover returned an unexpected response.", classification="unreadable_response", retryable=True)
    if payload.get("errors"):
        raise HardcoverConnectionError("Hardcover rejected the catalog query.")
    if not isinstance(payload.get("data"), dict):
        raise HardcoverConnectionError("Hardcover did not return the expected catalog response.")
    return payload["data"]


def test_catalog_connection(token):
    data = execute_graphql(token, TEST_QUERY)
    if "search" not in data:
        raise HardcoverConnectionError("Hardcover did not return the expected catalog response.")
    return True


def parse_hardcover_url(value):
    try:
        parsed = urlparse(value.strip())
    except (AttributeError, ValueError) as exc:
        raise HardcoverLinkError("Enter a valid Hardcover book or edition URL.") from exc
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"hardcover.app", "www.hardcover.app"}:
        raise HardcoverLinkError("Enter a hardcover.app book or edition URL.")
    match = HARDCOVER_LINK_PATTERN.fullmatch(parsed.path)
    if not match:
        raise HardcoverLinkError("The link must point to a Hardcover book or edition.")
    return {"slug": match.group("slug"), "edition_id": int(match.group("edition_id")) if match.group("edition_id") else None}


def _author_names(value):
    if not isinstance(value, list):
        return ""
    names = []
    for item in value:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict):
            author = item.get("author") or {}
            if author.get("name"):
                names.append(author["name"])
    return ", ".join(names)


def _image_url(value):
    return value.get("url", "") if isinstance(value, dict) else ""


def _normalize_search_hit(hit):
    document = hit.get("document", {}) if isinstance(hit, dict) else {}
    slug = document.get("slug", "")
    return {
        "provider": "hardcover",
        "book_id": str(document.get("id", "")),
        "title": document.get("title", ""),
        "subtitle": document.get("subtitle") or "",
        "author": _author_names(document.get("author_names", [])),
        "default_pages": document.get("pages"),
        "cover_url": _image_url(document.get("image")),
        "source_url": f"https://hardcover.app/books/{slug}" if slug else "",
    }


def search_books(token, query, per_page=8):
    query = " ".join(query.split())
    if not query:
        raise ValueError("Enter a title, author, or ISBN.")
    per_page = max(1, min(int(per_page), 10))
    cache_key = hashlib.sha256(f"{query.casefold()}|{per_page}".encode("utf-8")).hexdigest()
    now = timezone.now()
    CatalogSearchCache.objects.filter(expires_at__lte=now).delete()
    cached = CatalogSearchCache.objects.filter(query_hash=cache_key, expires_at__gt=now).first()
    if cached:
        return cached.results, True
    data = execute_graphql(token, SEARCH_QUERY, {"query": query, "perPage": per_page})
    raw_results = (data.get("search") or {}).get("results") or {}
    hits = raw_results.get("hits", []) if isinstance(raw_results, dict) else []
    results = [_normalize_search_hit(hit) for hit in hits[:per_page]]
    CatalogSearchCache.objects.update_or_create(query_hash=cache_key, defaults={"query_text": query[:300], "results": results, "expires_at": now + SEARCH_CACHE_TTL})
    return results, False


def _edition_payload(edition):
    return {
        "provider": edition.provider,
        "book_id": edition.book.provider_book_id,
        "edition_id": edition.provider_edition_id,
        "title": edition.book.title,
        "subtitle": edition.book.subtitle,
        "author": edition.book.author,
        "isbn_10": edition.isbn_10,
        "isbn_13": edition.isbn_13,
        "format": edition.format_name,
        "pages": edition.page_count,
        "audio_seconds": edition.audio_seconds,
        "cover_url": edition.book.cover_url,
        "source_url": edition.source_url,
    }


def lookup_edition(token, edition_id):
    edition_id = str(int(edition_id))
    fresh_after = timezone.now() - EDITION_CACHE_TTL
    cached = CatalogEdition.objects.select_related("book").filter(provider="hardcover", provider_edition_id=edition_id, refreshed_at__gte=fresh_after).first()
    if cached:
        return _edition_payload(cached), True
    item = execute_graphql(token, EDITION_QUERY, {"id": int(edition_id)}).get("editions_by_pk")
    if not item:
        raise HardcoverConnectionError("That Hardcover edition could not be found.")
    book_data = item.get("book") or {}
    book_id = str(item.get("book_id") or book_data.get("id") or "")
    slug = book_data.get("slug", "")
    title = item.get("title") or book_data.get("title") or "Untitled"
    source_url = f"https://hardcover.app/books/{slug}/editions/{edition_id}" if slug else ""
    book, _ = CatalogBook.objects.update_or_create(
        provider="hardcover", provider_book_id=book_id,
        defaults={"title": title, "author": _author_names(item.get("cached_contributors", [])), "subtitle": item.get("subtitle") or "", "cover_url": _image_url(item.get("image")), "source_url": f"https://hardcover.app/books/{slug}" if slug else ""},
    )
    edition, _ = CatalogEdition.objects.update_or_create(
        provider="hardcover", provider_edition_id=edition_id,
        defaults={"book": book, "isbn_10": item.get("isbn_10") or "", "isbn_13": item.get("isbn_13") or "", "format_name": item.get("edition_format") or item.get("physical_format") or "", "page_count": item.get("pages"), "audio_seconds": item.get("audio_seconds"), "users_count": item.get("users_count") or 0, "source_url": source_url},
    )
    return _edition_payload(edition), False


def lookup_hardcover_url(token, value):
    parsed = parse_hardcover_url(value)
    if parsed["edition_id"]:
        return lookup_edition(token, parsed["edition_id"])
    books = execute_graphql(token, BOOK_BY_SLUG_QUERY, {"slug": parsed["slug"]}).get("books") or []
    if not books:
        raise HardcoverConnectionError("That Hardcover book could not be found.")
    book = books[0]
    return {
        "provider": "hardcover", "book_id": str(book.get("id", "")), "title": book.get("title", ""),
        "subtitle": book.get("subtitle") or "", "author": _author_names(book.get("cached_contributors", [])),
        "default_pages": book.get("pages"), "cover_url": _image_url(book.get("image")),
        "source_url": f"https://hardcover.app/books/{book.get('slug', parsed['slug'])}", "edition_required": True,
    }, False


def list_book_editions(token, book_id):
    editions = execute_graphql(token, BOOK_EDITIONS_QUERY, {"bookId": int(book_id)}).get("editions") or []
    return [{
        "edition_id": str(item.get("id", "")),
        "title": item.get("title") or "",
        "isbn_10": item.get("isbn_10") or "",
        "isbn_13": item.get("isbn_13") or "",
        "pages": item.get("pages"),
        "audio_seconds": item.get("audio_seconds"),
        "users_count": item.get("users_count") or 0,
        "format": item.get("edition_format") or item.get("physical_format") or "Edition",
        "release_date": item.get("release_date") or "",
    } for item in editions]


def resolve_scoring_edition(token, selected):
    selected_format = (selected.get("format") or "").casefold()
    is_audio = "audio" in selected_format or bool(selected.get("audio_seconds"))
    if not is_audio:
        if selected.get("pages"):
            return selected, "hardcover"
        return None, None

    editions = list_book_editions(token, selected["book_id"])
    valid = [edition for edition in editions if edition.get("pages")]
    ebook = next((edition for edition in valid if any(term in edition["format"].casefold() for term in ("ebook", "e-book", "kindle", "digital"))), None)
    print_edition = next((edition for edition in valid if any(term in edition["format"].casefold() for term in ("paperback", "hardcover", "hardback", "physical"))), None)
    candidate = ebook or print_edition
    if not candidate:
        return None, None
    scoring, _ = lookup_edition(token, candidate["edition_id"])
    return scoring, "hardcover_audio"
