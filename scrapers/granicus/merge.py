"""Merge HTML agenda items with JSON.php video markers.

For items that appear in both sources (matched by title similarity),
the HTML item is kept and enriched with the marker's video timestamp.
Unmatched markers are appended with ``source="video_markers"``.
"""

import re


def merge_agenda_sources(
    html_items: list[dict],
    marker_items: list[dict],
    clip_id: int,
) -> list[dict]:
    """Merge HTML agenda items with JSON video markers."""
    if not html_items:
        return marker_items
    if not marker_items:
        return html_items

    available: dict[int, dict] = {i: m for i, m in enumerate(marker_items)}
    normalised_markers: list[tuple[int, str]] = [
        (i, _normalise_for_match(m["title"]))
        for i, m in enumerate(marker_items)
    ]

    matched_marker_ids: set[int] = set()

    for item in html_items:
        norm_title = _normalise_for_match(item["title"])
        best_idx: int | None = None
        best_score: float = 0.0

        for midx, mnorm in normalised_markers:
            if midx in matched_marker_ids:
                continue
            score = _title_similarity(norm_title, mnorm)
            if score > best_score:
                best_score = score
                best_idx = midx

        if best_idx is not None and best_score >= 0.6:
            matched_marker_ids.add(best_idx)
            marker = available[best_idx]
            if marker.get("media_ref") and not item.get("media_ref"):
                item["media_ref"] = marker["media_ref"]

    for midx, marker in available.items():
        if midx not in matched_marker_ids:
            html_items.append(marker)

    return html_items


def _normalise_for_match(title: str) -> str:
    """Normalise a title for fuzzy matching between sources."""
    t = title.lower().strip()
    t = re.sub(r"^(?:\d+[a-z]?[\.\)]\s*|[a-z][\.\)]\s*|[ivxlc]+[\.\)]\s*)", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _title_similarity(a: str, b: str) -> float:
    """Compute similarity between two normalised titles."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.9

    words_a = set(a.split())
    words_b = set(b.split())
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union)
