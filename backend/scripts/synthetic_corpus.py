"""Additional constructed fixtures: separate families, never labelled real manuals."""
from pathlib import Path
import pymupdf
from app.infrastructure.sample_manual import _draw_table


def build_synthetic_corpus(folder: Path, requested: set[str]) -> dict[str,Path]:
    result = {}
    if "pump_procedure" in requested:
        path = folder / "pump_procedure.pdf"
        with pymupdf.open() as pdf:
            page = pdf.new_page()
            page.insert_text((50,65),"Pump PX-17 - maintenance procedure",fontsize=15)
            for y,text in zip((110,160,210,260),[
                "1. Inspect the seal of pump PX-17 before starting.",
                "2. Keep pressure of pump PX-17 below 14.5 bar.",
                "3. Minimum ambient temperature for PX-17 is -10 C.",
                "4. Tightening torque for bolt M24 is 640 Nm.",
            ]):
                page.insert_text((50,y),text,fontsize=10)
            pdf.save(path)
        result["pump_procedure"] = path
    if "controller_table" in requested:
        path = folder / "controller_table.pdf"
        with pymupdf.open() as pdf:
            page = pdf.new_page()
            page.insert_text((50,65),"Controller CT-22 - protection setpoints",fontsize=15)
            _draw_table(page,50,110,[105,140,235],[
                ["Sensor","Threshold","Action"],
                ["TEMP-11","70 C","Start auxiliary fan AUX-20"],
                ["TRIP-12","90 C","Stop motor MOTOR-31"],
                ["PRESS-13","4.2 bar","Block start of pump PUMP-14"],
            ],row_height=32)
            pdf.save(path)
        result["controller_table"] = path
    if "visual_flow" in requested:
        path = folder / "visual_flow.pdf"
        with pymupdf.open() as pdf:
            page = pdf.new_page()
            page.insert_text((50,65),"Flow diagram",fontsize=15)
            page.draw_rect((70,140,490,340),color=(.4,.4,.4))
            page.draw_rect((100,210,185,265),color=(0,0,0))
            page.draw_rect((360,210,445,265),color=(0,0,0))
            page.insert_text((130,240),"A",fontsize=15)
            page.insert_text((395,240),"B",fontsize=15)
            page.draw_line((190,235),(350,235),color=(0,0,1),width=4)
            page.draw_line((350,235),(330,225),color=(0,0,1),width=4)
            page.draw_line((350,235),(330,245),color=(0,0,1),width=4)
            page.insert_text((70,365),"Figura 02: flow between A and B.",fontsize=10)
            pdf.save(path)
        result["visual_flow"] = path
    return result
