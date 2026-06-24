from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

from . import config, detect, render, state
from .models import Event
from .sources import close_approaches, sentry

_BASE_LABEL = "planetary-defense"


def labels_for(event: Event) -> list[str]:
    labels = [_BASE_LABEL, "sentry", f"severity-{event.severity}"]
    # Label objects currently at Torino >= 1, read from the actual Torino value the
    # event carries (TORINO_UP/DOWN expose `ts_now`; SENTRY_NEW exposes `ts_max`).
    current_torino = max(event.payload.get("ts_now", 0), event.payload.get("ts_max", 0))
    if current_torino >= 1:
        labels.append("torino-ge-1")
    return labels


def labels_cad(event: Event) -> list[str]:
    labels = [_BASE_LABEL, "close-approach", f"severity-{event.severity}"]
    if event.type == "CAD_SUBLUNAR":
        labels.append("sub-lunar")
    return labels


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _save_meta(state_dir: Path) -> None:
    state.save(state_dir / "meta.json",
               {"schema_version": config.SCHEMA_VERSION, "last_run_utc": _now(),
                "cold_start": False})


def _sources(fetch_sentry, fetch_cad):
    # (state filename, fetch, detect, snapshot, labels)
    return [
        ("sentry.json", fetch_sentry, detect.detect_sentry, detect.sentry_snapshot, labels_for),
        ("close_approaches.json", fetch_cad, detect.detect_cad, detect.cad_snapshot, labels_cad),
    ]


def _seed(state_dir: Path, sources) -> None:
    """Cold start: write every source's current snapshot, emit nothing."""
    for filename, fetch_fn, _detect, snapshot_fn, _labels in sources:
        state.save(state_dir / filename, snapshot_fn(fetch_fn()))
    _save_meta(state_dir)


def _process_source(state_dir: Path, source, sink, dry_run: bool) -> tuple[list[Event], bool]:
    # pylint: disable=too-many-locals
    """Run one source end to end. Returns (events, ok); ok=False means its state
    must NOT advance (so it re-detects next run)."""
    filename, fetch_fn, detect_fn, snapshot_fn, labels_fn = source
    try:
        current = fetch_fn()
    except Exception as exc:  # pylint: disable=broad-exception-caught
        print(f"source {filename}: fetch failed: {exc}", file=sys.stderr)
        return [], False

    previous = state.load(state_dir / filename)
    events = detect_fn(previous, current)

    try:
        for event in events:
            title, body = render.render(event)
            if dry_run:
                print(f"[{event.severity}] {event.type}: {title}")
            else:
                sink.upsert(event.key, title, body, labels_fn(event))
    except Exception as exc:  # pylint: disable=broad-exception-caught
        print(f"sink {filename}: emit failed: {exc}", file=sys.stderr)
        return [], False

    if not dry_run:
        state.save(state_dir / filename, snapshot_fn(current))
    return events, True


def run(*, state_dir: Path, sink, dry_run: bool = False,
        fetch_sentry=sentry.fetch, fetch_cad=close_approaches.fetch) -> list[Event]:
    meta = state.load(state_dir / "meta.json")
    cold = not meta or meta.get("cold_start", True)
    sources = _sources(fetch_sentry, fetch_cad)

    if cold:
        if not dry_run:
            _seed(state_dir, sources)
        return []

    all_events: list[Event] = []
    failures: list[str] = []
    for source in sources:
        events, ok = _process_source(state_dir, source, sink, dry_run)
        all_events.extend(events)
        if not ok:
            failures.append(source[0])

    if not dry_run:
        _save_meta(state_dir)
    if failures:
        raise RuntimeError(f"source(s) failed this run: {', '.join(failures)}")
    return all_events
