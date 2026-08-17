"""Site-specific stream resolution hook.

This is intentionally the only incomplete part of the catalog. Implement
``resolve_streams`` to inspect an event page and return ``(metadata,
acestream_reference)`` tuples. Metadata is a short optional label, such as a
stream language; use an empty string when the page does not provide one.
"""

from typing import Mapping
from html.parser import HTMLParser
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

Stream = tuple[str, str]


class _LinkTableParser(HTMLParser):
    """Extract links from LiveTV's per-stream ``lnktbj`` tables."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._tables: list[dict[str, object] | None] = []
        self.streams: list[Stream] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "table":
            classes = attributes.get("class", "") or ""
            self._tables.append(
                {"metadata": "", "references": []} if "lnktbj" in classes.split() else None
            )
            return

        table = self._current_link_table()
        if table is None:
            return
        if tag == "img":
            source = attributes.get("src", "") or ""
            metadata = attributes.get("title", "") or ""
            if "/img/linkflag/" in source and metadata:
                table["metadata"] = metadata.strip()
        elif tag == "a":
            reference = attributes.get("href", "") or ""
            table["references"].extend(
                f"acestream://{match.group(1).lower()}"
                for match in ACESTREAM_LINK.finditer(reference)
            )

    def handle_endtag(self, tag: str) -> None:
        if tag != "table" or not self._tables:
            return
        table = self._tables.pop()
        if table is None:
            return
        metadata = str(table["metadata"])
        self.streams.extend((metadata, reference) for reference in table["references"])

    def _current_link_table(self) -> dict[str, object] | None:
        return next((table for table in reversed(self._tables) if table is not None), None)


def parse_event_page(html: str, base_url: str) -> list[Stream]:
    """Extract AceStream references from one event page.

    Keep this function free of network access so it can be tested against saved
    HTML fixtures. Replace the stub with the site-specific parsing logic.
    """

    del base_url

    parser = _LinkTableParser()
    parser.feed(html)
    parser.close()

    # Some pages may expose a valid reference outside a standard link table.
    # Keep those links, with no metadata, rather than silently losing them.
    found_ids = {reference.removeprefix("acestream://") for _, reference in parser.streams}
    for match in ACESTREAM_LINK.finditer(html):
        content_id = match.group(1).lower()
        if content_id not in found_ids:
            parser.streams.append(("", f"acestream://{content_id}"))

    # dict preserves discovery order while removing duplicate metadata/link pairs.
    return list(dict.fromkeys(parser.streams))


def resolve_streams(event: Mapping[str, str], *, verify_tls: bool = True) -> list[Stream]:
    """Return metadata and AceStream references for one normalized event.

    ``event`` contains ``title``, ``link``, ``category`` and ``starts_at``.
    The first tuple element is optional display metadata and the second is an
    AceStream content ID or URL. Returning an empty list stores the event
    without any playlist entries.
    """

    # Fetch event["link"] here, then pass the response body and final URL to
    # parse_event_page. Network behavior remains site-specific and is therefore
    # deliberately left alongside the parser stub.

    html, final_url = fetch_page(event["link"], verify_tls=verify_tls)

    return parse_event_page(html, final_url)
