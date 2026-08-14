from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import threading
from contextlib import closing
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from string import Formatter
from typing import Callable, Iterable, Mapping, Sequence
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree

import yaml

from catalog.resolver import resolve_streams

LOGGER = logging.getLogger("catalog")
DEFAULT_FEED_URL = "https://cdn.livetv903.me/rss/upcoming_en.xml"
CONTENT_ID = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
MAX_FEED_BYTES = 8 * 1024 * 1024
DEFAULT_CONFIG_PATH = Path(__file__).with_name("config.yaml")

Event = dict[str, str]
Resolver = Callable[[Mapping[str, str]], list[str]]


@dataclass(frozen=True)
class RefreshSettings:
    discovery_interval_seconds: int = 3600
    stream_interval_seconds: int = 300
    stream_lookback_hours: int = 2
    stream_window_hours: int = 24
    retention_hours: int = 12


def fetch_feed(url: str, timeout: float = 20) -> bytes:
    request = Request(url, headers={"User-Agent": "acestream-event-catalog/1.0"})
    with urlopen(request, timeout=timeout) as response:
        content_type = response.headers.get_content_type()
        if content_type not in {"application/rss+xml", "application/xml", "text/xml"}:
            LOGGER.warning("unexpected feed content type: %s", content_type)
        payload = response.read(MAX_FEED_BYTES + 1)
    if len(payload) > MAX_FEED_BYTES:
        raise ValueError("feed exceeds the 8 MiB size limit")
    return payload


def parse_feed(payload: bytes) -> list[Event]:
    lowered = payload.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise ValueError("DTD and entity declarations are not accepted")

    root = ElementTree.fromstring(payload)
    events: list[Event] = []
    for item in root.findall("./channel/item"):
        title = _text(item, "title")
        link = _text(item, "link")
        description = _text(item, "description")
        published = _text(item, "pubDate")
        if not all((title, link, published)):
            LOGGER.warning("skipping incomplete RSS item")
            continue
        parsed_link = urlparse(link)
        if parsed_link.scheme not in {"http", "https"} or not parsed_link.netloc:
            LOGGER.warning("skipping item with invalid event URL: %s", link)
            continue
        starts_at = parsedate_to_datetime(published)
        if starts_at.tzinfo is None:
            raise ValueError(f"event date has no timezone: {published}")
        events.append(
            {
                "title": title,
                "link": link,
                "category": description.split(".", 1)[0].strip() or "Other",
                "description": description,
                "starts_at": starts_at.isoformat(),
            }
        )
    return events


def _text(item: ElementTree.Element, name: str) -> str:
    return (item.findtext(name) or "").strip()


def content_id(reference: str) -> str:
    value = reference.strip()
    if CONTENT_ID.fullmatch(value):
        return value.lower()
    parsed = urlparse(value)
    if parsed.scheme == "acestream" and CONTENT_ID.fullmatch(parsed.netloc):
        return parsed.netloc.lower()
    query_id = parse_qs(parsed.query).get("id", [""])[0]
    if CONTENT_ID.fullmatch(query_id):
        return query_id.lower()
    raise ValueError(f"invalid AceStream reference: {reference!r}")


def load_config(path: str | Path) -> dict[str, object]:
    config_path = Path(path)
    if not config_path.is_file():
        raise ValueError(f"configuration file does not exist: {config_path}")
    with config_path.open(encoding="utf-8") as file:
        document = yaml.safe_load(file) or {}
    if not isinstance(document, dict):
        raise ValueError("configuration root must be a mapping")
    return document


def load_ignore_patterns(path: str | Path) -> list[str]:
    patterns = load_config(path).get("ignore", [])
    if not isinstance(patterns, list) or not all(isinstance(pattern, str) for pattern in patterns):
        raise ValueError("configuration 'ignore' must be a list of strings")
    return [pattern.strip() for pattern in patterns if pattern.strip()]


def load_event_tls_verify(path: str | Path) -> bool:
    event_requests = load_config(path).get("event_requests", {})
    if not isinstance(event_requests, dict):
        raise ValueError("configuration 'event_requests' must be a mapping")
    verify_tls = event_requests.get("verify_tls", True)
    if not isinstance(verify_tls, bool):
        raise ValueError("configuration 'event_requests.verify_tls' must be true or false")
    return verify_tls


def load_refresh_settings(path: str | Path) -> RefreshSettings:
    refresh = load_config(path).get("refresh", {})
    if not isinstance(refresh, dict):
        raise ValueError("configuration 'refresh' must be a mapping")
    allowed = set(asdict(RefreshSettings()))
    unknown = set(refresh) - allowed
    if unknown:
        raise ValueError(f"unknown refresh setting(s): {', '.join(sorted(unknown))}")
    values: dict[str, int] = {}
    for name, value in refresh.items():
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError(f"configuration 'refresh.{name}' must be a positive integer")
        values[name] = value
    return RefreshSettings(**values)


def load_playlist_url_template(path: str | Path) -> str:
    playlist_config = load_config(path).get("playlist", {})
    if not isinstance(playlist_config, dict):
        raise ValueError("configuration 'playlist' must be a mapping")
    template = playlist_config.get("stream_url_template", "acestream://{content_id}")
    if not isinstance(template, str):
        raise ValueError("configuration 'playlist.stream_url_template' must be a string")
    fields = {field for _, field, _, _ in Formatter().parse(template) if field is not None}
    if "content_id" not in fields or not fields <= {"content_id", "base_url"}:
        raise ValueError(
            "configuration 'playlist.stream_url_template' may use only {content_id} and {base_url}"
        )
    return template


def ignored_by(event: Mapping[str, str], patterns: Sequence[str]) -> str | None:
    searchable = " ".join(
        event.get(field, "") for field in ("title", "category", "description")
    ).casefold()
    return next((pattern for pattern in patterns if pattern.casefold() in searchable), None)


class Catalog:
    def __init__(self, database: str | Path):
        self.database = str(database)
        Path(self.database).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with closing(self.connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY,
                    title TEXT NOT NULL,
                    event_url TEXT NOT NULL UNIQUE,
                    category TEXT NOT NULL,
                    starts_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_checked_at TEXT
                );
                CREATE TABLE IF NOT EXISTS streams (
                    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                    content_id TEXT NOT NULL,
                    PRIMARY KEY (event_id, content_id)
                );
                CREATE TABLE IF NOT EXISTS refresh_status (
                    name TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    state TEXT NOT NULL,
                    detail TEXT
                );
                CREATE INDEX IF NOT EXISTS events_starts_at ON events(starts_at);
                CREATE INDEX IF NOT EXISTS events_category ON events(category);
                """
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(events)")}
            if "last_checked_at" not in columns:
                connection.execute("ALTER TABLE events ADD COLUMN last_checked_at TEXT")

    def upsert_event(self, event: Mapping[str, str]) -> None:
        now = datetime.now().astimezone().isoformat()
        with closing(self.connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO events(title, event_url, category, starts_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(event_url) DO UPDATE SET
                    title=excluded.title,
                    category=excluded.category,
                    starts_at=excluded.starts_at,
                    updated_at=excluded.updated_at
                """,
                (event["title"], event["link"], event["category"], event["starts_at"], now),
            )

    def set_streams(self, event_url: str, stream_ids: Iterable[str]) -> None:
        unique_ids = sorted(set(stream_ids))
        checked_at = datetime.now().astimezone().isoformat()
        with closing(self.connect()) as connection, connection:
            row = connection.execute("SELECT id FROM events WHERE event_url = ?", (event_url,)).fetchone()
            if row is None:
                raise ValueError(f"cannot save streams for undiscovered event: {event_url}")
            event_id = row["id"]
            connection.execute("DELETE FROM streams WHERE event_id = ?", (event_id,))
            connection.executemany(
                "INSERT INTO streams(event_id, content_id) VALUES (?, ?)",
                ((event_id, stream_id) for stream_id in unique_ids),
            )
            connection.execute(
                "UPDATE events SET last_checked_at = ? WHERE id = ?", (checked_at, event_id)
            )

    def save(self, event: Mapping[str, str], stream_ids: Iterable[str]) -> None:
        """Convenience method retained for tests and one-off imports."""
        self.upsert_event(event)
        self.set_streams(event["link"], stream_ids)

    def events_for_resolution(self, start: datetime, end: datetime) -> list[Event]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                "SELECT title, event_url, category, starts_at FROM events ORDER BY starts_at, title"
            ).fetchall()
        events = []
        for row in rows:
            starts_at = datetime.fromisoformat(row["starts_at"])
            if start <= starts_at <= end:
                events.append(
                    {
                        "title": row["title"],
                        "link": row["event_url"],
                        "category": row["category"],
                        "starts_at": row["starts_at"],
                    }
                )
        return events

    def remove_before(self, cutoff: datetime) -> int:
        with closing(self.connect()) as connection, connection:
            rows = connection.execute("SELECT id, starts_at FROM events").fetchall()
            event_ids = [
                row["id"]
                for row in rows
                if datetime.fromisoformat(row["starts_at"]) < cutoff
            ]
            connection.executemany("DELETE FROM events WHERE id = ?", ((event_id,) for event_id in event_ids))
        return len(event_ids)

    def events(self, category: str = "", on_date: str = "") -> list[dict[str, object]]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT e.id, e.title, e.event_url, e.category, e.starts_at,
                       e.last_checked_at, s.content_id
                FROM events e LEFT JOIN streams s ON s.event_id = e.id
                ORDER BY e.starts_at, e.title, s.content_id
                """
            ).fetchall()
        grouped: dict[int, dict[str, object]] = {}
        for row in rows:
            if category and row["category"].casefold() != category.casefold():
                continue
            if on_date and datetime.fromisoformat(row["starts_at"]).date().isoformat() != on_date:
                continue
            event = grouped.setdefault(
                row["id"],
                {
                    "title": row["title"],
                    "link": row["event_url"],
                    "category": row["category"],
                    "starts_at": row["starts_at"],
                    "last_checked_at": row["last_checked_at"],
                    "content_ids": [],
                },
            )
            if row["content_id"] is not None:
                event["content_ids"].append(row["content_id"])
        return list(grouped.values())

    def mark_refresh(self, name: str, state: str, detail: str = "") -> None:
        now = datetime.now().astimezone().isoformat()
        completed_at = now if state in {"success", "error"} else None
        with closing(self.connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO refresh_status(name, started_at, completed_at, state, detail)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    started_at=excluded.started_at,
                    completed_at=excluded.completed_at,
                    state=excluded.state,
                    detail=excluded.detail
                """,
                (name, now, completed_at, state, detail),
            )

    def statuses(self) -> list[dict[str, str | None]]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                "SELECT name, started_at, completed_at, state, detail FROM refresh_status ORDER BY name"
            ).fetchall()
        return [dict(row) for row in rows]


def discover_events(
    feed_url: str,
    catalog: Catalog,
    ignore_patterns: Sequence[str],
    max_events: int | None = None,
) -> int:
    LOGGER.info("fetching RSS feed: %s", feed_url)
    events = parse_feed(fetch_feed(feed_url))
    LOGGER.info("discovered %d events in RSS feed", len(events))
    if max_events is not None:
        events = events[:max_events]
        LOGGER.info("limited discovery to %d event(s)", len(events))
    saved = 0
    ignored = 0
    for event in events:
        pattern = ignored_by(event, ignore_patterns)
        if pattern:
            ignored += 1
            continue
        catalog.upsert_event(event)
        saved += 1
    LOGGER.info("discovery complete: %d events saved; %d ignored", saved, ignored)
    return saved


def resolve_active_events(
    catalog: Catalog,
    resolver: Resolver,
    settings: RefreshSettings,
    now: datetime | None = None,
) -> int:
    now = now or datetime.now().astimezone()
    start = now - timedelta(hours=settings.stream_lookback_hours)
    end = now + timedelta(hours=settings.stream_window_hours)
    events = catalog.events_for_resolution(start, end)
    LOGGER.info("resolving %d active/upcoming event(s) between %s and %s", len(events), start, end)
    resolved = 0
    streams_found = 0
    for position, event in enumerate(events, start=1):
        try:
            LOGGER.info("[%d/%d] resolving %s", position, len(events), event["title"])
            stream_ids = [content_id(reference) for reference in resolver(event)]
            catalog.set_streams(event["link"], stream_ids)
            resolved += 1
            streams_found += len(stream_ids)
            LOGGER.info("[%d/%d] saved %d stream(s)", position, len(events), len(stream_ids))
        except Exception:
            LOGGER.exception("failed to resolve event %s", event["link"])
    LOGGER.info("stream refresh complete: %d/%d events checked; %d stream(s) found", resolved, len(events), streams_found)
    return resolved


def cleanup_events(catalog: Catalog, settings: RefreshSettings, now: datetime | None = None) -> int:
    now = now or datetime.now().astimezone()
    cutoff = now - timedelta(hours=settings.retention_hours)
    removed = catalog.remove_before(cutoff)
    LOGGER.info("cleanup complete: %d expired event(s) removed", removed)
    return removed


def process_events(
    events: Iterable[Event], catalog: Catalog, resolver: Resolver, max_events: int | None = None,
    ignore_patterns: Sequence[str] = (),
) -> int:
    """Compatibility helper: discover supplied events then resolve all supplied events."""
    selected = list(events)
    if max_events is not None:
        selected = selected[:max_events]
    for event in selected:
        if not ignored_by(event, ignore_patterns):
            catalog.upsert_event(event)
    checked = 0
    for event in selected:
        if ignored_by(event, ignore_patterns):
            continue
        try:
            catalog.set_streams(event["link"], [content_id(item) for item in resolver(event)])
            checked += 1
        except Exception:
            LOGGER.exception("failed to resolve event %s", event["link"])
    return checked


def playlist(
    events: Iterable[Mapping[str, object]],
    stream_url_template: str = "acestream://{content_id}",
    base_url: str = "",
) -> str:
    lines = ["#EXTM3U"]
    for event in events:
        for stream_id in event["content_ids"]:
            lines.append(f'#EXTINF:-1 group-title="{_m3u(event["category"])}",{_m3u(event["title"])}')
            lines.append(stream_url_template.format(content_id=stream_id, base_url=base_url))
    return "\n".join(lines) + "\n"


def _m3u(value: object) -> str:
    return str(value).replace('"', "'").replace("\r", " ").replace("\n", " ")


def handler(
    catalog: Catalog,
    settings: RefreshSettings,
    playlist_url_template: str,
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            category = query.get("category", [""])[0]
            on_date = query.get("date", [""])[0]
            if on_date:
                try:
                    date.fromisoformat(on_date)
                except ValueError:
                    self._send(HTTPStatus.BAD_REQUEST, b"date must be YYYY-MM-DD\n", "text/plain")
                    return
            if parsed.path == "/playlist.m3u":
                self._send(
                    HTTPStatus.OK,
                    playlist(
                        catalog.events(category, on_date),
                        playlist_url_template,
                        self._public_base_url(),
                    ).encode(),
                    "audio/x-mpegurl",
                )
            elif parsed.path == "/api/events":
                self._send(HTTPStatus.OK, json.dumps(catalog.events(category, on_date)).encode(), "application/json")
            elif parsed.path == "/api/status":
                self._send(
                    HTTPStatus.OK,
                    json.dumps(
                        {
                            "refresh": catalog.statuses(),
                            "settings": asdict(settings),
                            "playlist_url_template": playlist_url_template,
                        }
                    ).encode(),
                    "application/json",
                )
            elif parsed.path == "/healthz":
                self._send(HTTPStatus.OK, b"ok\n", "text/plain")
            else:
                self._send(HTTPStatus.NOT_FOUND, b"not found\n", "text/plain")

        def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _public_base_url(self) -> str:
            host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host", "")
            scheme = (self.headers.get("X-Forwarded-Proto") or "http").split(",", 1)[0].strip()
            return f"{scheme}://{host}" if host else ""

        def log_message(self, message: str, *args: object) -> None:
            LOGGER.info("%s - %s", self.address_string(), message % args)

    return Handler


class Scheduler:
    def __init__(self, catalog: Catalog, feed_url: str, resolver: Resolver, settings: RefreshSettings,
                 ignore_patterns: Sequence[str], max_events: int | None = None):
        self.catalog = catalog
        self.feed_url = feed_url
        self.resolver = resolver
        self.settings = settings
        self.ignore_patterns = ignore_patterns
        self.max_events = max_events
        self.stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="catalog-scheduler", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def shutdown(self) -> None:
        self.stop.set()
        self._thread.join(timeout=30)

    def run_once(self) -> None:
        self._task("discovery", lambda: discover_events(self.feed_url, self.catalog, self.ignore_patterns, self.max_events))
        self._task("stream", lambda: resolve_active_events(self.catalog, self.resolver, self.settings))
        self._task("cleanup", lambda: cleanup_events(self.catalog, self.settings))

    def _task(self, name: str, operation: Callable[[], int]) -> None:
        self.catalog.mark_refresh(name, "running")
        try:
            count = operation()
        except Exception as error:
            self.catalog.mark_refresh(name, "error", str(error))
            LOGGER.exception("%s refresh failed", name)
        else:
            self.catalog.mark_refresh(name, "success", f"processed {count} event(s)")

    def _run(self) -> None:
        next_discovery = next_stream = next_cleanup = datetime.min.replace(tzinfo=datetime.now().astimezone().tzinfo)
        while not self.stop.is_set():
            now = datetime.now().astimezone()
            if now >= next_discovery:
                self._task("discovery", lambda: discover_events(self.feed_url, self.catalog, self.ignore_patterns, self.max_events))
                next_discovery = datetime.now().astimezone() + timedelta(
                    seconds=self.settings.discovery_interval_seconds
                )
            now = datetime.now().astimezone()
            if now >= next_stream:
                self._task("stream", lambda: resolve_active_events(self.catalog, self.resolver, self.settings))
                next_stream = datetime.now().astimezone() + timedelta(
                    seconds=self.settings.stream_interval_seconds
                )
            now = datetime.now().astimezone()
            if now >= next_cleanup:
                self._task("cleanup", lambda: cleanup_events(self.catalog, self.settings))
                next_cleanup = datetime.now().astimezone() + timedelta(
                    seconds=self.settings.discovery_interval_seconds
                )
            wait_until = min(next_discovery, next_stream, next_cleanup)
            self.stop.wait(max(0.1, (wait_until - datetime.now().astimezone()).total_seconds()))


def _positive_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser(description="RSS-backed AceStream event catalog")
    parser.add_argument("--once", action="store_true", help="run discovery, stream refresh, and cleanup once")
    parser.add_argument("--max-events", type=_positive_integer, help="limit RSS discovery for testing")
    parser.add_argument("--config", type=Path, help="YAML configuration file (overrides CONFIG_PATH)")
    parser.add_argument("--discovery-interval-seconds", type=_positive_integer)
    parser.add_argument("--stream-interval-seconds", type=_positive_integer)
    parser.add_argument("--stream-lookback-hours", type=_positive_integer)
    parser.add_argument("--stream-window-hours", type=_positive_integer)
    parser.add_argument("--retention-hours", type=_positive_integer)
    args = parser.parse_args()
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s:%(name)s:%(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    config_path = args.config or Path(os.getenv("CONFIG_PATH", DEFAULT_CONFIG_PATH))
    try:
        ignore_patterns = load_ignore_patterns(config_path)
        verify_event_tls = load_event_tls_verify(config_path)
        settings = load_refresh_settings(config_path)
        playlist_url_template = load_playlist_url_template(config_path)
    except ValueError as error:
        parser.error(str(error))
    overrides = {
        name: getattr(args, name)
        for name in asdict(settings)
        if getattr(args, name) is not None
    }
    settings = replace(settings, **overrides)
    LOGGER.info("loaded %d ignore pattern(s) from %s", len(ignore_patterns), config_path)
    LOGGER.info("refresh settings: %s", asdict(settings))
    if not verify_event_tls:
        LOGGER.warning("event-page TLS certificate verification is DISABLED by configuration")
    max_events = args.max_events
    if max_events is None and os.getenv("MAX_EVENTS"):
        max_events = _positive_integer(os.environ["MAX_EVENTS"])
    catalog = Catalog(os.getenv("DATABASE_PATH", "/data/catalog.db"))
    resolver = partial(resolve_streams, verify_tls=verify_event_tls)
    scheduler = Scheduler(catalog, os.getenv("FEED_URL", DEFAULT_FEED_URL), resolver, settings, ignore_patterns, max_events)
    if args.once:
        scheduler.run_once()
        return
    server = ThreadingHTTPServer(
        (os.getenv("HOST", "0.0.0.0"), int(os.getenv("PORT", "8090"))),
        handler(catalog, settings, playlist_url_template),
    )
    # Bind the public endpoint before the first potentially long stream refresh.
    # Docker can otherwise publish the port while the application has not yet
    # opened its listener, causing initial connections to be reset.
    scheduler.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        scheduler.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
