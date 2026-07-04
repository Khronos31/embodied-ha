"""Helpers for resolving media entries from preferences."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from state_utils import clean


def load_media_items(prefs: dict[str, Any], bucket: str) -> list[dict[str, Any]]:
    items = prefs.get(bucket)
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _item_keys(item: dict[str, Any]) -> tuple[str, ...]:
    return (
        clean(item.get("id")),
        clean(item.get("source")),
        clean(item.get("label")),
    )


def _find_matching_item(items: Iterable[dict[str, Any]], needle: str) -> dict[str, Any] | None:
    target = clean(needle)
    if not target:
        return None
    for item in items:
        if target in _item_keys(item):
            return item
    return None


def resolve_media_item(
    prefs: dict[str, Any],
    source: str | None,
    *,
    buckets: tuple[str, ...],
    allow_single: bool = False,
) -> tuple[dict[str, Any] | None, str, str]:
    """Resolve a media item by ``id`` / ``source`` / ``label``.

    Returns ``(item, resolved_source, matched_bucket)``. ``resolved_source`` is
    chased through nested media references when the selected item's ``source``
    points at another media entry.
    """

    source_text = clean(source)
    bucket_items: list[tuple[str, dict[str, Any]]] = []
    for bucket in buckets:
        for item in load_media_items(prefs, bucket):
            bucket_items.append((bucket, item))

    if not source_text:
        if allow_single and len(bucket_items) == 1:
            bucket, item = bucket_items[0]
            return item, _resolve_target_source(prefs, item, buckets=buckets), bucket
        return None, "", ""

    item = None
    matched_bucket = ""
    for bucket, candidate in bucket_items:
        if source_text in _item_keys(candidate):
            item = candidate
            matched_bucket = bucket
            break

    if not item:
        return None, "", ""

    return item, _resolve_target_source(prefs, item, buckets=buckets), matched_bucket


def _resolve_target_source(
    prefs: dict[str, Any],
    item: dict[str, Any],
    *,
    buckets: tuple[str, ...],
    seen: set[str] | None = None,
) -> str:
    seen = seen or set()
    candidate = clean(item.get("source"))
    if not candidate or candidate in seen:
        return candidate
    seen.add(candidate)

    for bucket in buckets:
        target = _find_matching_item(load_media_items(prefs, bucket), candidate)
        if target and target is not item:
            next_source = clean(target.get("source"))
            if next_source and next_source != candidate:
                return _resolve_target_source(prefs, target, buckets=buckets, seen=seen)
            return next_source or candidate
    return candidate
