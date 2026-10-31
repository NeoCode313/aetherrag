"""Layout chunks bounded by complete embedding input, including its breadcrumb.

The default counter charges one token per UTF-8 byte: a conservative budget,
not an exact model token count. A provider tokenizer can be supplied explicitly.
Full source metadata is retained even when the embedding prefix is shortened.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from app.application.services.spatial_linker import TABLE_CAPTION_RE
from app.domain.entities import (
    BoundingBox, Chunk, ChunkKind, ElementKind, LayoutElement, LinkedVisual,
    LinkReason, ParsedDocument,
)
from app.domain.exceptions import ValidationError


def estimate_tokens(text: str) -> int:
    """Conservative byte budget; not a measured model token count."""
    return len(text.encode("utf-8"))


class LayoutAwareChunker:
    def __init__(self, max_tokens: int = 380, overlap_ratio: float = 0.15,
                 token_counter: Callable[[str], int] | None = None) -> None:
        if max_tokens < 50 or not 0 <= overlap_ratio < 0.5:
            raise ValueError("max_tokens must be >= 50 and overlap_ratio in [0, 0.5)")
        self.max_tokens = max_tokens
        self.overlap_tokens = int(max_tokens * overlap_ratio)
        self.count_tokens = token_counter or estimate_tokens

    def _take(self, text: str, budget: int) -> str:
        if budget <= 0:
            return ""
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.count_tokens(text[:mid]) <= budget:
                lo = mid
            else:
                hi = mid - 1
        return text[:lo]

    def _prefix(self, name: str, section: tuple[str, ...]) -> str:
        full = f"[{name} › {' › '.join(section)}]" if section else f"[{name}]"
        allowance = max(12, self.max_tokens // 2)
        if self.count_tokens(full) <= allowance:
            return full
        return self._take(full, allowance - self.count_tokens("…]")) + "…]"

    def _fits(self, text: str, prefix: str) -> bool:
        return self.count_tokens(f"{prefix}\n{text}") <= self.max_tokens

    def _split_long(self, text: str, prefix: str = "", *, overlap: bool = False) -> list[str]:
        """Natural boundaries with a guaranteed fallback for oversized words/cells."""
        remaining, parts = text.strip(), []
        while remaining:
            if self._fits(remaining, prefix):
                parts.append(remaining)
                break
            lo, hi = 0, len(remaining)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if self._fits(remaining[:mid], prefix):
                    lo = mid
                else:
                    hi = mid - 1
            if lo == 0:
                raise ValidationError("El presupuesto de chunk no permite un carácter junto a su contexto.")
            boundaries = [m.end() for m in re.finditer(r"\s+", remaining[:lo])]
            cut = next((p for p in reversed(boundaries) if p >= lo // 2), lo)
            piece = remaining[:cut].strip()
            if not piece:
                piece, cut = remaining[:lo], lo
            parts.append(piece)
            if overlap and self.overlap_tokens and cut < len(remaining):
                tail = remaining[:cut]
                start = cut
                while start > 0 and self.count_tokens(tail[start - 1:]) <= self.overlap_tokens:
                    start -= 1
                # Always advance; prefer a word boundary inside the bounded tail.
                while start < cut and start > 0 and not remaining[start - 1].isspace():
                    start += 1
                remaining = remaining[max(1, start):].lstrip()
            else:
                remaining = remaining[cut:].lstrip()
        return parts

    def _table_parts(self, text: str, caption: str, prefix: str) -> list[str]:
        rows = [line for line in text.splitlines() if line.strip()]
        if len(rows) < 2:
            return self._split_long("\n\n".join(filter(None, [caption, text])), prefix)
        header = "\n".join(rows[:2])
        preamble = "\n\n".join(filter(None, [caption, header]))
        parts: list[str] = []
        oversized_header = not self._fits(preamble + "\n| x |", prefix)
        # Preserve full oversized labels separately, and tag each value with its
        # row and column when a repeated header cannot fit the hard budget.
        if oversized_header:
            parts.extend(self._split_long(preamble, prefix))
            preamble = ""
        pending: list[str] = []
        labels = [s.strip() for s in re.split(r"(?<!\\)\|", rows[0].strip().strip("|"))]
        for row_index, row in enumerate(rows[2:], start=1):
            combined = "\n".join(filter(None, [preamble, *pending, row]))
            if not oversized_header and self._fits(combined, prefix):
                pending.append(row)
                continue
            if pending:
                parts.append("\n".join(filter(None, [preamble, *pending])))
                pending = []
            alone = "\n".join(filter(None, [preamble, row]))
            if not oversized_header and self._fits(alone, prefix):
                pending.append(row)
                continue
            # Oversized row/cell fallback retains row/column identity.
            cells = [s.strip() for s in re.split(r"(?<!\\)\|", row.strip().strip("|"))]
            for col_index, cell in enumerate(cells, start=1):
                label = labels[col_index - 1] if col_index <= len(labels) else str(col_index)
                label = self._take(label, max(8, self.max_tokens // 8))
                row_key = self._take(cells[0], max(8, self.max_tokens // 8))
                marker = f"[Fila {row_index}, columna {col_index}: {label}; clave {row_key}]\n"
                for value in self._split_long(cell or "(vacío)", prefix + "\n" + marker):
                    parts.append(marker + value)
        if pending:
            parts.append("\n".join(filter(None, [preamble, *pending])))
        if not parts:
            parts.extend(self._split_long(preamble, prefix))
        return parts

    def chunk(self, document: ParsedDocument) -> list[Chunk]:
        chunks: list[Chunk] = []
        stack: list[tuple[int, str]] = []
        for page in document.pages:
            seq = 0
            buffer: list[LayoutElement] = []
            pending_caption: LayoutElement | None = None
            section_marks = [(-1.0, self._path(stack))]
            assigned = {v.caption.strip() for v in page.visuals if v.caption}

            def emit(kind: ChunkKind, text: str, bbox: BoundingBox, *,
                     section: tuple[str, ...] | None = None,
                     visuals: list[LinkedVisual] | None = None) -> None:
                nonlocal seq
                section = self._path(stack) if section is None else section
                prefix = self._prefix(document.file_name, section)
                for piece in self._split_long(text, prefix, overlap=kind == ChunkKind.TEXT):
                    seq += 1
                    chunk = Chunk(
                        chunk_id=f"{document.document_id}:p{page.number}:c{seq}",
                        document_id=document.document_id, document_name=document.file_name,
                        page=page.number, kind=kind, text=piece, bbox=bbox,
                        section_path=section, visuals=list(visuals or []), embedding_prefix=prefix,
                    )
                    if self.count_tokens(chunk.embedding_text) > self.max_tokens:
                        raise ValidationError("El chunk excede el presupuesto de embeddings configurado.")
                    chunks.append(chunk)

            def flush() -> None:
                if buffer:
                    emit(ChunkKind.TEXT, "\n\n".join(e.text for e in buffer), self._union(buffer))
                    buffer.clear()

            def flush_caption() -> None:
                nonlocal pending_caption
                if pending_caption is not None:
                    emit(ChunkKind.TEXT, pending_caption.text, pending_caption.bbox)
                    pending_caption = None

            for element in page.elements:
                if element.kind == ElementKind.HEADING:
                    flush()
                    flush_caption()
                    level = element.heading_level or 1
                    while stack and stack[-1][0] >= level:
                        stack.pop()
                    stack.append((level, element.text.strip()))
                    section_marks.append((element.bbox.y0, self._path(stack)))
                    emit(ChunkKind.TEXT, element.text, element.bbox)
                elif element.kind == ElementKind.CAPTION:
                    flush()
                    flush_caption()
                    if TABLE_CAPTION_RE.match(element.text):
                        pending_caption = element
                    elif element.text.strip() not in assigned:
                        emit(ChunkKind.TEXT, element.text, element.bbox)
                elif element.kind == ElementKind.TABLE:
                    flush()
                    caption = pending_caption.text if pending_caption else ""
                    box = element.bbox.union(pending_caption.bbox) if pending_caption else element.bbox
                    pending_caption = None
                    prefix = self._prefix(document.file_name, self._path(stack))
                    for part in self._table_parts(element.text, caption, prefix):
                        emit(ChunkKind.TABLE, part, box)
                else:
                    flush_caption()
                    prefix = self._prefix(document.file_name, self._path(stack))
                    if buffer and not self._fits("\n\n".join([*(e.text for e in buffer), element.text]), prefix):
                        flush()
                    buffer.append(element)
                    if not self._fits(element.text, prefix):
                        flush()
            flush()
            flush_caption()
            for visual in page.visuals:
                section = section_marks[0][1]
                for y, snapshot in section_marks:
                    if y <= visual.bbox.y0:
                        section = snapshot
                lines = [visual.caption or f"Elemento visual en la página {visual.page}"]
                if visual.description:
                    lines.append(f"Descripción del diagrama: {visual.description}")
                if visual.inner_text:
                    lines.append(f"Etiquetas del diagrama: {visual.inner_text}")
                emit(ChunkKind.FIGURE, "\n".join(lines), visual.bbox, section=section,
                     visuals=[LinkedVisual(visual, 0.0, LinkReason.SELF)])
        return chunks

    @staticmethod
    def _path(stack: list[tuple[int, str]]) -> tuple[str, ...]:
        return tuple(title for _, title in stack)

    @staticmethod
    def _union(elements: list[LayoutElement]) -> BoundingBox:
        box = elements[0].bbox
        for element in elements[1:]:
            box = box.union(element.bbox)
        return box
