"""
Conversión de PDF a Word (.docx) con el motor pdf2docx (máxima fidelidad).

pdf2docx reconstruye el documento analizando el PDF con PyMuPDF y OpenCV:
párrafos con sus fuentes y estilos, imágenes, y tablas (incluidas las de
rejilla dibujada). La conversión nunca es perfecta al 100 % — el PDF no
conserva la estructura original del documento — pero es de las mejores
disponibles sin servicios en la nube.

Limitación importante: los PDF escaneados (imagen pura) no contienen texto
que extraer; el Word resultante llevará las imágenes de las páginas. Para
texto real haría falta OCR (posible mejora futura).

Como el resto de módulos, aquí no hay interfaz gráfica: la GUI llama a
`convertir_pdf_a_docx` desde su hilo de trabajo, un PDF cada vez.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from . import organizador

# pdf2docx y fontTools son muy verbosos por defecto
for _nombre in ("pdf2docx", "fontTools"):
    logging.getLogger(_nombre).setLevel(logging.ERROR)


# ---------------------------------------------------------------------------
# Opciones / resultado
# ---------------------------------------------------------------------------

@dataclass
class OpcionesPdfADocx:
    rangos_texto: str = ""          # "" = todas las páginas; ej. "1-3, 5"
    anonimizar: bool = False        # vaciar los metadatos del .docx generado


@dataclass
class ResultadoPdfADocx:
    ruta_destino: Path
    num_paginas: int                # páginas convertidas
    tamano_final: int
    aviso: str | None = None       # p. ej., PDF escaneado


# ---------------------------------------------------------------------------
# Detección de PDFs escaneados
# ---------------------------------------------------------------------------

# Menos de ~30 caracteres de media por página (en las primeras páginas)
# indica casi con seguridad un escaneo sin capa de texto.
_UMBRAL_CARACTERES_PAGINA = 30


def _parece_escaneado(doc) -> bool:
    paginas_a_mirar = min(len(doc), 5)
    if paginas_a_mirar == 0:
        return False
    total = sum(
        len(doc[i].get_text().strip()) for i in range(paginas_a_mirar)
    )
    return total < _UMBRAL_CARACTERES_PAGINA * paginas_a_mirar


# ---------------------------------------------------------------------------
# Conversión
# ---------------------------------------------------------------------------

def convertir_pdf_a_docx(ruta_entrada: str | Path,
                         ruta_salida: str | Path,
                         opciones: OpcionesPdfADocx | None = None,
                         ) -> ResultadoPdfADocx:
    """
    Convierte un PDF en un .docx. Lanza ValueError con mensajes claros si el
    PDF está cifrado, dañado o el rango de páginas no es válido.
    """
    # Import diferido: cargar pdf2docx (OpenCV + NumPy) tarda un par de
    # segundos y así no penaliza el arranque de la aplicación.
    from pdf2docx import Converter

    opciones = opciones or OpcionesPdfADocx()
    ruta_entrada = Path(ruta_entrada)
    ruta_salida = Path(ruta_salida)

    # Validación previa (contraseña / corrupto) + nº de páginas + escaneado
    doc = organizador.abrir_pdf(ruta_entrada)
    try:
        total_paginas = len(doc)
        escaneado = _parece_escaneado(doc)
    finally:
        doc.close()

    # Rangos de páginas opcionales ("1-3, 5" → índices 0-based)
    paginas = None
    if opciones.rangos_texto.strip():
        tramos = organizador.parsear_rangos(opciones.rangos_texto, total_paginas)
        paginas = sorted({
            p for inicio, fin in tramos for p in range(inicio - 1, fin)
        })

    conversor = Converter(str(ruta_entrada))
    try:
        conversor.convert(str(ruta_salida), pages=paginas)
    finally:
        try:
            conversor.close()
        except Exception:
            pass

    if not ruta_salida.exists():
        raise ValueError(
            f"No se pudo generar el Word de «{ruta_entrada.name}». "
            "El PDF puede tener una estructura no compatible."
        )

    _ajustar_metadatos_docx(
        ruta_salida,
        anonimizar=opciones.anonimizar,
        titulo=ruta_entrada.stem,
    )

    aviso = None
    if escaneado:
        aviso = (
            f"«{ruta_entrada.name}» parece un PDF escaneado (casi sin texto): "
            "el Word contendrá las páginas como imagen, no texto editable. "
            "Para extraer el texto haría falta OCR."
        )

    return ResultadoPdfADocx(
        ruta_destino=ruta_salida,
        num_paginas=len(paginas) if paginas is not None else total_paginas,
        tamano_final=ruta_salida.stat().st_size,
        aviso=aviso,
    )


def _ajustar_metadatos_docx(ruta_docx: Path, anonimizar: bool,
                            titulo: str = "") -> None:
    """
    python-docx firma los documentos con autor «python-docx»; aquí lo
    sustituimos. Con `anonimizar`, se vacían todos los metadatos personales.
    Nunca debe tumbar la conversión: cualquier fallo se ignora.
    """
    try:
        from docx import Document

        d = Document(str(ruta_docx))
        propiedades = d.core_properties
        if anonimizar:
            propiedades.title = ""
            propiedades.subject = ""
            propiedades.keywords = ""
            propiedades.comments = ""
            propiedades.author = ""
            propiedades.last_modified_by = ""
        else:
            propiedades.author = "ARAEMKA PDF"
            propiedades.last_modified_by = ""
            propiedades.title = titulo
            propiedades.comments = ""
        d.save(str(ruta_docx))
    except Exception:
        pass
