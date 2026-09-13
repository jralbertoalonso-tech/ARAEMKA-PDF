"""
Utilidades comunes: formato de tamaños, cálculo de unidades, parsing de
rutas que devuelve tkinterdnd2 al hacer drag-and-drop, etc.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Formato de tamaños
# ---------------------------------------------------------------------------

def formatear_tamano(bytes_: int) -> str:
    """Devuelve una cadena legible para humanos (KB, MB, GB)."""
    if bytes_ is None:
        return "—"
    unidades = ["B", "KB", "MB", "GB", "TB"]
    valor = float(bytes_)
    for unidad in unidades:
        if valor < 1024 or unidad == unidades[-1]:
            if unidad == "B":
                return f"{int(valor)} {unidad}"
            return f"{valor:.2f} {unidad}"
        valor /= 1024
    return f"{valor:.2f} TB"


def porcentaje_reduccion(original: int, final: int) -> float:
    """Porcentaje de reducción de tamaño (positivo = reducción)."""
    if original <= 0:
        return 0.0
    return (1 - final / original) * 100


# ---------------------------------------------------------------------------
# Conversión de unidades de página (puntos PDF)
# ---------------------------------------------------------------------------

def mm_a_puntos(mm: float) -> float:
    """Convierte milímetros a puntos PDF (72 pt = 25.4 mm)."""
    return mm * 72 / 25.4


# Tamaños de página estándar en puntos (vertical / portrait)
TAMANO_A4 = (595.276, 841.890)        # 210 x 297 mm
TAMANO_CARTA = (612.0, 792.0)         # 8.5 x 11 in


# ---------------------------------------------------------------------------
# Drag-and-drop: parsing de la cadena que entrega tkinterdnd2
# ---------------------------------------------------------------------------

def parsear_rutas_dnd(widget, data: str) -> list[str]:
    """
    tkinterdnd2 entrega rutas separadas por espacios y, si una ruta
    contiene espacios, la encierra en llaves: "{C:/Mis Documentos/a.pdf} otra.pdf".
    Tk ofrece splitlist() que respeta esa convención.
    """
    try:
        return [str(p) for p in widget.tk.splitlist(data)]
    except Exception:
        # Fallback muy básico
        return [data.strip()]


# ---------------------------------------------------------------------------
# Helpers de archivos
# ---------------------------------------------------------------------------

EXTENSIONES_IMAGEN = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}
EXTENSION_PDF = ".pdf"


def es_pdf(ruta: str | Path) -> bool:
    return Path(ruta).suffix.lower() == EXTENSION_PDF


def es_imagen(ruta: str | Path) -> bool:
    return Path(ruta).suffix.lower() in EXTENSIONES_IMAGEN


def ruta_salida_unica(directorio: Path, nombre_base: str, extension: str = ".pdf") -> Path:
    """
    Devuelve una ruta dentro de `directorio` que no exista todavía
    añadiendo un sufijo numérico si fuera necesario.
    """
    directorio = Path(directorio)
    candidato = directorio / f"{nombre_base}{extension}"
    contador = 1
    while candidato.exists():
        candidato = directorio / f"{nombre_base} ({contador}){extension}"
        contador += 1
    return candidato


def abrir_carpeta(ruta: str | Path) -> None:
    """
    Abre una carpeta en el explorador de archivos del sistema
    (Finder en macOS, Explorador en Windows). Falla en silencio si el
    sistema no lo permite: es solo una comodidad, nunca debe romper nada.
    """
    ruta = str(Path(ruta))
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", ruta])
        elif os.name == "nt":
            os.startfile(ruta)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", ruta])
    except Exception:
        pass
