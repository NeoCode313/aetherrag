"""Generator for the rich technical sample PDF manual (`Manual_Turbina_HX900.pdf`).

Creates a 3-page industrial engineering manual containing:
* Multi-level section headings & dense technical paragraphs
* 3 structured specification & fault-code tables (extractable by ``page.find_tables()``)
* 2 high-resolution CAD cutaway illustrations (``Figura 1`` and ``Figura 2``)
* 1 native PDF vector schematic diagram (``Figura 3``) with internal labels
"""

from __future__ import annotations

from pathlib import Path

import pymupdf


def _fit_textbox(page: pymupdf.Page, rect: pymupdf.Rect, text: str, *,
                 fontsize: float = 9.6, min_fontsize: float = 6.0, **kwargs) -> None:
    """Commit text only when it fits; a failed layout must never silently erase it."""
    text = text.replace("—", " - ").replace("–", "-")
    size = fontsize
    while size >= min_fontsize:
        shape = page.new_shape()
        if shape.insert_textbox(rect, text, fontsize=size, **kwargs) >= 0:
            shape.commit()
            return
        size = round(size - .2, 2)
    raise ValueError(f"Sample text does not fit at {min_fontsize} pt: {text[:60]}")


def _draw_table(
    page: pymupdf.Page,
    x0: float,
    y0: float,
    col_widths: list[float],
    rows: list[list[str]],
    row_height: float = 26.0,
) -> float:
    """Draw a bordered grid table on ``page`` so ``page.find_tables()`` extracts it cleanly."""
    for r_idx, row in enumerate(rows):
        top = y0 + r_idx * row_height
        bottom = top + row_height
        is_header = r_idx == 0
        fill_color = (0.11, 0.19, 0.31) if is_header else ((0.95, 0.97, 0.99) if r_idx % 2 == 1 else (1.0, 1.0, 1.0))
        text_color = (1.0, 1.0, 1.0) if is_header else (0.10, 0.14, 0.20)
        fontname = "helv"

        cx = x0
        for c_idx, cell_text in enumerate(row):
            cw = col_widths[c_idx]
            rect = pymupdf.Rect(cx, top, cx + cw, bottom)
            page.draw_rect(rect, color=(0.55, 0.62, 0.72), fill=fill_color, width=1.0)
            text_rect = pymupdf.Rect(cx + 4, top + 3, cx + cw - 3, bottom - 2)
            _fit_textbox(page,
                text_rect,
                cell_text,
                fontsize=7.8,
                fontname=fontname,
                color=text_color,
            )
            cx += cw
    return y0 + len(rows) * row_height


def build_sample_manual_pdf(output_path: Path, assets_dir: Path | None = None) -> Path:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if assets_dir is None:
        assets_dir = Path(__file__).resolve().parents[2] / "scripts" / "assets"

    doc = pymupdf.open()

    # =========================================================================
    # PAGE 1: Architecture, Nominal Specifications Table & Figure 1 (Cutaway)
    # =========================================================================
    p1 = doc.new_page(width=595, height=842)  # A4

    p1.insert_text(
        (45, 30),
        "MANUAL TÉCNICO DE INGENIERÍA Y MANTENIMIENTO · DOC-HX900-REV4",
        fontsize=7.5,
        color=(0.45, 0.50, 0.58),
    )
    p1.insert_text(
        (45, 58),
        "1. Arquitectura y Especificaciones de la Turbina Hidráulica HX-900",
        fontsize=14.5,
        color=(0.07, 0.14, 0.25),
    )

    _fit_textbox(p1,
        pymupdf.Rect(45, 72, 550, 148),
        (
            "La unidad generadora Serie HX-900 es una turbina hidráulica de reacción tipo Francis de eje "
            "vertical diseñada para centrales hidroeléctricas de salto medio. El flujo ingresa a través de "
            "la cámara espiral (1) de acero al carbono ASTM A516 Gr. 70, pasa por el predistribuidor fijo y "
            "los 24 álabes directrices móviles del distribuidor Fink (2) que regulan el caudal hacia el "
            "rodete Francis de bronce-aluminio (3). La energía mecánica se transmite mediante el eje "
            "principal forjado ASTM A668 (4) guiado por el cojinete radial (5), descargando el flujo hacia "
            "el tubo de aspiración cónico (6) con inyección de aire anti-vórtice. Ver Figura 1."
        ),
        fontsize=9.6,
        color=(0.12, 0.15, 0.20),
    )

    # Insert Figure 1 (Turbine Cutaway)
    fig1_path = assets_dir / "turbine_cutaway.jpg"
    fig1_rect = pymupdf.Rect(65, 152, 530, 405)
    if fig1_path.exists():
        p1.insert_image(fig1_rect, filename=str(fig1_path))
    else:
        p1.draw_rect(fig1_rect, color=(0.18, 0.35, 0.62), fill=(0.94, 0.97, 1.0), width=1.4)
        p1.insert_text((160, 275), "Diagrama Isometrico Turbina HX-900 (1..6)", fontsize=11)

    _fit_textbox(p1,
        pymupdf.Rect(65, 410, 530, 442),
        (
            "Figura 1: Corte isométrico del conjunto turbina Francis HX-900 — (1) Cámara espiral, "
            "(2) Álabes directrices, (3) Rodete Francis, (4) Eje principal, (5) Cojinete guía, (6) Tubo de aspiración."
        ),
        fontsize=8.6,
        color=(0.20, 0.28, 0.40),
    )

    p1.insert_text(
        (45, 462),
        "1.1 Parámetros Nominales de Diseño y Límites Operativos",
        fontsize=12.0,
        color=(0.07, 0.14, 0.25),
    )
    _fit_textbox(p1,
        pymupdf.Rect(45, 472, 550, 515),
        (
            "La operación continua fuera de la envolvente nominal mostrada en la Tabla 1 induce cavitación "
            "en el borde de salida del rodete (3) y fatiga prematura en el acoplamiento del eje principal (4). "
            "La sobrevelocidad máxima admisible ante rechazo total de carga no debe superar 465 RPM."
        ),
        fontsize=9.5,
        color=(0.12, 0.15, 0.20),
    )

    p1.insert_text(
        (45, 528),
        "Tabla 1: Especificaciones técnicas nominales de la turbina Francis Serie HX-900.",
        fontsize=8.8,
        color=(0.20, 0.28, 0.40),
    )
    _draw_table(
        p1,
        x0=45,
        y0=536,
        col_widths=[190, 125, 190],
        rows=[
            ["Parámetro de Diseño", "Valor Nominal", "Límite / Condición Operativa"],
            ["Potencia nominal en eje (P_nom)", "145.0 MW", "Máximo continuo 152.0 MW"],
            ["Salto hidráulico neto (H_n)", "182.0 m", "Rango admisible 165 m - 198 m"],
            ["Caudal nominal de diseño (Q_n)", "88.5 m³/s", "Mínimo técnico 38.0 m³/s (43%)"],
            ["Velocidad síncrona de giro", "300.0 RPM", "Sobrevelocidad disparo 465 RPM"],
            ["Presión máxima cámara espiral", "21.4 bar", "Prueba hidrostática 32.0 bar"],
            ["Eficiencia hidráulica pico", "94.6 %", "Garantizada a 88.5 m³/s y 182 m"],
        ],
    )

    p1.insert_text((45, 818), "Confidencial · Manual_Turbina_HX900.pdf · Página 1 de 3", fontsize=7.5, color=(0.5, 0.5, 0.5))

    # =========================================================================
    # PAGE 2: Hydrodynamic Bearing, RTD Sensor ST-104 & Figure 2
    # =========================================================================
    p2 = doc.new_page(width=595, height=842)
    p2.insert_text(
        (45, 30),
        "MANUAL TÉCNICO DE INGENIERÍA Y MANTENIMIENTO · DOC-HX900-REV4",
        fontsize=7.5,
        color=(0.45, 0.50, 0.58),
    )
    p2.insert_text(
        (45, 58),
        "2. Cojinete Guía Hidrodinámico y Monitoreo Térmico (ST-104)",
        fontsize=14.5,
        color=(0.07, 0.14, 0.25),
    )

    _fit_textbox(p2,
        pymupdf.Rect(45, 72, 550, 152),
        (
            "El cojinete guía radial de la turbina soporta los esfuerzos hidráulicos radiales del eje "
            "principal (1) mediante ocho zapatas basculantes autoalineables recubiertas en metal blanco "
            "Babbitt SnSb8Cu4 (2). El aceite sintético ISO VG 46 ingresa a presión por el colector anular "
            "y los canales de inyección (3) manteniendo una película hidrodinámica de 0.18 mm. La temperatura "
            "crítica del metal Babbitt se mide directamente mediante la sonda termorresistiva doble RTD Pt100 "
            "código ST-104 (4) insertada en la zona de carga de la zapata, protegida por sellos laberínticos "
            "de fluoroelastómero (5) tal como detalla la Figura 2."
        ),
        fontsize=9.6,
        color=(0.12, 0.15, 0.20),
    )

    fig2_path = assets_dir / "bearing_detail.jpg"
    fig2_rect = pymupdf.Rect(65, 156, 530, 412)
    if fig2_path.exists():
        p2.insert_image(fig2_rect, filename=str(fig2_path))
    else:
        p2.draw_rect(fig2_rect, color=(0.18, 0.35, 0.62), fill=(0.94, 0.97, 1.0), width=1.4)
        p2.insert_text((150, 280), "Seccion Cojinete Radial y Sensor RTD ST-104 (1..5)", fontsize=11)

    _fit_textbox(p2,
        pymupdf.Rect(65, 417, 530, 449),
        (
            "Figura 2: Sección transversal del cojinete guía hidrodinámico — (1) Eje principal, "
            "(2) Zapata revestida en Babbitt, (3) Canal de entrada de aceite ISO VG 46, "
            "(4) Sonda de temperatura RTD Pt100 ST-104, (5) Sello laberíntico."
        ),
        fontsize=8.6,
        color=(0.20, 0.28, 0.40),
    )

    p2.insert_text(
        (45, 468),
        "2.1 Umbrales de Temperatura, Presión de Lubricación y Enclavamientos",
        fontsize=12.0,
        color=(0.07, 0.14, 0.25),
    )
    _fit_textbox(p2,
        pymupdf.Rect(45, 478, 550, 522),
        (
            "El PLC de protección monitorea en ciclo de 50 ms la señal de la sonda RTD ST-104 (4). "
            "Si la temperatura de zapata alcanza 75.0 °C se activa la alarma preventiva AL-TEMP-01 y "
            "arranca la bomba auxiliar de lubricación PU-102B. Si alcanza 85.0 °C durante más de 3.0 segundos, "
            "se ejecuta el disparo inmediato (Trip TRIP-TEMP-02) cerrando el distribuidor."
        ),
        fontsize=9.5,
        color=(0.12, 0.15, 0.20),
    )

    p2.insert_text(
        (45, 534),
        "Tabla 2: Umbrales de operación y disparo del sistema de lubricación del cojinete guía.",
        fontsize=8.8,
        color=(0.20, 0.28, 0.40),
    )
    _draw_table(
        p2,
        x0=45,
        y0=542,
        col_widths=[185, 130, 190],
        rows=[
            ["Variable / Sensor", "Rango / Umbral", "Acción del Sistema de Control"],
            ["Temperatura normal (ST-104)", "52.0 °C – 64.0 °C", "Operación nominal con bomba PU-102A"],
            ["Alarma alta temperatura (AL-TEMP-01)", "75.0 °C", "Arranque automático bomba auxiliar PU-102B"],
            ["Disparo crítico / Trip (TRIP-TEMP-02)", "85.0 °C (> 3.0 s)", "Cierre de emergencia y bloqueo maestro 86T"],
            ["Presión mínima colector aceite (PT-108)", "4.2 bar", "Interlock de arranque y alarma por < 3.8 bar"],
            ["Caudal de aceite ISO VG 46", "110.0 L/min", "Filtrado dúplex a 10 micras (deltaP máx 1.2 bar)"],
            ["Holgura diametral de zapata Babbitt", "0.18 mm ± 0.02 mm", "Reemplazar zapata si supera 0.28 mm"],
        ],
    )

    p2.insert_text((45, 818), "Confidencial · Manual_Turbina_HX900.pdf · Página 2 de 3", fontsize=7.5, color=(0.5, 0.5, 0.5))

    # =========================================================================
    # PAGE 3: Hydraulic Governor Circuit Schematic (Vector Diagram) & Fault Codes
    # =========================================================================
    p3 = doc.new_page(width=595, height=842)
    p3.insert_text(
        (45, 30),
        "MANUAL TÉCNICO DE INGENIERÍA Y MANTENIMIENTO · DOC-HX900-REV4",
        fontsize=7.5,
        color=(0.45, 0.50, 0.58),
    )
    p3.insert_text(
        (45, 58),
        "3. Circuito Oleohidráulico del Regulador y Diagnóstico de Fallas",
        fontsize=14.5,
        color=(0.07, 0.14, 0.25),
    )
    _fit_textbox(p3,
        pymupdf.Rect(45, 72, 550, 136),
        (
            "El regulador electrohidráulico PID controla la apertura de los álabes directrices mediante el "
            "acumulador hidroneumático de nitrógeno AC-301 (precargado a 140 bar), la servoválvula "
            "proporcional redundante V-12B y los servomotores gemelos SM-01/SM-02. El esquema funcional "
            "del lazo de presión y disparo de emergencia se presenta en la Figura 3."
        ),
        fontsize=9.6,
        color=(0.12, 0.15, 0.20),
    )

    # Draw a crisp vector engineering schematic (Figura 3) so cluster_drawings() detects it
    diag_box = pymupdf.Rect(55, 145, 540, 315)
    p3.draw_rect(diag_box, color=(0.15, 0.26, 0.42), fill=(0.96, 0.98, 1.0), width=1.5)

    # Nodes inside the vector schematic
    nodes = [
        (pymupdf.Rect(75, 195, 175, 255), (0.12, 0.32, 0.58), "Bomba Alta Presión\nHP-300 (160 bar)"),
        (pymupdf.Rect(205, 165, 310, 220), (0.09, 0.48, 0.44), "Acumulador N2\nAC-301 (PT-301)"),
        (pymupdf.Rect(205, 240, 310, 295), (0.75, 0.34, 0.10), "Válvula Solenoide\nParo Emerg. ESD-86"),
        (pymupdf.Rect(340, 195, 435, 255), (0.12, 0.32, 0.58), "Servoválvula PID\nV-12B (4-20 mA)"),
        (pymupdf.Rect(455, 195, 528, 255), (0.22, 0.26, 0.34), "Servomotor\nDistribuidor SM-01"),
    ]
    for nrect, col, label in nodes:
        p3.draw_rect(nrect, color=col, fill=(1.0, 1.0, 1.0), width=1.6)
        _fit_textbox(p3, nrect, label, fontsize=8.0, color=(0.10, 0.14, 0.22), align=1)

    # Connecting hydraulic lines
    p3.draw_line((175, 225), (205, 192), color=(0.12, 0.32, 0.58), width=2.0)
    p3.draw_line((175, 225), (205, 268), color=(0.75, 0.34, 0.10), width=2.0)
    p3.draw_line((310, 192), (340, 225), color=(0.09, 0.48, 0.44), width=2.0)
    p3.draw_line((310, 268), (340, 225), color=(0.75, 0.34, 0.10), width=2.0)
    p3.draw_line((435, 225), (455, 225), color=(0.12, 0.32, 0.58), width=2.2)

    _fit_textbox(p3,
        pymupdf.Rect(55, 320, 540, 348),
        (
            "Figura 3: Esquema funcional del circuito oleohidráulico del gobernador — Bomba HP-300, "
            "Acumulador de nitrógeno AC-301, Válvula de paro ESD-86, Servoválvula proporcional V-12B y Servomotor SM-01."
        ),
        fontsize=8.6,
        color=(0.20, 0.28, 0.40),
    )

    p3.insert_text(
        (45, 368),
        "3.1 Matriz de Códigos de Falla, Vibraciones ISO-10816-5 y Procedimientos",
        fontsize=12.0,
        color=(0.07, 0.14, 0.25),
    )
    _fit_textbox(p3,
        pymupdf.Rect(45, 378, 550, 442),
        (
            "De acuerdo con la norma ISO-10816-5, la velocidad de vibración radial en el alojamiento del "
            "cojinete guía no debe superar 2.8 mm/s RMS en operación continua (Zona A/B). Una lectura "
            "superior a 4.5 mm/s RMS dispara el código E-512. Asimismo, cuando el transductor de presión "
            "PT-301 en el acumulador AC-301 cae por debajo de 115 bar, se registra el código crítico E-409 "
            "activando la válvula solenoide ESD-86. Consulte la Tabla 3 para el protocolo de resolución."
        ),
        fontsize=9.5,
        color=(0.12, 0.15, 0.20),
    )

    p3.insert_text(
        (45, 452),
        "Tabla 3: Códigos de falla del sistema de supervisión, causas raíz y acciones correctivas.",
        fontsize=8.8,
        color=(0.20, 0.28, 0.40),
    )
    _draw_table(
        p3,
        x0=45,
        y0=460,
        col_widths=[75, 195, 235],
        row_height=24.0,
        rows=[
            ["Código", "Condición de Disparo / Síntoma", "Acción Correctiva Autorizada"],
            ["E-409", "Presión acumulador AC-301 < 115 bar (PT-301)", "Verificar precarga de N2 (140 bar) y fugas en servoválvula V-12B"],
            ["E-512", "Vibración radial > 4.5 mm/s RMS (ISO-10816-5)", "Inspeccionar holgura de zapatas (0.18 mm) y balanceo del rodete"],
            ["E-607", "Pulsación severa en tubo aspiración (PT-204 > 1.8 bar)", "Abrir válvula de admisión de aire anti-vórtice y subir carga > 45%"],
            ["E-703", "Desviación de posición en servoválvula V-12B > 3.5%", "Purgar aire del circuito HP-300 y recalibrar lazo 4-20 mA"],
            ["AL-TEMP-01", "Temperatura zapata RTD ST-104 >= 75.0 °C", "Confirmar entrada en servicio de bomba auxiliar PU-102B (110 L/min)"],
        ],
    )

    p3.insert_text((45, 818), "Confidencial · Manual_Turbina_HX900.pdf · Página 3 de 3", fontsize=7.5, color=(0.5, 0.5, 0.5))

    doc.save(str(output_path))
    doc.close()
    return output_path
