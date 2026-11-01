"""Caching page text must preserve clipped cell text, decimals and rotation."""
import pymupdf
import pytest

from app.infrastructure.pdf.pymupdf_parser import PyMuPDFDocumentParser
from app.infrastructure.sample_manual import _draw_table


@pytest.mark.parametrize("rotation", [0,90,180,270])
@pytest.mark.parametrize("crosses_cell", [False,True])
def test_cached_cells_equal_the_previous_native_extraction(rotation, crosses_cell):
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=600, height=800)
        _draw_table(page, 50, 80, [220, 220], [
            ["Parámetro", "Valor"], ["Potencia", "145.0 MW"],
            ["Holgura nominal", "0.18 mm"], ["Umbral ST-104", "85.0 C"],
        ], row_height=30)
        if crosses_cell:
            page.insert_text((260, 170), "900.01", fontsize=12)
        page.set_cropbox(pymupdf.Rect(20, 20, 580, 780))
        page.set_rotation(rotation)
        previous = page.rotation
        page.set_rotation(0)
        tables = page.find_tables().tables
        expected = [([[page.get_text("text", clip=pymupdf.Rect(cell), sort=True).strip()
            if cell else "" for cell in row.cells] for row in table.rows], tuple(table.bbox))
            for table in tables]
        actual = PyMuPDFDocumentParser._extract_table_cells(page)
        page.set_rotation(previous)
        assert expected and actual == expected
