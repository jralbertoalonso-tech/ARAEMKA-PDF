"""
Punto de entrada de ARAEMKA PDF.

Se invoca tanto al ejecutar `python -m src.main` durante el desarrollo
como cuando PyInstaller empaqueta el .exe / .app (que termina llamando
a este mismo archivo como módulo principal).
"""

from __future__ import annotations

import sys


def main() -> None:
    # Importación diferida: si tkinterdnd2 no está disponible, mostramos
    # un mensaje claro al usuario en vez de un traceback opaco.
    try:
        from .gui import lanzar
    except ImportError:
        # Cuando se ejecuta como script suelto (sin paquete), volvemos a
        # intentar con import absoluto.
        from gui import lanzar  # type: ignore

    lanzar()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # En modo --windowed no hay consola; mostramos un messagebox.
        try:
            import tkinter.messagebox as mb
            mb.showerror("Error fatal", f"ARAEMKA PDF no pudo iniciarse:\n\n{e}")
        finally:
            sys.exit(1)
