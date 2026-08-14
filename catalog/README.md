# Event catalog prototype

This standalone service reads the RSS feed, stores eligible event metadata in
SQLite, refreshes playable links for active/upcoming events, and serves JSON or
an extended M3U playlist. Events without a stream remain in the catalog and
are retried in later stream refreshes.

Implement `resolve_streams` in `resolver.py`. It receives a mapping containing
`title`, `link`, `category`, and timezone-aware `starts_at`, and must return a
list containing 40-character content IDs, `acestream://` links, or HTTP URLs
with an `id` query parameter. Invalid references are rejected.

The project uses [uv](https://docs.astral.sh/uv/) for its Python environment.
Initialize or update the environment from the repository root with:

```bash
uv sync
```

Run the service with:

```bash
DATABASE_PATH=/tmp/acestream-catalog.db uv run python -m catalog.app
```

Or run one refresh without starting the server:

```bash
DATABASE_PATH=/tmp/acestream-catalog.db uv run python -m catalog.app --once
```

For a small end-to-end test, process only the first five RSS events:

```bash
DATABASE_PATH=/tmp/acestream-catalog.db \
uv run python -m catalog.app --once --max-events 5
```

The HTTP endpoints are:

- `GET /playlist.m3u`
- `GET /api/events`
- `GET /api/status`
- `GET /healthz`

The playlist and JSON endpoints accept exact, case-insensitive `category` and
ISO `date` filters, for example:

```text
/playlist.m3u?category=Athletics&date=2026-08-14
```

Playlist entries use the configured AceStream HTTP endpoint rather than direct
`acestream://` URIs, allowing VLC and similar clients to use this Pi's running
engine. In the Compose deployment, nginx supplies `{base_url}` so the playlist
uses its existing public origin and `/ace/` proxy:

```yaml
playlist:
  stream_url_template: "{base_url}/ace/getstream?id={content_id}"
```

For a standalone service exposed directly on port 8090, replace `{base_url}`
with the reachable player/engine URL you intend clients to use.

Configuration uses `FEED_URL`, `DATABASE_PATH`, `HOST`, `PORT`, `LOG_LEVEL`,
and `MAX_EVENTS`. `--max-events` overrides the environment value and is for
small test discoveries only. At `INFO` level the service logs discovery,
active-event resolution, cleanup, and final task status.

## Refreshes

The background scheduler starts all jobs immediately, then uses the intervals
in `config.yaml`:

```yaml
refresh:
  discovery_interval_seconds: 3600
  stream_interval_seconds: 300
  stream_lookback_hours: 2
  stream_window_hours: 24
  retention_hours: 12
```

Discovery downloads the RSS feed and stores only non-ignored events; it does
not request their detail pages. Stream refreshes request detail pages only for
events from the lookback window through the upcoming window. Cleanup removes
events older than the retention window. The active values and last job results
are available from `/api/status`.

Every YAML setting can be overridden for an invocation, for example:

```bash
uv run python -m catalog.app \
  --stream-interval-seconds 120 \
  --stream-window-hours 12
```

## Ignoring events

[`config.yaml`](./config.yaml) controls event filtering. Terms in `ignore` are
case-insensitive substrings matched against the event title, category, and full
RSS description. Matching events are logged and skipped before their detail
page is fetched:

```yaml
ignore:
  - baseball
  - football
```

Use another configuration file with `--config /path/to/config.yaml` or
`CONFIG_PATH=/path/to/config.yaml`.

### Event-page TLS verification

TLS verification for event-detail requests is enabled by default. If a source
has an invalid certificate chain, it can be disabled only for those requests:

```yaml
event_requests:
  verify_tls: false
```

This accepts any certificate for event pages and therefore exposes those
requests to interception. Keep it enabled for trusted sources. RSS fetching
continues to verify TLS certificates.

Build the standalone image from the repository root:

```bash
docker build -f catalog/Dockerfile -t acestream-event-catalog .
docker run --rm -p 8090:8090 -v acestream-catalog:/data acestream-event-catalog
```

## Resolver tests

Keep network fetching in `resolve_streams` and implement extraction as the pure
`parse_event_page(html, base_url)` function. Its tests use saved HTML in
`fixtures/`, so they do not contact an external site.

Run the parser and component tests with:

```bash
uv run python -m unittest discover -v
```

Replace or add sanitized HTML fixtures as page layouts evolve. The expected
result should remain a list of AceStream references; the processing pipeline
performs final content-ID validation.
