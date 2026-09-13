"""
Organización de PDFs: unir, dividir y reordenar / editar páginas.

Funciones principales (todas 100 % locales, motor PyMuPDF):

    - unir_pdfs(rutas, ruta_salida, opciones)
        Une varios PDFs en uno solo, en el orden recibido.

    - dividir_pdf(ruta, carpeta_salida, opciones)
        Divide un PDF en varios según el modo elegido:
            * 'rangos'   : rangos personalizados escritos por el usuario
                           (ej. "1-3, 5, 8-10" → un PDF por cada tramo).
            * 'cada_n'   : bloques consecutivos de N páginas.
            * 'paginas'  : un PDF independiente por cada página.

    - recomponer_pdf(ruta, paginas, ruta_salida, opciones)
        Genera un PDF nuevo aplicando el orden, las rotaciones y las
        eliminaciones indicadas en la lista `paginas`. Sirve tanto para
        "reordenar" como para "extraer páginas" (pasando solo un subconjunto).

    - render_miniatura_png(doc, indice, rotacion_extra, max_px)
        Devuelve los bytes PNG de una miniatura de la página, para la
        vista previa de la interfaz.

Como en el resto de módulos, aquí NO hay nada de interfaz gráfica: la GUI
llama a estas funciones desde un hilo de trabajo.
"""

from __future__ import annotations

import io
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

try:  # PyMuPDF ≥ 1.24 recomienda el nuevo nombre del paquete
    import pymupdf as fitz  # type: ignore
except ImportError:  # compatibilidad con instalaciones antiguas
    import fitz  # PyMuPDF

from . import anonimizador, compresor
from .utilidades import ruta_salida_unica


# Parámetros de guardado compacto usados en todo el proyecto.
GUARDADO_COMPACTO = dict(
    garbage=4,
    deflate=True,
    deflate_images=True,
    deflate_fonts=True,
    clean=True,
)


# ---------------------------------------------------------------------------
# Utilidades comunes
# ---------------------------------------------------------------------------

def abrir_pdf(ruta: str | Path) -> fitz.Document:
    """
    Abre un PDF para su procesamiento y valida que sea utilizable.
    Lanza ValueError con un mensaje claro si está cifrado o dañado.
    """
    ruta = Path(ruta)
    try:
        doc = fitz.open(ruta)
    except Exception as e:
        raise ValueError(f"No se pudo abrir «{ruta.name}»: {e}") from e

    if doc.needs_pass:
        doc.close()
        raise ValueError(
            f"«{ruta.name}» está protegido con contraseña. "
            "Desbloquéalo antes de procesarlo."
        )
    if len(doc) == 0:
        doc.close()
        raise ValueError(f"«{ruta.name}» no contiene ninguna página.")
    return doc


def numero_paginas(ruta: str | Path) -> int:
    """Número de páginas de un PDF (validando que se pueda abrir)."""
    doc = abrir_pdf(ruta)
    try:
        return len(doc)
    finally:
        doc.close()


def _anonimizar_si_procede(doc: fitz.Document, anonimizar: bool,
                           eliminar_anotaciones: bool) -> None:
    if anonimizar:
        anonimizador.anonimizar_documento(
            doc, eliminar_anotaciones=eliminar_anotaciones
        )


def _comprimir_in_situ(ruta_pdf: Path) -> Path:
    """
    Comprime el PDF con el nivel 'equilibrada' del módulo `compresor` y
    reemplaza el archivo por la versión comprimida (igual que hace el
    conversor de imágenes).
    """
    opciones = compresor.OpcionesCompresion(nivel="equilibrada", dpi=150)
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        ruta_tmp = Path(tmp.name)
    try:
        compresor.comprimir_pdf(ruta_pdf, ruta_tmp, opciones)
        shutil.move(str(ruta_tmp), str(ruta_pdf))
    finally:
        if ruta_tmp.exists():
            try:
                ruta_tmp.unlink()
            except Exception:
                pass
    return ruta_pdf


# ---------------------------------------------------------------------------
# 1) UNIR VARIOS PDFs EN UNO
# ---------------------------------------------------------------------------

@dataclass
class OpcionesUnion:
    anonimizar: bool = False
    eliminar_anotaciones: bool = False
    comprimir_resultado: bool = False


@dataclass
class ResultadoUnion:
    ruta_destino: Path
    num_archivos: int
    num_paginas: int
    tamano_final: int


def unir_pdfs(rutas: list[str | Path],
              ruta_salida: str | Path,
              opciones: OpcionesUnion | None = None,
              progreso_callback=None) -> ResultadoUnion:
    """
    Une los PDFs de `rutas` (en ese mismo orden) en un único archivo.

    Args:
        rutas: lista ordenada de PDFs de entrada (mínimo 2).
        ruta_salida: ruta del PDF resultante.
        opciones: ver `OpcionesUnion`.
        progreso_callback: función opcional `cb(texto, indice, total)`.
    """
    opciones = opciones or OpcionesUnion()
    rutas = [Path(r) for r in rutas]
    ruta_salida = Path(ruta_salida)

    if len(rutas) < 2:
        raise ValueError("Añade al menos dos PDFs a la lista para unirlos.")

    destino = fitz.open()
    try:
        total = len(rutas)
        for i, ruta in enumerate(rutas, start=1):
            if progreso_callback:
                progreso_callback(f"Uniendo {ruta.name}", i, total)
            origen = abrir_pdf(ruta)
            try:
                destino.insert_pdf(origen)
            finally:
                origen.close()

        _anonimizar_si_procede(
            destino, opciones.anonimizar, opciones.eliminar_anotaciones
        )
        num_paginas = len(destino)
        destino.save(ruta_salida, **GUARDADO_COMPACTO)
    finally:
        destino.close()

    if opciones.comprimir_resultado:
        if progreso_callback:
            progreso_callback("Comprimiendo el PDF resultante…", len(rutas), len(rutas))
        _comprimir_in_situ(ruta_salida)

    return ResultadoUnion(
        ruta_destino=ruta_salida,
        num_archivos=len(rutas),
        num_paginas=num_paginas,
        tamano_final=ruta_salida.stat().st_size,
    )


# ---------------------------------------------------------------------------
# 2) DIVIDIR UN PDF
# ---------------------------------------------------------------------------

@dataclass
class OpcionesDivision:
    modo: str = "rangos"                 # 'rangos' | 'cada_n' | 'paginas'
    rangos_texto: str = ""               # solo para modo 'rangos'
    cada_n: int = 5                      # solo para modo 'cada_n'
    anonimizar: bool = False
    eliminar_anotaciones: bool = False


@dataclass
class ResultadoDivision:
    pdfs_generados: list[Path] = field(default_factory=list)
    tamano_total: int = 0


# Un token válido es "5" o "3-7" (se admiten espacios alrededor).
_PATRON_RANGO = re.compile(r"^\s*(\d+)\s*(?:-\s*(\d+)\s*)?$")


def parsear_rangos(texto: str, total_paginas: int) -> list[tuple[int, int]]:
    """
    Convierte un texto como "1-3, 5, 8-10" en una lista de tuplas
    (inicio, fin) 1-based e inclusivas: [(1, 3), (5, 5), (8, 10)].

    Lanza ValueError con mensajes claros (en castellano) si el texto está
    vacío, contiene tokens no válidos o referencia páginas inexistentes.
    """
    if not texto or not texto.strip():
        raise ValueError(
            "Escribe los rangos de páginas, por ejemplo: 1-3, 5, 8-10"
        )

    rangos: list[tuple[int, int]] = []
    for token in texto.split(","):
        if not token.strip():
            continue
        m = _PATRON_RANGO.match(token)
        if not m:
            raise ValueError(
                f"Rango no válido: «{token.strip()}». "
                "Usa números o tramos como «2-5», separados por comas."
            )
        inicio = int(m.group(1))
        fin = int(m.group(2)) if m.group(2) else inicio

        if inicio < 1 or fin < 1:
            raise ValueError("Los números de página empiezan en 1.")
        if inicio > fin:
            raise ValueError(
                f"Rango invertido: «{token.strip()}» (el inicio es mayor que el fin)."
            )
        if fin > total_paginas:
            raise ValueError(
                f"El rango «{token.strip()}» supera el número de páginas "
                f"del documento ({total_paginas})."
            )
        rangos.append((inicio, fin))

    if not rangos:
        raise ValueError(
            "No se ha reconocido ningún rango. Ejemplo válido: 1-3, 5, 8-10"
        )
    return rangos


def _calcular_tramos(opciones: OpcionesDivision,
                     total_paginas: int) -> list[tuple[int, int]]:
    """Devuelve la lista de tramos (1-based, inclusivos) según el modo."""
    if opciones.modo == "rangos":
        return parsear_rangos(opciones.rangos_texto, total_paginas)

    if opciones.modo == "cada_n":
        n = int(opciones.cada_n)
        if n < 1:
            raise ValueError("El tamaño de bloque debe ser al menos 1 página.")
        return [
            (inicio, min(inicio + n - 1, total_paginas))
            for inicio in range(1, total_paginas + 1, n)
        ]

    if opciones.modo == "paginas":
        return [(p, p) for p in range(1, total_paginas + 1)]

    raise ValueError(f"Modo de división desconocido: {opciones.modo}")


def dividir_pdf(ruta_entrada: str | Path,
                carpeta_salida: str | Path,
                opciones: OpcionesDivision,
                progreso_callback=None) -> ResultadoDivision:
    """
    Divide `ruta_entrada` en varios PDFs dentro de `carpeta_salida`.
    Los archivos se nombran «{nombre}_pag_3.pdf» o «{nombre}_pags_4-7.pdf».
    """
    ruta_entrada = Path(ruta_entrada)
    carpeta_salida = Path(carpeta_salida)
    carpeta_salida.mkdir(parents=True, exist_ok=True)

    resultado = ResultadoDivision()

    doc = abrir_pdf(ruta_entrada)
    try:
        tramos = _calcular_tramos(opciones, len(doc))
        total = len(tramos)

        for i, (inicio, fin) in enumerate(tramos, start=1):
            if progreso_callback:
                progreso_callback(
                    f"Generando parte {i}/{total} (páginas {inicio}-{fin})",
                    i, total,
                )

            if inicio == fin:
                sufijo = f"{ruta_entrada.stem}_pag_{inicio}"
            else:
                sufijo = f"{ruta_entrada.stem}_pags_{inicio}-{fin}"
            ruta_destino = ruta_salida_unica(carpeta_salida, sufijo, ".pdf")

            parte = fitz.open()
            try:
                parte.insert_pdf(doc, from_page=inicio - 1, to_page=fin - 1)
                _anonimizar_si_procede(
                    parte, opciones.anonimizar, opciones.eliminar_anotaciones
                )
                parte.save(ruta_destino, **GUARDADO_COMPACTO)
            finally:
                parte.close()

            resultado.pdfs_generados.append(ruta_destino)
    finally:
        doc.close()

    resultado.tamano_total = sum(p.stat().st_size for p in resultado.pdfs_generados)
    return resultado


# ---------------------------------------------------------------------------
# 3) REORDENAR / ROTAR / ELIMINAR / EXTRAER PÁGINAS
# ---------------------------------------------------------------------------

@dataclass
class PaginaEditada:
    """
    Estado de una página dentro del editor de la GUI.

    indice_original: posición 0-based de la página en el PDF de origen.
    rotacion_extra:  grados añadidos por el usuario (0, 90, 180 o 270,
                     en sentido horario) sobre la rotación propia de la página.
    """
    indice_original: int
    rotacion_extra: int = 0


@dataclass
class OpcionesRecomposicion:
    anonimizar: bool = False
    eliminar_anotaciones: bool = False


@dataclass
class ResultadoRecomposicion:
    ruta_destino: Path
    num_paginas: int
    tamano_final: int


def recomponer_pdf(ruta_entrada: str | Path,
                   paginas: list[PaginaEditada],
                   ruta_salida: str | Path,
                   opciones: OpcionesRecomposicion | None = None,
                   progreso_callback=None) -> ResultadoRecomposicion:
    """
    Crea un PDF nuevo tomando de `ruta_entrada` las páginas indicadas en
    `paginas`, EN ESE ORDEN, y aplicando la rotación extra de cada una.

    - Reordenar  → pasar todas las páginas en el nuevo orden.
    - Eliminar   → simplemente no incluir esas páginas en la lista.
    - Extraer    → pasar solo el subconjunto seleccionado.

    El PDF original NO se modifica nunca.
    """
    opciones = opciones or OpcionesRecomposicion()
    ruta_entrada = Path(ruta_entrada)
    ruta_salida = Path(ruta_salida)

    if not paginas:
        raise ValueError("El PDF resultante debe conservar al menos una página.")

    doc = abrir_pdf(ruta_entrada)
    try:
        total_origen = len(doc)
        for p in paginas:
            if p.indice_original < 0 or p.indice_original >= total_origen:
                raise ValueError(
                    f"Página fuera de rango: {p.indice_original + 1} "
                    f"(el documento tiene {total_origen})."
                )

        nuevo = fitz.open()
        try:
            total = len(paginas)
            for i, p in enumerate(paginas, start=1):
                if progreso_callback:
                    progreso_callback(f"Componiendo página {i}/{total}", i, total)
                nuevo.insert_pdf(
                    doc,
                    from_page=p.indice_original,
                    to_page=p.indice_original,
                )
                if p.rotacion_extra % 360:
                    pagina_nueva = nuevo[len(nuevo) - 1]
                    pagina_nueva.set_rotation(
                        (pagina_nueva.rotation + p.rotacion_extra) % 360
                    )

            _anonimizar_si_procede(
                nuevo, opciones.anonimizar, opciones.eliminar_anotaciones
            )
            num_paginas = len(nuevo)
            nuevo.save(ruta_salida, **GUARDADO_COMPACTO)
        finally:
            nuevo.close()
    finally:
        doc.close()

    return ResultadoRecomposicion(
        ruta_destino=ruta_salida,
        num_paginas=num_paginas,
        tamano_final=ruta_salida.stat().st_size,
    )


# ---------------------------------------------------------------------------
# Miniaturas para la vista previa
# ---------------------------------------------------------------------------

def render_miniatura_png(doc: fitz.Document,
                         indice: int,
                         rotacion_extra: int = 0,
                         max_px: int = 340) -> bytes:
    """
    Renderiza la página `indice` (0-based) de un documento YA ABIERTO como
    PNG, con el lado mayor limitado a `max_px` píxeles, y aplicando la
    rotación extra elegida por el usuario (solo en la vista, sin tocar el
    documento).
    """
    pagina = doc[indice]
    rect = pagina.rect
    lado_mayor = max(rect.width, rect.height) or 1.0
    zoom = max_px / lado_mayor

    pix = pagina.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    png = pix.tobytes("png")

    if rotacion_extra % 360:
        # La rotación de PDF es horaria; PIL rota en sentido antihorario,
        # por eso el signo negativo.
        from PIL import Image
        img = Image.open(io.BytesIO(png))
        img = img.rotate(-(rotacion_extra % 360), expand=True)
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        png = buffer.getvalue()

    return png
