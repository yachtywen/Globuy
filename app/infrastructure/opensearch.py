"""OpenSearch client construction for the BaseStore application layer."""

from urllib.parse import urlparse

from opensearchpy import OpenSearch

from app.config import Settings, get_settings


def build_opensearch_client(settings: Settings | None = None) -> OpenSearch:
    settings = settings or get_settings()
    target = urlparse(settings.opensearch_url)
    if not target.hostname:
        raise ValueError("GLOBUY_OPENSEARCH_URL 缺少主机名")
    use_ssl = target.scheme == "https"
    return OpenSearch(
        hosts=[{"host": target.hostname, "port": target.port or (443 if use_ssl else 9200)}],
        use_ssl=use_ssl,
        verify_certs=use_ssl,
        ssl_assert_hostname=use_ssl,
        ssl_show_warn=use_ssl,
        timeout=settings.opensearch_timeout_seconds,
        max_retries=0,
        retry_on_timeout=False,
    )
