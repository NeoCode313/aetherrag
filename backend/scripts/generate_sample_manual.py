"""CLI script to generate the sample technical PDF manual for testing and demos."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.infrastructure.sample_manual import build_sample_manual_pdf


def main() -> None:
    target = Path(__file__).resolve().parents[1] / "data" / "sample_manuals" / "Manual_Turbina_HX900.pdf"
    out = build_sample_manual_pdf(target)
    print(f"Generated sample technical manual at: {out} ({out.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
