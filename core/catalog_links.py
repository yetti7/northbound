from urllib.parse import urlsplit


HARDCOVER_HOSTS = {"hardcover.app", "www.hardcover.app"}


def safe_hardcover_url(*, provider, source_url):
    """Return a public Hardcover book URL only from authoritative catalog data."""
    if provider != "hardcover" or not source_url:
        return ""
    try:
        parsed = urlsplit(source_url)
    except ValueError:
        return ""
    if (
        parsed.scheme != "https"
        or parsed.hostname not in HARDCOVER_HOSTS
        or parsed.username
        or parsed.password
        or not parsed.path.startswith("/books/")
    ):
        return ""
    return f"https://hardcover.app{parsed.path.rstrip('/')}"


def catalog_hardcover_url(*, catalog_book=None, catalog_edition=None):
    if catalog_edition is not None:
        url = safe_hardcover_url(
            provider=catalog_edition.provider,
            source_url=catalog_edition.source_url,
        )
        if url:
            return url
    if catalog_book is not None:
        return safe_hardcover_url(
            provider=catalog_book.provider,
            source_url=catalog_book.source_url,
        )
    return ""
