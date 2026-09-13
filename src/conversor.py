"""
Conversión de imágenes (JPG, JPEG, PNG, TIFF, BMP, WEBP) a PDF.

Modos:
    - Una imagen = un PDF                 (genera N PDFs)
    - Varias imágenes = un único PDF      (genera 1 PDF multipágina)

Opciones:
    - Tamaño de página: A4, Carta o "ajustar al tamaño de imagen".
    - Orientación: automática, vertical, horizontal.
    - Margen en milímetros.
    - Anonimización (limpia EXIF antes de incrustar la imagen).
    - Compresión automática del PDF resultante (delegada al módulo
      `compresor`).
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

try:  # PyMuPDF ≥ 1.24 recomienda el nuevo nombre del paquete
    import pymupdf as fitz  # type: ignore
except ImportError:  # compatibilidad con instalaciones antiguas
    import fitz  # PyMuPDF
from PIL import Image

from . import anonimizador, compresor
from .utilidades import (
    TAMANO_A4,
    TAMANO_CARTA,
    mm_a_puntos,
    ruta_salida_unica,
)


# ---------------------------------------------------------------------------
# Opciones / resultado
# ---------------------------------------------------------------------------

@dataclass
class OpcionesConversion:
    multipagina: bool = False                      # True = un PDF con todas
    tamano_pagina: str = "A4"                      # 'A4' | 'carta' | 'ajustar'
    orientacion: str = "auto"                      # 'auto' | 'vertical' | 'horizontal'
    margen_mm: float = 10.0
    anonimizar: bool = False
    eliminar_anotaciones: bool = False             # solo si anonimizar
    comprimir_resultado: bool = False
    opciones_compresion: compresor.OpcionesCompresion | None = None


@dataclass
class ResultadoConversion:
    pdfs_generados: list[Path] = field(default_factory=list)
    tamano_total: int = 0


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------

def convertir(rutas_imagenes: list[str | Path],
              carpeta_salida: str | Path,
              opciones: OpcionesConversion,
              nombre_pdf_combinado: str = "imagenes_combinadas.pdf",
              progreso_callback=None) -> ResultadoConversion:
    """
    Convierte la lista de imágenes en uno o varios PDFs según las opciones.

    Args:
        rutas_imagenes: lista ya ordenada (la GUI controla el orden).
        carpeta_salida: carpeta donde se guardarán los PDFs.
        opciones: ver `OpcionesConversion`.
        nombre_pdf_combinado: nombre para el PDF en modo multipágina.
        progreso_callback: función opcional `cb(texto, indice, total)`.

    Returns:
        `ResultadoConversion` con la lista de PDFs creados y su tamaño total.
    """
    rutas_imagenes = [Path(p) for p in rutas_imagenes]
    carpeta_salida = Path(carpeta_salida)
    carpeta_salida.mkdir(parents=True, exist_ok=True)

    resultado = ResultadoConversion()

    if opciones.multipagina:
        ruta_destino = ruta_salida_unica(
            carpeta_salida, Path(nombre_pdf_combinado).stem, ".pdf"
        )
        _construir_pdf(
            rutas_imagenes,
            ruta_destino,
            opciones,
            progreso_callback=progreso_callback,
        )
        if opciones.comprimir_resultado:
            ruta_destino = _comprimir_in_situ(ruta_destino, opciones)
        resultado.pdfs_generados.append(ruta_destino)

    else:
        total = len(rutas_imagenes)
        for i, ruta_img in enumerate(rutas_imagenes, start=1):
            if progreso_callback:
                progreso_callback(f"Procesando {ruta_img.name}", i, total)

            ruta_destino = ruta_salida_unica(
                carpeta_salida, ruta_img.stem, ".pdf"
            )
            _construir_pdf([ruta_img], ruta_destino, opciones)
            if opciones.comprimir_resultado:
                ruta_destino = _comprimir_in_situ(ruta_destino, opciones)
            resultado.pdfs_generados.append(ruta_destino)

    resultado.tamano_total = sum(p.stat().st_size for p in resultado.pdfs_generados)
    return resultado


# ---------------------------------------------------------------------------
# Construcción del PDF
# ---------------------------------------------------------------------------

def _construir_pdf(rutas_imagenes: list[Path],
                   ruta_salida: Path,
                   opciones: OpcionesConversion,
                   progreso_callback=None) -> None:
    """Crea un PDF con una página por cada imagen de la lista."""
    doc = fitz.open()
    try:
        margen_pt = mm_a_puntos(opciones.margen_mm)
        total = len(rutas_imagenes)

        for i, ruta_img in enumerate(rutas_imagenes, start=1):
            if progreso_callback:
                progreso_callback(
                    f"Añadiendo {ruta_img.name}", i, total
                )
            _agregar_pagina_con_imagen(doc, ruta_img, opciones, margen_pt)

        if opciones.anonimizar:
            anonimizador.anonimizar_documento(
                doc, eliminar_anotaciones=opciones.eliminar_anotaciones
            )

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


def _agregar_pagina_con_imagen(doc: fitz.Document,
                               ruta_imagen: Path,
                               opciones: OpcionesConversion,
                               margen_pt: float) -> None:
    """Añade al documento una página con la imagen dada centrada y respetando proporción."""
    with Image.open(ruta_imagen) as img:
        # Honrar la rotación EXIF antes de leer dimensiones reales
        img = _aplicar_orientacion_exif(img)
        ancho_img, alto_img = img.size
        modo_original = img.mode

        # ---- Decidir orientación ----
        if opciones.orientacion == "auto":
            es_horizontal = ancho_img > alto_img
        elif opciones.orientacion == "horizontal":
            es_horizontal = True
        else:  # 'vertical'
            es_horizontal = False

        # ---- Decidir tamaño de página (en puntos) ----
        if opciones.tamano_pagina == "ajustar":
            # 1 px = 1 pt: la página queda exactamente del tamaño de la imagen.
            ancho_pag = ancho_img + 2 * margen_pt
            alto_pag = alto_img + 2 * margen_pt
        else:
            base = TAMANO_A4 if opciones.tamano_pagina == "A4" else TAMANO_CARTA
            v_w, v_h = base                  # vertical (portrait)
            if es_horizontal:
                ancho_pag, alto_pag = v_h, v_w
            else:
                ancho_pag, alto_pag = v_w, v_h

        pagina = doc.new_page(width=ancho_pag, height=alto_pag)
        rect = fitz.Rect(
            margen_pt,
            margen_pt,
            ancho_pag - margen_pt,
            alto_pag - margen_pt,
        )

        # ---- Anonimización: re-codificar imagen sin EXIF ----
        if opciones.anonimizar:
            stream = anonimizador.imagen_a_bytes_sin_metadatos(
                img,
                formato="JPEG" if modo_original != "RGBA" else "PNG",
                calidad=92,
            )
            pagina.insert_image(rect, stream=stream, keep_proportion=True)
        else:
            # PyMuPDF acepta directamente la ruta original
            pagina.insert_image(rect, filename=str(ruta_imagen), keep_proportion=True)


def _aplicar_orientacion_exif(img: Image.Image) -> Image.Image:
    """
    Si la imagen viene con EXIF Orientation (típico de fotos de móvil),
    rotar los píxeles para que se vea bien y descartar la etiqueta.
    """
    try:
        from PIL import ImageOps
        return ImageOps.exif_transpose(img)
    except Exception:
        return img


# ---------------------------------------------------------------------------
# Comprimir el PDF resultante (opción del usuario)
# ---------------------------------------------------------------------------

def _comprimir_in_situ(ruta_pdf: Path,
                       opciones: OpcionesConversion) -> Path:
    """
    Aplica la compresión usando el módulo `compresor` y reemplaza el archivo
    original por la versión comprimida.
    """
    opc_comp = opciones.opciones_compresion or compresor.OpcionesCompresion(
        nivel="equilibrada",
        dpi=150,
        escala_grises=False,
        anonimizar=opciones.anonimizar,
        eliminar_anotaciones=opciones.eliminar_anotaciones,
    )

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        ruta_tmp = Path(tmp.name)
    try:
        compresor.comprimir_pdf(ruta_pdf, ruta_tmp, opc_comp)
        # Reemplazar
        shutil.move(str(ruta_tmp), str(ruta_pdf))
    finally:
        if ruta_tmp.exists():
            try:
                ruta_tmp.unlink()
            except Exception:
                pass
    return ruta_pdf
