"""
Lógica de compresión de PDFs usando exclusivamente PyMuPDF + Pillow.
No depende de Ghostscript ni de ningún binario externo.

Estrategia:
  1. Abrir el PDF.
  2. Por cada imagen embebida (xref), extraerla, redimensionarla al DPI
     objetivo, opcionalmente convertir a escala de grises, recomprimir
     como JPEG y reemplazar la imagen original dentro del PDF.
  3. Si está activa la anonimización, vaciar metadatos y XMP.
  4. Guardar con basura=4, deflate=True, clean=True para sacar todo el
     provecho de la compresión nativa de PyMuPDF.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

try:  # PyMuPDF ≥ 1.24 recomienda el nuevo nombre del paquete
    import pymupdf as fitz  # type: ignore
except ImportError:  # compatibilidad con instalaciones antiguas
    import fitz  # PyMuPDF
from PIL import Image

from . import anonimizador
from .utilidades import porcentaje_reduccion


# ---------------------------------------------------------------------------
# Configuración de niveles
# ---------------------------------------------------------------------------

# Calidad JPEG por nivel (1-95). DPI predeterminado por nivel.
PRESETS_NIVEL = {
    "maxima":      {"calidad_jpeg": 40, "dpi_predet": 96},
    "equilibrada": {"calidad_jpeg": 65, "dpi_predet": 150},
    "alta":        {"calidad_jpeg": 85, "dpi_predet": 200},
}


@dataclass
class OpcionesCompresion:
    nivel: str = "equilibrada"          # 'maxima' | 'equilibrada' | 'alta'
    dpi: int = 150                       # 72 / 150 / 300 (configurable por GUI)
    escala_grises: bool = False
    anonimizar: bool = False
    eliminar_anotaciones: bool = False


@dataclass
class ResultadoCompresion:
    ruta_origen: Path
    ruta_destino: Path
    tamano_original: int
    tamano_final: int

    @property
    def reduccion_pct(self) -> float:
        return porcentaje_reduccion(self.tamano_original, self.tamano_final)


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------

def comprimir_pdf(ruta_entrada: str | Path,
                  ruta_salida: str | Path,
                  opciones: OpcionesCompresion) -> ResultadoCompresion:
    """
    Comprime un único PDF aplicando las opciones indicadas.
    Devuelve un `ResultadoCompresion` con los tamaños y rutas.
    """
    ruta_entrada = Path(ruta_entrada)
    ruta_salida = Path(ruta_salida)

    tamano_original = ruta_entrada.stat().st_size
    calidad_jpeg = PRESETS_NIVEL[opciones.nivel]["calidad_jpeg"]

    doc = fitz.open(ruta_entrada)
    if doc.needs_pass:
        doc.close()
        raise ValueError(
            f"«{ruta_entrada.name}» está protegido con contraseña. "
            "Desbloquéalo antes de comprimirlo."
        )
    try:
        _recomprimir_imagenes(
            doc,
            dpi_objetivo=opciones.dpi,
            calidad_jpeg=calidad_jpeg,
            escala_grises=opciones.escala_grises,
        )

        if opciones.anonimizar:
            anonimizador.anonimizar_documento(
                doc, eliminar_anotaciones=opciones.eliminar_anotaciones
            )

        # garbage=4 : elimina objetos no referenciados (XMP huérfano, etc.)
        # deflate=True / deflate_images=True / deflate_fonts=True : comprimir streams
        # clean=True : reescribe la sintaxis para ser lo más compacta posible
        doc.save(
            ruta_salida,
            garbage=4,
            deflate=True,
            deflate_images=True,
            deflate_fonts=True,
            clean=True,
        )
    finally:
        doc.close()

    return ResultadoCompresion(
        ruta_origen=ruta_entrada,
        ruta_destino=ruta_salida,
        tamano_original=tamano_original,
        tamano_final=ruta_salida.stat().st_size,
    )


# ---------------------------------------------------------------------------
# Recompresión de imágenes embebidas
# ---------------------------------------------------------------------------

def _recomprimir_imagenes(doc: fitz.Document,
                          dpi_objetivo: int,
                          calidad_jpeg: int,
                          escala_grises: bool) -> None:
    """
    Recorre todas las imágenes del documento y las sustituye por versiones
    redimensionadas y recomprimidas. PyMuPDF se encarga internamente de
    actualizar todas las páginas que referencian al mismo xref.
    """
    xrefs_procesados: set[int] = set()

    for indice_pagina in range(len(doc)):
        pagina = doc[indice_pagina]
        for info in pagina.get_images(full=True):
            xref = info[0]
            if xref in xrefs_procesados:
                continue
            xrefs_procesados.add(xref)

            try:
                base = doc.extract_image(xref)
            except Exception:
                continue
            if not base:
                continue

            try:
                nuevo_stream = _generar_imagen_optimizada(
                    base["image"], dpi_objetivo, calidad_jpeg, escala_grises
                )
            except Exception:
                # Si la imagen no se puede recomprimir, la dejamos tal cual.
                continue

            if not nuevo_stream:
                continue

            # Reemplazar (replace_image actualiza TODAS las apariciones del xref)
            try:
                pagina.replace_image(xref, stream=nuevo_stream)
            except Exception:
                continue


def _generar_imagen_optimizada(bytes_imagen: bytes,
                               dpi_objetivo: int,
                               calidad_jpeg: int,
                               escala_grises: bool) -> bytes | None:
    """
    Convierte los bytes de una imagen original a un JPEG redimensionado y
    recomprimido. Devuelve `None` si la imagen es demasiado pequeña para
    aplicarle compresión con sentido.
    """
    pil = Image.open(io.BytesIO(bytes_imagen))

    # Aplanar paletas y canales alfa para JPEG.
    if pil.mode in ("P", "RGBA", "LA"):
        fondo = Image.new("RGB", pil.size, (255, 255, 255))
        if pil.mode == "RGBA":
            fondo.paste(pil, mask=pil.split()[3])
        elif pil.mode == "LA":
            fondo.paste(pil.convert("RGBA"), mask=pil.convert("RGBA").split()[3])
        else:
            fondo.paste(pil.convert("RGB"))
        pil = fondo
    elif pil.mode not in ("RGB", "L"):
        pil = pil.convert("RGB")

    # Redimensionado en función del DPI objetivo. Tomamos como referencia la
    # dimensión más larga de una página A4 (~8.27" de ancho x 11.69" de alto).
    # max_pixels = ancho A4 * dpi  → da el píxel-tope razonable para esa página.
    max_pixels = int(8.5 * dpi_objetivo)
    ancho, alto = pil.size

    if max(ancho, alto) > max_pixels:
        factor = max_pixels / max(ancho, alto)
        nuevas = (max(1, int(ancho * factor)), max(1, int(alto * factor)))
        pil = pil.resize(nuevas, Image.LANCZOS)

    # Escala de grises
    if escala_grises and pil.mode != "L":
        pil = pil.convert("L")

    # Recompresión
    salida = io.BytesIO()
    pil.save(salida, format="JPEG", quality=int(calidad_jpeg), optimize=True)
    bytes_resultantes = salida.getvalue()

    # Si la imagen recomprimida es MAYOR que la original (caso típico de
    # imágenes ya muy comprimidas), la descartamos para no engordar el PDF.
    if len(bytes_resultantes) >= len(bytes_imagen) * 0.95:
        return None

    return bytes_resultantes
