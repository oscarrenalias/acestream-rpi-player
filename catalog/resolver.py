"""Site-specific stream resolution hook.

This is intentionally the only incomplete part of the catalog. Implement
``resolve_streams`` to inspect an event page and return any AceStream content
IDs or ``acestream://`` URLs found there.
"""

from typing import Mapping
import httpx
import re

ACESTREAM_LINK = re.compile(
    r"acestream://([0-9a-f]{40})(?![0-9a-f])",
    re.IGNORECASE,
)

def fetch_page(url: str, verify_tls: bool = True) -> tuple[str, str]:
   with httpx.Client(
          timeout=20,
          follow_redirects=True,
          verify=verify_tls,
          headers={"User-Agent": "EventCatalog/0.1"},
   ) as client:
          response = client.get(url)
          response.raise_for_status()
          return response.text, str(response.url)

def parse_event_page(html: str, base_url: str) -> list[str]:
    """Extract AceStream references from one event page.

    Keep this function free of network access so it can be tested against saved
    HTML fixtures. Replace the stub with the site-specific parsing logic.
    """

    del base_url

    # dict preserves discovery order while removing duplicates.
    content_ids = dict.fromkeys(
         match.group(1).lower()
         for match in ACESTREAM_LINK.finditer(html)
    )

    return [f"acestream://{content_id}" for content_id in content_ids]


def resolve_streams(event: Mapping[str, str], *, verify_tls: bool = True) -> list[str]:
    """Return AceStream references for one normalized event.

    ``event`` contains ``title``, ``link``, ``category`` and ``starts_at``.
    Returning an empty list stores the event without any playlist entries.
    """

    # Fetch event["link"] here, then pass the response body and final URL to
    # parse_event_page. Network behavior remains site-specific and is therefore
    # deliberately left alongside the parser stub.

    html, final_url = fetch_page(event["link"], verify_tls=verify_tls)

    return parse_event_page(html, final_url)
