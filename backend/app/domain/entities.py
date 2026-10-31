"""Core entities and value objects of the multimodal RAG domain.

Everything here is framework-agnostic (stdlib dataclasses only) so the business rules
can be unit-tested in milliseconds without network, GPU or databases.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------------------
# Geometry
# --------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class BoundingBox:
    """Axis-aligned rectangle in *normalized* page coordinates (0..1, origin top-left).

    Normalizing by page width/height makes distances comparable across page sizes
    (A4, Letter, A3 drawings) and lets the UI overlay boxes at any zoom level.
    """

    x0: float
    y0: float
    x1: float
    y1: float

    @classmethod
    def from_absolute(
        cls, x0: float, y0: float, x1: float, y1: float, page_width: float, page_height: float
    ) -> BoundingBox:
        if page_width <= 0 or page_height <= 0:
            raise ValueError("Page dimensions must be positive")

        def clamp(v: float) -> float:
            return max(0.0, min(1.0, v))

        return cls(
            clamp(x0 / page_width),
            clamp(y0 / page_height),
            clamp(x1 / page_width),
            clamp(y1 / page_height),
        )

    @property
    def width(self) -> float:
        return max(0.0, self.x1 - self.x0)

    @property
    def height(self) -> float:
        return max(0.0, self.y1 - self.y0)

    @property
    def area(self) -> float:
        return self.width * self.height

    @property
    def center(self) -> tuple[float, float]:
        return ((self.x0 + self.x1) / 2, (self.y0 + self.y1) / 2)

    def union(self, other: BoundingBox) -> BoundingBox:
        return BoundingBox(
            min(self.x0, other.x0),
            min(self.y0, other.y0),
            max(self.x1, other.x1),
            max(self.y1, other.y1),
        )

    def intersection_area(self, other: BoundingBox) -> float:
        w = min(self.x1, other.x1) - max(self.x0, other.x0)
        h = min(self.y1, other.y1) - max(self.y0, other.y0)
        return w * h if w > 0 and h > 0 else 0.0

    def overlap_ratio(self, other: BoundingBox) -> float:
        """Fraction of *this* box covered by ``other`` (0..1)."""
        return self.intersection_area(other) / self.area if self.area > 0 else 0.0

    def distance_to(self, other: BoundingBox) -> float:
        """Minimum Euclidean gap between two rectangles (0 when they touch or overlap)."""
        dx = max(0.0, other.x0 - self.x1, self.x0 - other.x1)
        dy = max(0.0, other.y0 - self.y1, self.y0 - other.y1)
        return math.hypot(dx, dy)

    def to_list(self) -> list[float]:
        return [round(self.x0, 5), round(self.y0, 5), round(self.x1, 5), round(self.y1, 5)]

    @classmethod
    def from_list(cls, values: list[float] | tuple[float, ...]) -> BoundingBox:
        x0, y0, x1, y1 = values
        return cls(float(x0), float(y0), float(x1), float(y1))


# --------------------------------------------------------------------------------------
# Parsed document (output of the PDF parser port)
# --------------------------------------------------------------------------------------
class ElementKind(str, Enum):
    PARAGRAPH = "paragraph"
    HEADING = "heading"
    TABLE = "table"
    CAPTION = "caption"


class VisualKind(str, Enum):
    RASTER = "raster"  # embedded bitmap (photo, scanned diagram)
    VECTOR = "vector"  # drawn with PDF vector operators (schematics, flowcharts)


@dataclass(slots=True)
class LayoutElement:
    """A textual layout block extracted from a page."""

    page: int
    kind: ElementKind
    text: str
    bbox: BoundingBox
    font_size: float = 0.0
    heading_level: int = 0


@dataclass(slots=True)
class VisualAsset:
    """An image or diagram cropped from a page, with its spatial footprint."""

    asset_id: str
    document_id: str
    page: int
    bbox: BoundingBox
    kind: VisualKind
    file_name: str
    caption: str | None = None
    figure_number: int | str | None = None
    inner_text: str = ""  # labels drawn inside a vector diagram
    description: str | None = None  # optional vision-LLM description (searchable)
    width_px: int = 0
    height_px: int = 0
    figure_label: str | None = None


@dataclass(slots=True)
class ParsedPage:
    number: int  # 1-based, as humans cite pages
    width: float
    height: float
    elements: list[LayoutElement] = field(default_factory=list)
    visuals: list[VisualAsset] = field(default_factory=list)


@dataclass(slots=True)
class ParsedDocument:
    document_id: str
    file_name: str
    pages: list[ParsedPage]

    @property
    def visuals(self) -> list[VisualAsset]:
        return [v for p in self.pages for v in p.visuals]


# --------------------------------------------------------------------------------------
# Index units
# --------------------------------------------------------------------------------------
class ChunkKind(str, Enum):
    TEXT = "text"
    TABLE = "table"
    FIGURE = "figure"


class LinkReason(str, Enum):
    SELF = "self"  # the chunk *is* the figure description
    EXPLICIT_REFERENCE = "explicit_reference"  # "ver Figura 3"
    SPATIAL_PROXIMITY = "spatial_proximity"  # closest box on the same page


@dataclass(slots=True)
class LinkedVisual:
    asset: VisualAsset
    distance: float | None
    reason: LinkReason


@dataclass(slots=True)
class Chunk:
    chunk_id: str
    document_id: str
    document_name: str
    page: int
    kind: ChunkKind
    text: str
    bbox: BoundingBox
    section_path: tuple[str, ...] = ()
    visuals: list[LinkedVisual] = field(default_factory=list)
    storage_id: str | None = None
    embedding_prefix: str | None = None

    @property
    def section_title(self) -> str:
        return " › ".join(self.section_path)

    @property
    def embedding_text(self) -> str:
        """Text that is embedded/indexed: the section breadcrumb restores the context
        lost by splitting (reduces the conditional entropy of the isolated fragment)."""
        header = f"[{self.document_name} › {self.section_title}]" if self.section_path else f"[{self.document_name}]"
        return f"{self.embedding_prefix if self.embedding_prefix is not None else header}\n{self.text}"

    @property
    def nearest_visual(self) -> LinkedVisual | None:
        return min(self.visuals, key=lambda v: v.distance if v.distance is not None else math.inf) if self.visuals else None


@dataclass(frozen=True, slots=True)
class SparseVector:
    indices: list[int]
    values: list[float]


# --------------------------------------------------------------------------------------
# Documents & jobs
# --------------------------------------------------------------------------------------
class JobStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(slots=True)
class Document:
    document_id: str
    file_name: str
    sha256: str
    size_bytes: int
    status: JobStatus = JobStatus.PENDING
    page_count: int = 0
    chunk_count: int = 0
    visual_count: int = 0
    table_count: int = 0
    job_id: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    indexed_at: datetime | None = None


@dataclass(slots=True)
class IngestionJob:
    job_id: str
    document_id: str
    file_name: str
    status: JobStatus = JobStatus.PENDING
    progress: float = 0.0  # 0..100
    stage: str = "queued"
    message: str = "En cola"
    error: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def advance(self, progress: float, stage: str, message: str) -> None:
        self.status = JobStatus.PROCESSING
        self.progress = round(max(self.progress, min(progress, 99.0)), 1)
        self.stage = stage
        self.message = message
        self.updated_at = utcnow()
        self.started_at = self.started_at or self.updated_at

    def complete(self, message: str) -> None:
        self.status = JobStatus.COMPLETED
        self.progress = 100.0
        self.stage = "completed"
        self.message = message
        self.updated_at = self.finished_at = utcnow()

    def fail(self, error: str) -> None:
        self.status = JobStatus.FAILED
        self.stage = "failed"
        self.message = "La ingesta falló"
        self.error = error
        self.updated_at = self.finished_at = utcnow()


# --------------------------------------------------------------------------------------
# Retrieval & generation
# --------------------------------------------------------------------------------------
@dataclass(slots=True)
class ScoredChunk:
    chunk: Chunk
    score: float  # fused RRF score
    dense_score: float | None = None
    sparse_score: float | None = None
    dense_rank: int | None = None
    sparse_rank: int | None = None


@dataclass(frozen=True, slots=True)
class ConversationTurn:
    role: str  # "user" | "assistant"
    content: str


@dataclass(frozen=True, slots=True)
class ImageInput:
    """An image handed to a multimodal LLM together with its prompt alias (e.g. 'F1')."""

    alias: str
    data: bytes
    mime_type: str = "image/png"


@dataclass(frozen=True, slots=True)
class EvidenceReference:
    evidence_id: str
    quote: str | None = None
    span_start: int | None = None
    span_end: int | None = None
    visual_region: tuple[float, float, float, float] | None = None


@dataclass(slots=True)
class GeneratedClaim:
    statement: str
    evidence_refs: list[EvidenceReference] = field(default_factory=list)


@dataclass(slots=True)
class GeneratedAnswer:
    """Raw structured output returned by the LLM port (not yet validated)."""

    answer: str
    abstained: bool
    cited_ids: list[str] = field(default_factory=list)
    used_visual_ids: list[str] = field(default_factory=list)
    evidence_coverage: str = "none"  # full | partial | none
    abstention_reason: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    answer_mode: str = "grounded_answer"
    claims: list[GeneratedClaim] = field(default_factory=list)
    missing_facets: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Citation:
    ref: int  # [1], [2]... as shown to the user
    chunk_id: str
    document_id: str
    document_name: str
    page: int
    section: str
    kind: ChunkKind
    snippet: str
    bbox: BoundingBox
    score: float
    dense_score: float | None = None
    sparse_score: float | None = None
    storage_id: str | None = None
    modality: str = "text"


@dataclass(slots=True)
class VisualEvidence:
    asset: VisualAsset
    document_name: str
    distance: float | None
    reason: LinkReason
    anchor_chunk_id: str
    anchor_bbox: BoundingBox


@dataclass(slots=True)
class RAGMetrics:
    """Execution telemetry. Semantic quality scores stay null until independently evaluated."""

    max_cosine_similarity: float = 0.0
    mean_cosine_similarity: float = 0.0
    max_bm25_score: float = 0.0  # deprecated alias of lexical_score, not conventional BM25
    top_rrf_score: float = 0.0
    faithfulness_score: float | None = None  # deprecated: no online truth estimate
    citation_precision: float | None = None  # deprecated: ID validity is not citation support
    answer_relevance: float | None = None
    context_utilization: float = 0.0
    spatial_alignment_score: float = 0.0
    lexical_score: float = 0.0
    reference_validity: float | None = None
    lexical_overlap_score: float | None = None
    visual_proximity_score: float | None = None
    images_sent: int = 0


@dataclass(slots=True)
class RAGAnswer:
    answer: str
    abstained: bool
    abstention_reason: str | None
    evidence_coverage: str
    citations: list[Citation]
    primary_visual: VisualEvidence | None
    related_visuals: list[VisualEvidence]
    retrieved: int
    model: str
    latency_ms: int
    timings_ms: dict[str, int] = field(default_factory=dict)
    metrics: RAGMetrics = field(default_factory=RAGMetrics)
    answer_mode: str = "grounded_answer"
    message_id: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)
