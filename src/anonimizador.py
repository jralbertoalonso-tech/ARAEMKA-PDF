"""
Anonimización de PDFs y de imágenes embebidas.

Funciones principales:
    - anonimizar_documento(doc, eliminar_anotaciones)
        Limpia metadatos clásicos y XMP de un objeto fitz.Document abierto.
    - limpiar_exif(imagen_pil)
        Devuelve una copia de una imagen PIL sin datos EXIF / GPS / fechas.

Estas funciones están pensadas para ser invocadas desde los módulos
`compresor` y `conversor` mientras tienen un documento ya abierto, de
forma que la anonimización se aplique en una única pasada (sin reabrir
el archivo).
"""

from __future__ import annotations

import io

try:  # PyMuPDF ≥ 1.24 recomienda el nuevo nombre del paquete
    import pymupdf as fitz  # type: ignore
except ImportError:  # compatibilidad con instalaciones antiguas
    import fitz  # PyMuPDF
from PIL import Image


# ---------------------------------------------------------------------------
# PDFs
# ---------------------------------------------------------------------------

# Claves estándar de metadatos en PDFs que vamos a vaciar
_CLAVES_METADATOS_PDF = (
    "title", "author", "subject", "keywords",
    "creator", "producer", "creationDate", "modDate", "trapped", "format",
)


def anonimizar_documento(doc: fitz.Document, eliminar_anotaciones: bool = False) -> None:
    """
    Anonimiza un documento PyMuPDF en memoria:

      1. Vacía los metadatos clásicos (autor, título, software, fechas...).
      2. Elimina los metadatos XMP de Adobe.
      3. Opcionalmente, borra todas las anotaciones / comentarios.

    El documento NO se guarda aquí; el código que lo invoca debe llamar a
    `doc.save(...)` con `garbage=4, deflate=True, clean=True` para que
    los cambios se materialicen en disco.
    """
    # 1) Metadatos básicos: forzamos cadenas vacías para todas las claves
    metadatos_vacios = {clave: "" for clave in _CLAVES_METADATOS_PDF}
    try:
        doc.set_metadata(metadatos_vacios)
    except Exception:
        # En versiones muy antiguas de PyMuPDF puede ser estricto con las
        # claves; en ese caso recurrimos a un dict completamente vacío.
        try:
            doc.set_metadata({})
        except Exception:
            pass

    # 2) Metadatos XMP (extendidos / Adobe)
    try:
        doc.del_xml_metadata()
    except AttributeError:
        # set_xml_metadata("") también funciona en versiones recientes
        try:
            doc.set_xml_metadata("")
        except Exception:
            pass

    # 3) Anotaciones y comentarios
    if eliminar_anotaciones:
        for pagina in doc:
            anotaciones = list(pagina.annots() or [])
            for anot in anotaciones:
                try:
                    pagina.delete_annot(anot)
                except Exception:
                    continue
            # Widgets de formularios que pueden contener nombre de usuario
            try:
                widgets = list(pagina.widgets() or [])
                for w in widgets:
                    try:
                        pagina.delete_widget(w)
                    except Exception:
                        continue
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Imágenes (EXIF / GPS / cámara / fecha exacta)
# ---------------------------------------------------------------------------

def limpiar_exif(imagen: Image.Image) -> Image.Image:
    """
    Devuelve una nueva imagen PIL idéntica a la original pero SIN
    metadatos EXIF, IPTC, XMP, ICC ni tira de iconos (thumbnail).

    El truco habitual: re-construir la imagen desde cero a partir de los
    píxeles. La nueva instancia no hereda ningún diccionario `info`.
    """
    if imagen.mode in ("P",):
        imagen = imagen.convert("RGBA" if "transparency" in imagen.info else "RGB")

    # Reconstruir desde los bytes crudos es mucho más rápido que putdata()
    # (importante en fotos de móvil de 12+ Mpx) y el resultado es idéntico.
    nueva = Image.frombytes(imagen.mode, imagen.size, imagen.tobytes())
    return nueva


def imagen_a_bytes_sin_metadatos(imagen: Image.Image, formato: str = "JPEG",
                                 calidad: int = 92) -> bytes:
    """
    Devuelve los bytes codificados de una imagen sin EXIF y lista para
    incrustar en un PDF.
    """
    img = limpiar_exif(imagen)

    # JPEG no admite RGBA: convertimos a RGB sobre fondo blanco
    if formato.upper() == "JPEG" and img.mode in ("RGBA", "LA"):
        fondo = Image.new("RGB", img.size, (255, 255, 255))
        if img.mode == "RGBA":
            fondo.paste(img, mask=img.split()[3])
        else:
            fondo.paste(img.convert("RGB"))
        img = fondo

    buffer = io.BytesIO()
    parametros = {"format": formato}
    if formato.upper() == "JPEG":
        parametros.update({"quality": calidad, "optimize": True})
    img.save(buffer, **parametros)
    return buffer.getvalue()
