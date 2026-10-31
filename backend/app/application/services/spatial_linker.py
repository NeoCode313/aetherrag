"""Spatial correlation between text and visual context.

Two deterministic signals (zero LLM cost at ingestion time):

1. **Explicit reference** — the text mentions "Figura 3" / "Fig. 3" / "Diagrama 3" and a
   visual on the document carries that figure number in its caption. Strongest signal.
2. **Geometric proximity** — minimum Euclidean gap between the chunk's bounding box and
   the visual's bounding box on the same page, in normalized coordinates. Authors place
   figures next to the paragraph that explains them, so proximity is a good prior.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from app.domain.entities import (
    Chunk,
    ChunkKind,
    ElementKind,
    LinkedVisual,
    LinkReason,
    ParsedPage,
    VisualAsset,
)

FIGURE_LABEL = r"(?:fig(?:ura|ure)?s?\.?|diagrama|esquema|imagen|ilustraci[oó]n|figure|diagram)"
# Keep the complete identifier: chapter-qualified and appendix figures are common
# in manuals. A trailing full stop is punctuation, rather than part of the id.
FIGURE_ID = r"(?:[A-Za-z]|\d+[A-Za-z]?)(?:[.\-–]\d+[A-Za-z]?)*"
ID_END = r"(?!\w|[.\-–]\w)"
CAPTION_RE = re.compile(rf"^\s*{FIGURE_LABEL}\s*({FIGURE_ID}){ID_END}", re.IGNORECASE)
REFERENCE_RE = re.compile(rf"\b{FIGURE_LABEL}\s*({FIGURE_ID}){ID_END}", re.IGNORECASE)
TABLE_CAPTION_RE = re.compile(rf"^\s*(?:tabla|table)\s*({FIGURE_ID}){ID_END}", re.IGNORECASE)


def _canonical_label(value: str) -> str:
    parts = re.split(r"([.-])", value.replace("–", "-").upper())
    return "".join(str(int(part)) if part.isdigit() else part for part in parts)


def figure_label_from_caption(text: str) -> str | None:
    match = CAPTION_RE.match(text)
    return _canonical_label(match.group(1)) if match else None


def figure_number_from_caption(text: str) -> int | str | None:
    """Compatibility helper: integer ids stay integers; qualified ids stay intact."""
    label = figure_label_from_caption(text)
    return int(label) if label and label.isdigit() else label


def referenced_figure_numbers(text: str) -> set[int | str]:
    return {int(n) if n.isdigit() else _canonical_label(n) for n in REFERENCE_RE.findall(text)}


class SpatialLinker:
    """Assigns captions to visuals and links every chunk to its nearest visual context."""

    def __init__(self, max_distance: float = 0.35, caption_max_distance: float = 0.08, max_links: int = 3) -> None:
        self.max_distance = max_distance
        self.caption_max_distance = caption_max_distance
        self.max_links = max_links

    # ------------------------------------------------------------------ captions
    def assign_captions(self, page: ParsedPage) -> None:
        """Attach the closest figure caption on the page to each visual (prefer below)."""
        captions = [
            e
            for e in page.elements
            if e.kind == ElementKind.CAPTION and figure_number_from_caption(e.text) is not None
        ]
        proposals: list[tuple[float, VisualAsset, int]] = []
        for visual in page.visuals:
            candidates: list[tuple[float, int]] = []
            for idx, caption in enumerate(captions):
                horizontal_overlap = max(0.0, min(visual.bbox.x1, caption.bbox.x1) - max(visual.bbox.x0, caption.bbox.x0))
                if horizontal_overlap < min(visual.bbox.width, caption.bbox.width) * 0.15:
                    continue
                gap = visual.bbox.distance_to(caption.bbox)
                if gap > self.caption_max_distance:
                    continue
                # Captions conventionally sit *below* figures: small penalty otherwise.
                below = caption.bbox.y0 >= visual.bbox.y1 - 0.01
                score = gap + (0.0 if below else 0.02)
                candidates.append((score, idx))
            candidates.sort()
            if candidates and (len(candidates) == 1 or candidates[1][0] - candidates[0][0] > 0.005):
                proposals.append((candidates[0][0], visual, candidates[0][1]))
        used: set[int] = set()
        for score, visual, idx in sorted(proposals, key=lambda p: p[0]):
            if idx in used:
                continue
            # Avoid assigning a shared caption arbitrarily to equally plausible figures.
            if any(other is not visual and j == idx and abs(s - score) <= 0.005 for s, other, j in proposals):
                continue
            used.add(idx)
            visual.caption = captions[idx].text.strip()
            visual.figure_label = figure_label_from_caption(visual.caption)
            visual.figure_number = figure_number_from_caption(visual.caption)

    # ------------------------------------------------------------------ linking
    def link(self, chunks: Iterable[Chunk], visuals: Sequence[VisualAsset]) -> None:
        by_page: dict[int, list[VisualAsset]] = {}
        by_number: dict[tuple[str, str], list[VisualAsset]] = {}
        for v in visuals:
            by_page.setdefault(v.page, []).append(v)
            label = v.figure_label or figure_label_from_caption(v.caption or "")
            if label is None and v.figure_number is not None:
                label = str(v.figure_number)
            if label is not None:
                by_number.setdefault((v.document_id, _canonical_label(label)), []).append(v)

        for chunk in chunks:
            if chunk.kind == ChunkKind.FIGURE:
                continue  # figure chunks are linked to themselves at creation time
            links: dict[str, LinkedVisual] = {}

            for number in referenced_figure_numbers(chunk.text):
                candidates = by_number.get((chunk.document_id, _canonical_label(str(number))), [])
                same_page = [v for v in candidates if v.page == chunk.page]
                resolved = same_page if same_page else candidates
                if len(resolved) == 1:
                    v = resolved[0]
                    distance = chunk.bbox.distance_to(v.bbox) if v.page == chunk.page else None
                    links[v.asset_id] = LinkedVisual(v, distance, LinkReason.EXPLICIT_REFERENCE)

            for v in by_page.get(chunk.page, []):
                distance = chunk.bbox.distance_to(v.bbox)
                if v.document_id == chunk.document_id and distance <= self.max_distance and v.asset_id not in links:
                    links[v.asset_id] = LinkedVisual(v, round(distance, 4), LinkReason.SPATIAL_PROXIMITY)

            chunk.visuals = sorted(
                links.values(),
                key=lambda lv: (lv.reason != LinkReason.EXPLICIT_REFERENCE, lv.distance if lv.distance is not None else float("inf")),
            )[: self.max_links]
