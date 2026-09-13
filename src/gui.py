"""
Interfaz gráfica de ARAEMKA PDF.

Estructura:
    - Ventana principal (hereda de TkinterDnD.Tk para soporte drag-and-drop).
    - ttk.Notebook con cuatro pestañas:
        * "Comprimir PDFs"
        * "Imágenes a PDF"
        * "Organizar PDFs"  (unir, dividir y reordenar/rotar/extraer páginas)
        * "PDF a Word"      (conversión a .docx con el motor pdf2docx)
    - Cada pestaña tiene su propio panel con la lista de archivos, las
      opciones, el bloque de anonimización, la barra de progreso y los
      botones de acción.

Toda la operación pesada se ejecuta en un hilo de trabajo
(`threading.Thread`) y se comunica con la GUI a través de una `queue.Queue`
que se vacía periódicamente con `self.after()`.
"""

from __future__ import annotations

import queue
import sys
import threading
import tkinter as tk
import traceback
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import compresor, conversor, conversor_docx, organizador


# ---------------------------------------------------------------------------
# Soporte de drag-and-drop opcional
# ---------------------------------------------------------------------------
# tkinterdnd2 trae binarios de TkDnD compilados para Tcl/Tk 8.6. Si la
# instalación local de Python usa Tk 9.0 (por ejemplo, Python de Homebrew
# en macOS reciente), la carga del paquete tkdnd falla. En ese caso la
# aplicación sigue arrancando, pero sin drag-and-drop. En Windows con Tk 8.6
# (la situación normal del .exe empaquetado) el DnD funciona sin cambios.

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD  # type: ignore
    _BaseTk = TkinterDnD.Tk
    # No se crea una ventana temporal para probar TkDnD: en macOS, destruir
    # un primer intérprete Tcl/Tk y abrir otro inmediatamente puede provocar
    # un fallo nativo. La propia ventana principal carga TkDnD una sola vez.
    DND_DISPONIBLE = True
except Exception:
    DND_FILES = None  # type: ignore
    _BaseTk = tk.Tk
    DND_DISPONIBLE = False
from . import __version__
from .utilidades import (
    EXTENSIONES_IMAGEN,
    abrir_carpeta,
    es_imagen,
    es_pdf,
    formatear_tamano,
    parsear_rutas_dnd,
    porcentaje_reduccion,
    ruta_salida_unica,
)

# Versión corta para el título de la ventana ("1.1.0" → "1.1")
VERSION_CORTA = ".".join(__version__.split(".")[:2])


# ===========================================================================
# Ventana principal
# ===========================================================================

class AplicacionPDF(_BaseTk):
    """Ventana principal de la aplicación."""

    def __init__(self) -> None:
        super().__init__()

        self.title(f"ARAEMKA PDF {VERSION_CORTA} — Procesador local de PDFs")
        ancho, alto = 900, 800
        self.minsize(780, 680)

        # Abrir la ventana centrada horizontalmente y a 1/3 de altura
        try:
            x = max(0, (self.winfo_screenwidth() - ancho) // 2)
            y = max(0, (self.winfo_screenheight() - alto) // 3)
            self.geometry(f"{ancho}x{alto}+{x}+{y}")
        except tk.TclError:
            self.geometry(f"{ancho}x{alto}")

        # Estilo ligeramente más moderno
        estilo = ttk.Style(self)
        for tema in ("aqua", "vista", "clam"):
            try:
                estilo.theme_use(tema)
                break
            except tk.TclError:
                continue
        estilo.configure("TButton", padding=6)
        estilo.configure("Accion.TButton", padding=8)

        # Notebook con pestañas
        notebook = ttk.Notebook(self)
        notebook.pack(expand=True, fill="both", padx=10, pady=(10, 4))

        self.pestana_comprimir = PestanaComprimir(notebook)
        self.pestana_convertir = PestanaConvertir(notebook)
        self.pestana_organizar = PestanaOrganizar(notebook)
        self.pestana_pdf_a_word = PestanaPdfAWord(notebook)

        notebook.add(self.pestana_comprimir, text="  Comprimir PDFs  ")
        notebook.add(self.pestana_convertir, text="  Imágenes a PDF  ")
        notebook.add(self.pestana_organizar, text="  Organizar PDFs  ")
        notebook.add(self.pestana_pdf_a_word, text="  PDF a Word  ")

        # Barra de estado inferior
        self.var_estado = tk.StringVar(value="Listo.")
        barra = ttk.Label(
            self, textvariable=self.var_estado,
            anchor="w", relief="sunken", padding=(8, 4),
        )
        barra.pack(side="bottom", fill="x")

        # Las pestañas necesitan acceso a la barra de estado
        self.pestana_comprimir.set_var_estado(self.var_estado)
        self.pestana_convertir.set_var_estado(self.var_estado)
        self.pestana_organizar.set_var_estado(self.var_estado)
        self.pestana_pdf_a_word.set_var_estado(self.var_estado)


# ===========================================================================
# Componente reutilizable: lista de archivos con drag-and-drop
# ===========================================================================

class ListaArchivos(ttk.Frame):
    """
    Lista de rutas (Listbox + scrollbar) con soporte de drag-and-drop.
    Filtro de extensiones opcional.
    """

    def __init__(self, master, extensiones_validas: set[str] | None = None,
                 alto: int = 8, **kwargs):
        super().__init__(master, **kwargs)
        self.extensiones_validas = extensiones_validas

        self.lista = tk.Listbox(
            self, height=alto, selectmode="extended",
            activestyle="dotbox",
        )
        scroll = ttk.Scrollbar(self, orient="vertical", command=self.lista.yview)
        self.lista.configure(yscrollcommand=scroll.set)

        self.lista.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        # --- Drag and drop (opcional) ---
        if DND_DISPONIBLE:
            try:
                self.lista.drop_target_register(DND_FILES)
                self.lista.dnd_bind("<<Drop>>", self._on_drop)
            except Exception:
                # Si tkdnd existe pero el widget no acepta el registro,
                # la app sigue funcionando con los botones "Añadir…".
                pass

    # ---- API pública ----

    @staticmethod
    def _expandir_carpetas(rutas: list[str]) -> list[str]:
        """
        Si el usuario arrastra una carpeta, se sustituye por los archivos
        que contiene (sin entrar en subcarpetas). El filtro de extensiones
        de `agregar` se encarga después de quedarse solo con los válidos.
        """
        expandidas: list[str] = []
        for ruta in rutas:
            p = Path(ruta)
            try:
                if p.is_dir():
                    expandidas.extend(
                        str(hijo) for hijo in sorted(p.iterdir()) if hijo.is_file()
                    )
                else:
                    expandidas.append(str(p))
            except Exception:
                expandidas.append(str(p))
        return expandidas

    def agregar(self, rutas: list[str]) -> int:
        """Añade rutas válidas y devuelve cuántas se añadieron."""
        rutas = self._expandir_carpetas(rutas)
        existentes = set(self.obtener_todas())
        agregadas = 0
        for ruta in rutas:
            ruta = str(Path(ruta))
            if ruta in existentes:
                continue
            if not Path(ruta).is_file():
                continue
            if self.extensiones_validas is not None:
                if Path(ruta).suffix.lower() not in self.extensiones_validas:
                    continue
            self.lista.insert("end", ruta)
            existentes.add(ruta)
            agregadas += 1
        return agregadas

    def quitar_seleccionadas(self) -> None:
        for indice in reversed(self.lista.curselection()):
            self.lista.delete(indice)

    def vaciar(self) -> None:
        self.lista.delete(0, "end")

    def obtener_todas(self) -> list[str]:
        return list(self.lista.get(0, "end"))

    def obtener_seleccionadas(self) -> list[int]:
        return list(self.lista.curselection())

    def mover(self, delta: int) -> None:
        """Mueve los elementos seleccionados arriba (-1) o abajo (+1)."""
        seleccion = list(self.lista.curselection())
        if not seleccion:
            return
        # Si el bloque ya toca el borde, no hacemos nada: así una selección
        # múltiple nunca se "aplasta" contra el extremo perdiendo su orden.
        if delta < 0 and seleccion[0] == 0:
            return
        if delta > 0 and seleccion[-1] == self.lista.size() - 1:
            return
        if delta < 0:
            iterar = seleccion
        else:
            iterar = list(reversed(seleccion))
        for idx in iterar:
            destino = idx + delta
            if destino < 0 or destino >= self.lista.size():
                continue
            valor = self.lista.get(idx)
            self.lista.delete(idx)
            self.lista.insert(destino, valor)
            self.lista.selection_set(destino)

    def ordenar(self, clave) -> None:
        """Ordena por una función `clave(ruta) -> sortable`."""
        items = self.obtener_todas()
        items.sort(key=clave)
        self.vaciar()
        for it in items:
            self.lista.insert("end", it)

    # ---- Drag & drop ----

    def _on_drop(self, event) -> None:
        rutas = parsear_rutas_dnd(self, event.data)
        self.agregar(rutas)


# ===========================================================================
# Pestaña base con utilidades comunes (hilo, progreso, anonimización)
# ===========================================================================

class PestanaBase(ttk.Frame):
    """Funcionalidad común a todas las pestañas."""

    def __init__(self, master):
        super().__init__(master, padding=10)
        self._cola_eventos: queue.Queue = queue.Queue()
        self._hilo_actual: threading.Thread | None = None
        self.var_estado: tk.StringVar | None = None

        # Variables comunes de anonimización
        self.var_anonimizar = tk.BooleanVar(value=False)
        self.var_eliminar_anotaciones = tk.BooleanVar(value=False)

        # Carpeta de salida común
        self.var_carpeta_salida = tk.StringVar(value="")

        # Drenaje periódico de la cola de eventos del hilo
        self.after(100, self._drenar_cola)

    # ---- Conexión con la barra de estado ----

    def set_var_estado(self, var: tk.StringVar) -> None:
        self.var_estado = var

    def estado(self, texto: str) -> None:
        if self.var_estado is not None:
            self.var_estado.set(texto)

    # ---- Construcción de bloque "Anonimización" ----

    def construir_bloque_anonimizacion(self, parent) -> ttk.LabelFrame:
        marco = ttk.LabelFrame(parent, text="Anonimización", padding=10)

        chk = ttk.Checkbutton(
            marco,
            text="Eliminar metadatos del PDF (autor, software, fechas, XMP, EXIF de imágenes)",
            variable=self.var_anonimizar,
            command=self._toggle_subopciones_anonimizar,
        )
        chk.grid(row=0, column=0, sticky="w")

        self._chk_anotaciones = ttk.Checkbutton(
            marco,
            text="Eliminar también anotaciones y comentarios",
            variable=self.var_eliminar_anotaciones,
            state="disabled",
        )
        self._chk_anotaciones.grid(row=1, column=0, sticky="w", padx=(20, 0))

        return marco

    def _toggle_subopciones_anonimizar(self) -> None:
        estado = "normal" if self.var_anonimizar.get() else "disabled"
        try:
            self._chk_anotaciones.configure(state=estado)
            if estado == "disabled":
                self.var_eliminar_anotaciones.set(False)
        except AttributeError:
            pass

    # ---- Construcción del bloque "Carpeta de salida" ----

    def construir_bloque_carpeta_salida(self, parent) -> ttk.Frame:
        marco = ttk.Frame(parent)
        ttk.Label(marco, text="Carpeta de salida:").grid(row=0, column=0, sticky="w")
        entrada = ttk.Entry(marco, textvariable=self.var_carpeta_salida)
        entrada.grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(marco, text="Elegir…", command=self._elegir_carpeta_salida)\
            .grid(row=0, column=2)
        marco.columnconfigure(1, weight=1)
        return marco

    def _elegir_carpeta_salida(self) -> None:
        carpeta = filedialog.askdirectory(title="Carpeta de salida")
        if carpeta:
            self.var_carpeta_salida.set(carpeta)

    # ---- Hilo de trabajo + cola de eventos ----

    def _lanzar_hilo(self, objetivo, *args) -> None:
        if self._hilo_actual and self._hilo_actual.is_alive():
            messagebox.showwarning(
                "Procesamiento en curso",
                "Espera a que termine la operación actual.",
            )
            return
        self._hilo_actual = threading.Thread(
            target=self._ejecutar_seguro,
            args=(objetivo, *args),
            daemon=True,
        )
        self._hilo_actual.start()

    def _ejecutar_seguro(self, objetivo, *args) -> None:
        """Ejecuta `objetivo(*args)` capturando excepciones."""
        try:
            objetivo(*args)
        except Exception as e:
            self._cola_eventos.put(("error", traceback.format_exc(), str(e)))

    def _drenar_cola(self) -> None:
        try:
            while True:
                evento = self._cola_eventos.get_nowait()
                self._procesar_evento(evento)
        except queue.Empty:
            pass
        self.after(100, self._drenar_cola)

    def _procesar_evento(self, evento) -> None:
        """Cada subclase sobreescribe esto para reaccionar a sus eventos."""
        tipo = evento[0]
        if tipo == "error":
            tb, msg = evento[1], evento[2]
            print(tb, file=sys.stderr)
            messagebox.showerror("Error", f"Se ha producido un error:\n\n{msg}")


# ===========================================================================
# Pestaña 1: Comprimir PDFs
# ===========================================================================

class PestanaComprimir(PestanaBase):

    def __init__(self, master):
        super().__init__(master)

        # ---- Controles ----
        self.var_nivel = tk.StringVar(value="equilibrada")
        self.var_dpi = tk.StringVar(value="150")
        self.var_grises = tk.BooleanVar(value=False)
        self.var_progreso = tk.DoubleVar(value=0.0)

        self._construir_layout()

    # ----------------------------------------------------------------------
    # UI
    # ----------------------------------------------------------------------

    def _construir_layout(self) -> None:
        pista = (
            "Arrastra PDFs aquí o pulsa «Añadir PDFs». El procesamiento es 100% local."
            if DND_DISPONIBLE
            else "Pulsa «Añadir PDFs» para seleccionar archivos. (Drag-and-drop "
                 "no disponible en esta instalación de Tk.)"
        )
        ttk.Label(self, text=pista, foreground="#555").pack(anchor="w", pady=(0, 6))

        # Lista de archivos
        self.lista = ListaArchivos(self, extensiones_validas={".pdf"}, alto=7)
        self.lista.pack(fill="both", expand=True)

        # Botones de gestión de la lista
        botones = ttk.Frame(self)
        botones.pack(fill="x", pady=(6, 8))
        ttk.Button(botones, text="Añadir PDFs…", command=self._anadir_pdfs)\
            .pack(side="left")
        ttk.Button(botones, text="Quitar seleccionados",
                   command=self.lista.quitar_seleccionadas).pack(side="left", padx=4)
        ttk.Button(botones, text="Vaciar lista",
                   command=self.lista.vaciar).pack(side="left")

        # ---- Opciones de compresión ----
        opciones = ttk.LabelFrame(self, text="Opciones de compresión", padding=10)
        opciones.pack(fill="x", pady=4)

        ttk.Label(opciones, text="Nivel:").grid(row=0, column=0, sticky="w")
        marco_nivel = ttk.Frame(opciones)
        marco_nivel.grid(row=0, column=1, sticky="w", padx=(8, 0))
        for valor, texto in (
            ("maxima", "Máxima (menor tamaño)"),
            ("equilibrada", "Equilibrada"),
            ("alta", "Alta calidad"),
        ):
            ttk.Radiobutton(
                marco_nivel, text=texto, value=valor, variable=self.var_nivel,
            ).pack(side="left", padx=(0, 12))

        ttk.Label(opciones, text="DPI imágenes:").grid(row=1, column=0, sticky="w", pady=(8, 0))
        combo_dpi = ttk.Combobox(
            opciones, textvariable=self.var_dpi,
            values=("72", "150", "300"), width=6, state="readonly",
        )
        combo_dpi.grid(row=1, column=1, sticky="w", padx=(8, 0), pady=(8, 0))

        ttk.Checkbutton(
            opciones, text="Convertir imágenes a escala de grises",
            variable=self.var_grises,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))

        # ---- Anonimización ----
        self.construir_bloque_anonimizacion(self).pack(fill="x", pady=4)

        # ---- Carpeta de salida ----
        self.construir_bloque_carpeta_salida(self).pack(fill="x", pady=4)

        # ---- Progreso ----
        marco_progreso = ttk.Frame(self)
        marco_progreso.pack(fill="x", pady=(8, 0))
        self.barra = ttk.Progressbar(
            marco_progreso, variable=self.var_progreso,
            maximum=100.0, mode="determinate",
        )
        self.barra.pack(fill="x")
        self.lbl_progreso = ttk.Label(marco_progreso, text="", foreground="#444")
        self.lbl_progreso.pack(anchor="w", pady=(2, 0))

        # ---- Botón principal ----
        ttk.Button(
            self, text="Comprimir PDFs", style="Accion.TButton",
            command=self._iniciar_compresion,
        ).pack(pady=10)

    # ----------------------------------------------------------------------
    # Acciones
    # ----------------------------------------------------------------------

    def _anadir_pdfs(self) -> None:
        rutas = filedialog.askopenfilenames(
            title="Selecciona PDFs",
            filetypes=[("Archivos PDF", "*.pdf")],
        )
        if rutas:
            self.lista.agregar(list(rutas))

    def _iniciar_compresion(self) -> None:
        rutas = self.lista.obtener_todas()
        if not rutas:
            messagebox.showinfo("Sin archivos", "Añade al menos un PDF a la lista.")
            return

        carpeta_salida = self.var_carpeta_salida.get().strip()
        if not carpeta_salida:
            carpeta_salida = filedialog.askdirectory(title="Carpeta de salida")
            if not carpeta_salida:
                return
            self.var_carpeta_salida.set(carpeta_salida)

        try:
            dpi = int(self.var_dpi.get())
        except ValueError:
            dpi = 150

        opciones = compresor.OpcionesCompresion(
            nivel=self.var_nivel.get(),
            dpi=dpi,
            escala_grises=self.var_grises.get(),
            anonimizar=self.var_anonimizar.get(),
            eliminar_anotaciones=self.var_eliminar_anotaciones.get(),
        )

        self.var_progreso.set(0.0)
        self.lbl_progreso.configure(text="Iniciando…")
        self.estado("Comprimiendo PDFs…")
        self._lanzar_hilo(self._trabajo_comprimir, rutas, Path(carpeta_salida), opciones)

    # ----------------------------------------------------------------------
    # Trabajo en segundo plano
    # ----------------------------------------------------------------------

    def _trabajo_comprimir(self, rutas: list[str], carpeta_salida: Path,
                           opciones: compresor.OpcionesCompresion) -> None:
        carpeta_salida.mkdir(parents=True, exist_ok=True)

        total = len(rutas)
        resultados: list[compresor.ResultadoCompresion] = []
        errores: list[tuple[str, str]] = []

        for i, ruta in enumerate(rutas, start=1):
            ruta_origen = Path(ruta)
            self._cola_eventos.put((
                "progreso",
                (i - 1) * 100 / total,
                f"Procesando {i}/{total}: {ruta_origen.name}",
            ))
            ruta_destino = ruta_salida_unica(
                carpeta_salida, f"{ruta_origen.stem}_comprimido", ".pdf"
            )
            try:
                resultado = compresor.comprimir_pdf(ruta_origen, ruta_destino, opciones)
                resultados.append(resultado)
            except Exception as e:
                errores.append((ruta_origen.name, str(e)))

        self._cola_eventos.put(("progreso", 100.0, "Completado"))
        self._cola_eventos.put(("fin", resultados, errores))

    # ----------------------------------------------------------------------
    # Manejo de eventos del hilo
    # ----------------------------------------------------------------------

    def _procesar_evento(self, evento) -> None:
        tipo = evento[0]
        if tipo == "progreso":
            _, valor, texto = evento
            self.var_progreso.set(valor)
            self.lbl_progreso.configure(text=texto)
        elif tipo == "fin":
            _, resultados, errores = evento
            self._mostrar_resumen(resultados, errores)
            self.estado("Listo.")
        else:
            super()._procesar_evento(evento)

    def _mostrar_resumen(self, resultados, errores) -> None:
        if not resultados and not errores:
            return

        ahorrado_total = sum(r.tamano_original - r.tamano_final for r in resultados)
        original_total = sum(r.tamano_original for r in resultados)
        final_total = sum(r.tamano_final for r in resultados)

        lineas = [f"PDFs procesados: {len(resultados)}"]
        if resultados:
            lineas.append(f"Tamaño original: {formatear_tamano(original_total)}")
            lineas.append(f"Tamaño final:    {formatear_tamano(final_total)}")
            lineas.append(
                f"Ahorrado:        {formatear_tamano(ahorrado_total)}  "
                f"({porcentaje_reduccion(original_total, final_total):.1f}%)"
            )
            lineas.append("")
            lineas.append("Detalle:")
            for r in resultados:
                lineas.append(
                    f" • {r.ruta_origen.name}: "
                    f"{formatear_tamano(r.tamano_original)} → "
                    f"{formatear_tamano(r.tamano_final)} "
                    f"({r.reduccion_pct:+.1f}%)"
                )
        if errores:
            lineas.append("")
            lineas.append("Errores:")
            for nombre, err in errores:
                lineas.append(f" • {nombre}: {err}")

        mensaje = "\n".join(lineas)
        if resultados:
            if messagebox.askyesno(
                "Compresión finalizada",
                mensaje + "\n\n¿Quieres abrir la carpeta de salida?",
            ):
                abrir_carpeta(resultados[0].ruta_destino.parent)
        else:
            messagebox.showinfo("Compresión finalizada", mensaje)


# ===========================================================================
# Pestaña 2: Imágenes a PDF
# ===========================================================================

class PestanaConvertir(PestanaBase):

    def __init__(self, master):
        super().__init__(master)

        self.var_modo_multipagina = tk.BooleanVar(value=True)
        self.var_tamano = tk.StringVar(value="A4")
        self.var_orientacion = tk.StringVar(value="auto")
        self.var_margen = tk.StringVar(value="10")
        self.var_comprimir = tk.BooleanVar(value=False)
        self.var_progreso = tk.DoubleVar(value=0.0)

        self._construir_layout()

    # ----------------------------------------------------------------------
    # UI
    # ----------------------------------------------------------------------

    def _construir_layout(self) -> None:
        pista = (
            "Arrastra imágenes aquí o pulsa «Añadir imágenes». "
            "Reordénalas con los botones o por nombre/fecha."
            if DND_DISPONIBLE
            else "Pulsa «Añadir imágenes» para seleccionarlas. Reordénalas con los "
                 "botones de la derecha. (Drag-and-drop no disponible en esta Tk.)"
        )
        ttk.Label(self, text=pista, foreground="#555").pack(anchor="w", pady=(0, 6))

        # Lista de imágenes
        marco_lista = ttk.Frame(self)
        marco_lista.pack(fill="both", expand=True)

        self.lista = ListaArchivos(
            marco_lista, extensiones_validas=EXTENSIONES_IMAGEN, alto=8,
        )
        self.lista.pack(side="left", fill="both", expand=True)

        # Botonera vertical para reordenar
        botones_orden = ttk.Frame(marco_lista)
        botones_orden.pack(side="left", fill="y", padx=(6, 0))
        ttk.Button(botones_orden, text="↑ Subir", width=14,
                   command=lambda: self.lista.mover(-1)).pack(pady=2)
        ttk.Button(botones_orden, text="↓ Bajar", width=14,
                   command=lambda: self.lista.mover(+1)).pack(pady=2)
        ttk.Separator(botones_orden, orient="horizontal").pack(fill="x", pady=6)
        ttk.Button(botones_orden, text="Ordenar A-Z", width=14,
                   command=self._ordenar_nombre).pack(pady=2)
        ttk.Button(botones_orden, text="Ordenar fecha", width=14,
                   command=self._ordenar_fecha).pack(pady=2)

        # Botones de gestión de la lista
        botones = ttk.Frame(self)
        botones.pack(fill="x", pady=(6, 8))
        ttk.Button(botones, text="Añadir imágenes…",
                   command=self._anadir_imagenes).pack(side="left")
        ttk.Button(botones, text="Quitar seleccionadas",
                   command=self.lista.quitar_seleccionadas).pack(side="left", padx=4)
        ttk.Button(botones, text="Vaciar lista",
                   command=self.lista.vaciar).pack(side="left")

        # ---- Opciones de salida ----
        opciones = ttk.LabelFrame(self, text="Opciones de salida", padding=10)
        opciones.pack(fill="x", pady=4)

        ttk.Label(opciones, text="Modo:").grid(row=0, column=0, sticky="w")
        marco_modo = ttk.Frame(opciones)
        marco_modo.grid(row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Radiobutton(
            marco_modo, text="Un único PDF multipágina",
            variable=self.var_modo_multipagina, value=True,
        ).pack(side="left", padx=(0, 12))
        ttk.Radiobutton(
            marco_modo, text="Un PDF por imagen",
            variable=self.var_modo_multipagina, value=False,
        ).pack(side="left")

        ttk.Label(opciones, text="Tamaño página:").grid(row=1, column=0, sticky="w", pady=(8, 0))
        combo_tam = ttk.Combobox(
            opciones, textvariable=self.var_tamano,
            values=("A4", "carta", "ajustar"),
            width=10, state="readonly",
        )
        combo_tam.grid(row=1, column=1, sticky="w", padx=(8, 0), pady=(8, 0))

        ttk.Label(opciones, text="Orientación:").grid(row=2, column=0, sticky="w", pady=(8, 0))
        combo_ori = ttk.Combobox(
            opciones, textvariable=self.var_orientacion,
            values=("auto", "vertical", "horizontal"),
            width=12, state="readonly",
        )
        combo_ori.grid(row=2, column=1, sticky="w", padx=(8, 0), pady=(8, 0))

        ttk.Label(opciones, text="Margen (mm):").grid(row=3, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(opciones, textvariable=self.var_margen, width=6)\
            .grid(row=3, column=1, sticky="w", padx=(8, 0), pady=(8, 0))

        ttk.Checkbutton(
            opciones,
            text="Comprimir automáticamente el PDF resultante",
            variable=self.var_comprimir,
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(10, 0))

        # ---- Anonimización ----
        self.construir_bloque_anonimizacion(self).pack(fill="x", pady=4)

        # ---- Carpeta de salida ----
        self.construir_bloque_carpeta_salida(self).pack(fill="x", pady=4)

        # ---- Progreso ----
        marco_progreso = ttk.Frame(self)
        marco_progreso.pack(fill="x", pady=(8, 0))
        self.barra = ttk.Progressbar(
            marco_progreso, variable=self.var_progreso,
            maximum=100.0, mode="determinate",
        )
        self.barra.pack(fill="x")
        self.lbl_progreso = ttk.Label(marco_progreso, text="", foreground="#444")
        self.lbl_progreso.pack(anchor="w", pady=(2, 0))

        # ---- Botón principal ----
        ttk.Button(
            self, text="Convertir a PDF", style="Accion.TButton",
            command=self._iniciar_conversion,
        ).pack(pady=10)

    # ----------------------------------------------------------------------
    # Acciones
    # ----------------------------------------------------------------------

    def _anadir_imagenes(self) -> None:
        rutas = filedialog.askopenfilenames(
            title="Selecciona imágenes",
            filetypes=[
                ("Imágenes",
                 "*.jpg *.jpeg *.png *.tif *.tiff *.bmp *.webp"),
                ("Todos los archivos", "*.*"),
            ],
        )
        if rutas:
            self.lista.agregar(list(rutas))

    def _ordenar_nombre(self) -> None:
        self.lista.ordenar(lambda r: Path(r).name.lower())

    def _ordenar_fecha(self) -> None:
        def clave(r):
            try:
                return Path(r).stat().st_mtime
            except Exception:
                return 0
        self.lista.ordenar(clave)

    def _iniciar_conversion(self) -> None:
        rutas = self.lista.obtener_todas()
        if not rutas:
            messagebox.showinfo("Sin archivos", "Añade al menos una imagen a la lista.")
            return

        carpeta_salida = self.var_carpeta_salida.get().strip()
        if not carpeta_salida:
            carpeta_salida = filedialog.askdirectory(title="Carpeta de salida")
            if not carpeta_salida:
                return
            self.var_carpeta_salida.set(carpeta_salida)

        try:
            margen = float(self.var_margen.get().replace(",", "."))
        except ValueError:
            margen = 10.0

        opciones = conversor.OpcionesConversion(
            multipagina=self.var_modo_multipagina.get(),
            tamano_pagina=self.var_tamano.get(),
            orientacion=self.var_orientacion.get(),
            margen_mm=margen,
            anonimizar=self.var_anonimizar.get(),
            eliminar_anotaciones=self.var_eliminar_anotaciones.get(),
            comprimir_resultado=self.var_comprimir.get(),
        )

        nombre_combinado = (
            f"imagenes_combinadas_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
        )

        self.var_progreso.set(0.0)
        self.lbl_progreso.configure(text="Iniciando…")
        self.estado("Convirtiendo imágenes a PDF…")
        self._lanzar_hilo(
            self._trabajo_convertir,
            rutas, Path(carpeta_salida), opciones, nombre_combinado,
        )

    # ----------------------------------------------------------------------
    # Trabajo en segundo plano
    # ----------------------------------------------------------------------

    def _trabajo_convertir(self, rutas, carpeta_salida, opciones, nombre_combinado):
        total = len(rutas)

        def cb(texto, indice, total_):
            valor = (indice / max(total_, 1)) * 100
            self._cola_eventos.put(("progreso", valor, texto))

        # Filtrar a sólo imágenes válidas, por si la lista tuviera ruidos
        rutas = [r for r in rutas if es_imagen(r)]

        cb("Generando PDF(s)…", 0, total)
        resultado = conversor.convertir(
            rutas, carpeta_salida, opciones,
            nombre_pdf_combinado=nombre_combinado,
            progreso_callback=cb,
        )

        self._cola_eventos.put(("progreso", 100.0, "Completado"))
        self._cola_eventos.put(("fin", resultado))

    # ----------------------------------------------------------------------
    # Manejo de eventos del hilo
    # ----------------------------------------------------------------------

    def _procesar_evento(self, evento) -> None:
        tipo = evento[0]
        if tipo == "progreso":
            _, valor, texto = evento
            self.var_progreso.set(valor)
            self.lbl_progreso.configure(text=texto)
        elif tipo == "fin":
            _, resultado = evento
            self._mostrar_resumen(resultado)
            self.estado("Listo.")
        else:
            super()._procesar_evento(evento)

    def _mostrar_resumen(self, resultado: conversor.ResultadoConversion) -> None:
        if not resultado.pdfs_generados:
            messagebox.showwarning(
                "Sin resultados",
                "No se generó ningún PDF. Comprueba el formato de las imágenes.",
            )
            return

        lineas = [
            f"PDFs generados: {len(resultado.pdfs_generados)}",
            f"Tamaño total:   {formatear_tamano(resultado.tamano_total)}",
            "",
            "Archivos:",
        ]
        for p in resultado.pdfs_generados:
            lineas.append(f" • {p.name}  ({formatear_tamano(p.stat().st_size)})")

        if messagebox.askyesno(
            "Conversión finalizada",
            "\n".join(lineas) + "\n\n¿Quieres abrir la carpeta de salida?",
        ):
            abrir_carpeta(resultado.pdfs_generados[0].parent)


# ===========================================================================
# Componente reutilizable: selector de un único PDF (con drag-and-drop)
# ===========================================================================

class SelectorPDF(ttk.Frame):
    """Fila «PDF: [ruta] [Elegir…]» con soporte de arrastrar y soltar."""

    def __init__(self, master, al_cambiar=None, **kwargs):
        super().__init__(master, **kwargs)
        self.al_cambiar = al_cambiar          # callback(ruta: str)
        self.var_ruta = tk.StringVar(value="")

        ttk.Label(self, text="PDF:").grid(row=0, column=0, sticky="w")
        self._entrada = ttk.Entry(
            self, textvariable=self.var_ruta, state="readonly",
        )
        self._entrada.grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(self, text="Elegir PDF…", command=self._elegir)\
            .grid(row=0, column=2)
        self.columnconfigure(1, weight=1)

        if DND_DISPONIBLE:
            for widget in (self, self._entrada):
                try:
                    widget.drop_target_register(DND_FILES)
                    widget.dnd_bind("<<Drop>>", self._on_drop)
                except Exception:
                    pass

    def _elegir(self) -> None:
        ruta = filedialog.askopenfilename(
            title="Selecciona un PDF",
            filetypes=[("Archivos PDF", "*.pdf")],
        )
        if ruta:
            self._establecer(ruta)

    def _on_drop(self, event) -> None:
        rutas = [r for r in parsear_rutas_dnd(self, event.data) if es_pdf(r)]
        if rutas:
            self._establecer(rutas[0])

    def _establecer(self, ruta: str) -> None:
        ruta = str(Path(ruta))
        self.var_ruta.set(ruta)
        if self.al_cambiar:
            self.al_cambiar(ruta)

    def obtener(self) -> str:
        return self.var_ruta.get().strip()


# ===========================================================================
# Pestaña 3: Organizar PDFs (contenedor con tres sub-apartados)
# ===========================================================================

class PestanaOrganizar(ttk.Frame):
    """
    Pestaña «Organizar PDFs». Contiene un sub-notebook con:
        * Unir            — juntar varios PDFs en uno.
        * Dividir         — trocear un PDF (rangos / cada N / página a página).
        * Reordenar       — reordenar, rotar, eliminar y extraer páginas,
                            con vista previa de la página seleccionada.
    """

    def __init__(self, master):
        super().__init__(master, padding=(2, 2))

        sub = ttk.Notebook(self)
        sub.pack(expand=True, fill="both")

        self.pestana_unir = PestanaUnir(sub)
        self.pestana_dividir = PestanaDividir(sub)
        self.pestana_reordenar = PestanaReordenar(sub)

        sub.add(self.pestana_unir, text="  Unir PDFs  ")
        sub.add(self.pestana_dividir, text="  Dividir PDF  ")
        sub.add(self.pestana_reordenar, text="  Reordenar páginas  ")

    def set_var_estado(self, var: tk.StringVar) -> None:
        for pestana in (self.pestana_unir, self.pestana_dividir,
                        self.pestana_reordenar):
            pestana.set_var_estado(var)


# ---------------------------------------------------------------------------
# 3.a) Unir varios PDFs en uno
# ---------------------------------------------------------------------------

class PestanaUnir(PestanaBase):

    def __init__(self, master):
        super().__init__(master)

        self.var_nombre_salida = tk.StringVar(value="PDF_unido")
        self.var_comprimir = tk.BooleanVar(value=False)
        self.var_progreso = tk.DoubleVar(value=0.0)

        self._construir_layout()

    def _construir_layout(self) -> None:
        pista = (
            "Arrastra PDFs aquí o pulsa «Añadir PDFs». Se unirán en el orden "
            "de la lista (reordénalos con ↑/↓)."
            if DND_DISPONIBLE
            else "Pulsa «Añadir PDFs» y ordénalos con ↑/↓: se unirán en el "
                 "orden de la lista. (Drag-and-drop no disponible en esta Tk.)"
        )
        ttk.Label(self, text=pista, foreground="#555").pack(anchor="w", pady=(0, 6))

        # Lista + botonera de orden (mismo patrón que "Imágenes a PDF")
        marco_lista = ttk.Frame(self)
        marco_lista.pack(fill="both", expand=True)

        self.lista = ListaArchivos(
            marco_lista, extensiones_validas={".pdf"}, alto=8,
        )
        self.lista.pack(side="left", fill="both", expand=True)

        botones_orden = ttk.Frame(marco_lista)
        botones_orden.pack(side="left", fill="y", padx=(6, 0))
        ttk.Button(botones_orden, text="↑ Subir", width=14,
                   command=lambda: self.lista.mover(-1)).pack(pady=2)
        ttk.Button(botones_orden, text="↓ Bajar", width=14,
                   command=lambda: self.lista.mover(+1)).pack(pady=2)
        ttk.Separator(botones_orden, orient="horizontal").pack(fill="x", pady=6)
        ttk.Button(botones_orden, text="Ordenar A-Z", width=14,
                   command=lambda: self.lista.ordenar(
                       lambda r: Path(r).name.lower())).pack(pady=2)
        ttk.Button(botones_orden, text="Ordenar fecha", width=14,
                   command=self._ordenar_fecha).pack(pady=2)

        botones = ttk.Frame(self)
        botones.pack(fill="x", pady=(6, 8))
        ttk.Button(botones, text="Añadir PDFs…", command=self._anadir_pdfs)\
            .pack(side="left")
        ttk.Button(botones, text="Quitar seleccionados",
                   command=self.lista.quitar_seleccionadas).pack(side="left", padx=4)
        ttk.Button(botones, text="Vaciar lista",
                   command=self.lista.vaciar).pack(side="left")

        # ---- Opciones ----
        opciones = ttk.LabelFrame(self, text="Opciones de unión", padding=10)
        opciones.pack(fill="x", pady=4)

        ttk.Label(opciones, text="Nombre del PDF resultante:")\
            .grid(row=0, column=0, sticky="w")
        ttk.Entry(opciones, textvariable=self.var_nombre_salida, width=32)\
            .grid(row=0, column=1, sticky="w", padx=(8, 0))

        ttk.Checkbutton(
            opciones,
            text="Comprimir automáticamente el PDF resultante",
            variable=self.var_comprimir,
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))

        # ---- Anonimización / carpeta / progreso / botón ----
        self.construir_bloque_anonimizacion(self).pack(fill="x", pady=4)
        self.construir_bloque_carpeta_salida(self).pack(fill="x", pady=4)

        marco_progreso = ttk.Frame(self)
        marco_progreso.pack(fill="x", pady=(8, 0))
        self.barra = ttk.Progressbar(
            marco_progreso, variable=self.var_progreso,
            maximum=100.0, mode="determinate",
        )
        self.barra.pack(fill="x")
        self.lbl_progreso = ttk.Label(marco_progreso, text="", foreground="#444")
        self.lbl_progreso.pack(anchor="w", pady=(2, 0))

        ttk.Button(
            self, text="Unir PDFs", style="Accion.TButton",
            command=self._iniciar_union,
        ).pack(pady=10)

    # ---- Acciones ----

    def _anadir_pdfs(self) -> None:
        rutas = filedialog.askopenfilenames(
            title="Selecciona PDFs",
            filetypes=[("Archivos PDF", "*.pdf")],
        )
        if rutas:
            self.lista.agregar(list(rutas))

    def _ordenar_fecha(self) -> None:
        def clave(r):
            try:
                return Path(r).stat().st_mtime
            except Exception:
                return 0
        self.lista.ordenar(clave)

    def _iniciar_union(self) -> None:
        rutas = self.lista.obtener_todas()
        if len(rutas) < 2:
            messagebox.showinfo(
                "Faltan archivos",
                "Añade al menos dos PDFs a la lista para unirlos.",
            )
            return

        carpeta_salida = self.var_carpeta_salida.get().strip()
        if not carpeta_salida:
            carpeta_salida = filedialog.askdirectory(title="Carpeta de salida")
            if not carpeta_salida:
                return
            self.var_carpeta_salida.set(carpeta_salida)

        nombre = self.var_nombre_salida.get().strip() or "PDF_unido"
        if nombre.lower().endswith(".pdf"):
            nombre = nombre[:-4]

        opciones = organizador.OpcionesUnion(
            anonimizar=self.var_anonimizar.get(),
            eliminar_anotaciones=self.var_eliminar_anotaciones.get(),
            comprimir_resultado=self.var_comprimir.get(),
        )

        self.var_progreso.set(0.0)
        self.lbl_progreso.configure(text="Iniciando…")
        self.estado("Uniendo PDFs…")
        self._lanzar_hilo(
            self._trabajo_unir, rutas, Path(carpeta_salida), nombre, opciones,
        )

    def _trabajo_unir(self, rutas, carpeta_salida: Path, nombre: str,
                      opciones: organizador.OpcionesUnion) -> None:
        carpeta_salida.mkdir(parents=True, exist_ok=True)
        ruta_destino = ruta_salida_unica(carpeta_salida, nombre, ".pdf")

        def cb(texto, indice, total):
            self._cola_eventos.put(
                ("progreso", indice * 100 / max(total, 1), texto)
            )

        resultado = organizador.unir_pdfs(
            rutas, ruta_destino, opciones, progreso_callback=cb,
        )
        self._cola_eventos.put(("progreso", 100.0, "Completado"))
        self._cola_eventos.put(("fin", resultado))

    def _procesar_evento(self, evento) -> None:
        tipo = evento[0]
        if tipo == "progreso":
            _, valor, texto = evento
            self.var_progreso.set(valor)
            self.lbl_progreso.configure(text=texto)
        elif tipo == "fin":
            _, resultado = evento
            if messagebox.askyesno(
                "Unión finalizada",
                f"PDFs unidos: {resultado.num_archivos}\n"
                f"Páginas totales: {resultado.num_paginas}\n"
                f"Tamaño final: {formatear_tamano(resultado.tamano_final)}\n\n"
                f"Guardado como:\n{resultado.ruta_destino}\n\n"
                "¿Quieres abrir la carpeta de salida?",
            ):
                abrir_carpeta(resultado.ruta_destino.parent)
            self.estado("Listo.")
        else:
            super()._procesar_evento(evento)


# ---------------------------------------------------------------------------
# 3.b) Dividir un PDF
# ---------------------------------------------------------------------------

class PestanaDividir(PestanaBase):

    def __init__(self, master):
        super().__init__(master)

        self.var_modo = tk.StringVar(value="rangos")
        self.var_rangos = tk.StringVar(value="")
        self.var_cada_n = tk.StringVar(value="5")
        self.var_subcarpeta = tk.BooleanVar(value=True)
        self.var_info_pdf = tk.StringVar(value="Ningún PDF seleccionado.")
        self.var_progreso = tk.DoubleVar(value=0.0)

        self._num_paginas: int | None = None
        self._construir_layout()

    def _construir_layout(self) -> None:
        pista = (
            "Elige (o arrastra) el PDF a dividir y la modalidad de división."
            if DND_DISPONIBLE
            else "Elige el PDF a dividir y la modalidad de división."
        )
        ttk.Label(self, text=pista, foreground="#555").pack(anchor="w", pady=(0, 6))

        self.selector = SelectorPDF(self, al_cambiar=self._pdf_cambiado)
        self.selector.pack(fill="x")

        ttk.Label(self, textvariable=self.var_info_pdf, foreground="#555")\
            .pack(anchor="w", pady=(4, 6))

        # ---- Modalidades ----
        opciones = ttk.LabelFrame(self, text="Modo de división", padding=10)
        opciones.pack(fill="x", pady=4)

        ttk.Radiobutton(
            opciones, text="Por rangos personalizados:",
            value="rangos", variable=self.var_modo,
            command=self._actualizar_estado_modo,
        ).grid(row=0, column=0, sticky="w")
        self._entrada_rangos = ttk.Entry(
            opciones, textvariable=self.var_rangos, width=28,
        )
        self._entrada_rangos.grid(row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Label(
            opciones, text="ej.: 1-3, 5, 8-10  →  un PDF por cada tramo",
            foreground="#777",
        ).grid(row=1, column=1, sticky="w", padx=(8, 0))

        ttk.Radiobutton(
            opciones, text="Cada N páginas:",
            value="cada_n", variable=self.var_modo,
            command=self._actualizar_estado_modo,
        ).grid(row=2, column=0, sticky="w", pady=(8, 0))
        self._spin_cada_n = ttk.Spinbox(
            opciones, from_=1, to=9999, textvariable=self.var_cada_n, width=6,
        )
        self._spin_cada_n.grid(row=2, column=1, sticky="w", padx=(8, 0), pady=(8, 0))

        ttk.Radiobutton(
            opciones, text="Página a página (un PDF por cada página)",
            value="paginas", variable=self.var_modo,
            command=self._actualizar_estado_modo,
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))

        ttk.Checkbutton(
            opciones,
            text="Guardar las partes en una subcarpeta propia («nombre_dividido»)",
            variable=self.var_subcarpeta,
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(10, 0))

        self._actualizar_estado_modo()

        # ---- Anonimización / carpeta / progreso / botón ----
        self.construir_bloque_anonimizacion(self).pack(fill="x", pady=4)
        self.construir_bloque_carpeta_salida(self).pack(fill="x", pady=4)

        marco_progreso = ttk.Frame(self)
        marco_progreso.pack(fill="x", pady=(8, 0))
        self.barra = ttk.Progressbar(
            marco_progreso, variable=self.var_progreso,
            maximum=100.0, mode="determinate",
        )
        self.barra.pack(fill="x")
        self.lbl_progreso = ttk.Label(marco_progreso, text="", foreground="#444")
        self.lbl_progreso.pack(anchor="w", pady=(2, 0))

        ttk.Button(
            self, text="Dividir PDF", style="Accion.TButton",
            command=self._iniciar_division,
        ).pack(pady=10)

    # ---- Acciones ----

    def _pdf_cambiado(self, ruta: str) -> None:
        try:
            self._num_paginas = organizador.numero_paginas(ruta)
            self.var_info_pdf.set(
                f"«{Path(ruta).name}» — {self._num_paginas} páginas."
            )
        except ValueError as e:
            self._num_paginas = None
            self.var_info_pdf.set("Ningún PDF seleccionado.")
            messagebox.showerror("PDF no válido", str(e))

    def _actualizar_estado_modo(self) -> None:
        modo = self.var_modo.get()
        self._entrada_rangos.configure(
            state="normal" if modo == "rangos" else "disabled"
        )
        self._spin_cada_n.configure(
            state="normal" if modo == "cada_n" else "disabled"
        )

    def _iniciar_division(self) -> None:
        ruta = self.selector.obtener()
        if not ruta:
            messagebox.showinfo("Sin archivo", "Elige primero el PDF a dividir.")
            return
        if self._num_paginas is None:
            self._pdf_cambiado(ruta)
            if self._num_paginas is None:
                return

        modo = self.var_modo.get()

        # Validación temprana con mensajes claros
        if modo == "rangos":
            try:
                organizador.parsear_rangos(self.var_rangos.get(), self._num_paginas)
            except ValueError as e:
                messagebox.showwarning("Rangos no válidos", str(e))
                return
        cada_n = 1
        if modo == "cada_n":
            try:
                cada_n = max(1, int(self.var_cada_n.get()))
            except ValueError:
                messagebox.showwarning(
                    "Valor no válido",
                    "Indica cuántas páginas debe tener cada bloque (número entero).",
                )
                return

        carpeta_salida = self.var_carpeta_salida.get().strip()
        if not carpeta_salida:
            carpeta_salida = filedialog.askdirectory(title="Carpeta de salida")
            if not carpeta_salida:
                return
            self.var_carpeta_salida.set(carpeta_salida)

        opciones = organizador.OpcionesDivision(
            modo=modo,
            rangos_texto=self.var_rangos.get(),
            cada_n=cada_n,
            anonimizar=self.var_anonimizar.get(),
            eliminar_anotaciones=self.var_eliminar_anotaciones.get(),
        )

        carpeta_final = Path(carpeta_salida)
        if self.var_subcarpeta.get():
            carpeta_final = carpeta_final / f"{Path(ruta).stem}_dividido"

        self.var_progreso.set(0.0)
        self.lbl_progreso.configure(text="Iniciando…")
        self.estado("Dividiendo PDF…")
        self._lanzar_hilo(
            self._trabajo_dividir, ruta, carpeta_final, opciones,
        )

    def _trabajo_dividir(self, ruta, carpeta_salida: Path,
                         opciones: organizador.OpcionesDivision) -> None:
        def cb(texto, indice, total):
            self._cola_eventos.put(
                ("progreso", indice * 100 / max(total, 1), texto)
            )

        resultado = organizador.dividir_pdf(
            ruta, carpeta_salida, opciones, progreso_callback=cb,
        )
        self._cola_eventos.put(("progreso", 100.0, "Completado"))
        self._cola_eventos.put(("fin", resultado))

    def _procesar_evento(self, evento) -> None:
        tipo = evento[0]
        if tipo == "progreso":
            _, valor, texto = evento
            self.var_progreso.set(valor)
            self.lbl_progreso.configure(text=texto)
        elif tipo == "fin":
            _, resultado = evento
            self._mostrar_resumen(resultado)
            self.estado("Listo.")
        else:
            super()._procesar_evento(evento)

    def _mostrar_resumen(self, resultado) -> None:
        if not resultado.pdfs_generados:
            messagebox.showwarning(
                "Sin resultados", "No se generó ningún PDF.",
            )
            return

        carpeta = resultado.pdfs_generados[0].parent
        lineas = [
            f"PDFs generados: {len(resultado.pdfs_generados)}",
            f"Tamaño total:   {formatear_tamano(resultado.tamano_total)}",
            f"Carpeta: {carpeta}",
            "",
            "Archivos:",
        ]
        MAX_LINEAS = 12
        for p in resultado.pdfs_generados[:MAX_LINEAS]:
            lineas.append(f" • {p.name}")
        restantes = len(resultado.pdfs_generados) - MAX_LINEAS
        if restantes > 0:
            lineas.append(f" … y {restantes} más en la misma carpeta.")

        if messagebox.askyesno(
            "División finalizada",
            "\n".join(lineas) + "\n\n¿Quieres abrir la carpeta de salida?",
        ):
            abrir_carpeta(carpeta)


# ---------------------------------------------------------------------------
# 3.c) Reordenar / rotar / eliminar / extraer páginas
# ---------------------------------------------------------------------------

class PestanaReordenar(PestanaBase):

    ANCHO_PREVIA = 260   # píxeles máximos de la miniatura

    def __init__(self, master):
        super().__init__(master)

        self.var_info_pdf = tk.StringVar(value="Ningún PDF cargado.")
        self.var_info_previa = tk.StringVar(value="")
        self.var_progreso = tk.DoubleVar(value=0.0)

        self._ruta_pdf: str | None = None
        self._doc = None                     # fitz.Document para las miniaturas
        self._paginas: list[organizador.PaginaEditada] = []
        self._img_previa = None              # referencia viva del PhotoImage

        self._construir_layout()

    def _construir_layout(self) -> None:
        pista = (
            "Elige (o arrastra) un PDF, reordena sus páginas con ↑/↓, gíralas, "
            "elimina las que sobren o extrae una selección. El original no se toca."
            if DND_DISPONIBLE
            else "Elige un PDF, reordena sus páginas con ↑/↓, gíralas, elimina "
                 "las que sobren o extrae una selección. El original no se toca."
        )
        ttk.Label(self, text=pista, foreground="#555", wraplength=760,
                  justify="left").pack(anchor="w", pady=(0, 6))

        self.selector = SelectorPDF(self, al_cambiar=self._cargar_pdf)
        self.selector.pack(fill="x")

        ttk.Label(self, textvariable=self.var_info_pdf, foreground="#555")\
            .pack(anchor="w", pady=(4, 6))

        # ---- Zona central: lista + botones + vista previa ----
        centro = ttk.Frame(self)
        centro.pack(fill="both", expand=True)

        marco_lista = ttk.Frame(centro)
        marco_lista.pack(side="left", fill="both", expand=True)

        self.lista = tk.Listbox(
            marco_lista, selectmode="extended", activestyle="dotbox", height=8,
        )
        scroll = ttk.Scrollbar(marco_lista, orient="vertical",
                               command=self.lista.yview)
        self.lista.configure(yscrollcommand=scroll.set)
        self.lista.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        marco_lista.columnconfigure(0, weight=1)
        marco_lista.rowconfigure(0, weight=1)
        self.lista.bind("<<ListboxSelect>>", self._al_seleccionar)

        botones = ttk.Frame(centro)
        botones.pack(side="left", fill="y", padx=6)
        ttk.Button(botones, text="↑ Subir", width=16,
                   command=lambda: self._mover(-1)).pack(pady=2)
        ttk.Button(botones, text="↓ Bajar", width=16,
                   command=lambda: self._mover(+1)).pack(pady=2)
        ttk.Separator(botones, orient="horizontal").pack(fill="x", pady=6)
        ttk.Button(botones, text="⟲ Girar 90° izq.", width=16,
                   command=lambda: self._rotar(-90)).pack(pady=2)
        ttk.Button(botones, text="⟳ Girar 90° der.", width=16,
                   command=lambda: self._rotar(+90)).pack(pady=2)
        ttk.Separator(botones, orient="horizontal").pack(fill="x", pady=6)
        ttk.Button(botones, text="Eliminar página(s)", width=16,
                   command=self._eliminar).pack(pady=2)
        ttk.Button(botones, text="Extraer selección…", width=16,
                   command=self._extraer).pack(pady=2)
        ttk.Separator(botones, orient="horizontal").pack(fill="x", pady=6)
        ttk.Button(botones, text="Restaurar original", width=16,
                   command=self._restaurar).pack(pady=2)

        marco_previa = ttk.LabelFrame(centro, text="Vista previa", padding=6)
        marco_previa.pack(side="left", fill="y")
        self.lbl_previa = ttk.Label(
            marco_previa, text="(Selecciona una página)",
            anchor="center", width=38,
        )
        self.lbl_previa.pack(expand=True, fill="both")
        ttk.Label(marco_previa, textvariable=self.var_info_previa,
                  foreground="#555").pack(anchor="center", pady=(4, 0))

        # ---- Anonimización / carpeta / progreso / botón ----
        self.construir_bloque_anonimizacion(self).pack(fill="x", pady=4)
        self.construir_bloque_carpeta_salida(self).pack(fill="x", pady=4)

        marco_progreso = ttk.Frame(self)
        marco_progreso.pack(fill="x", pady=(8, 0))
        self.barra = ttk.Progressbar(
            marco_progreso, variable=self.var_progreso,
            maximum=100.0, mode="determinate",
        )
        self.barra.pack(fill="x")
        self.lbl_progreso = ttk.Label(marco_progreso, text="", foreground="#444")
        self.lbl_progreso.pack(anchor="w", pady=(2, 0))

        ttk.Button(
            self, text="Guardar PDF reordenado", style="Accion.TButton",
            command=self._guardar,
        ).pack(pady=(6, 8))

    # ------------------------------------------------------------------
    # Carga del PDF y modelo de páginas
    # ------------------------------------------------------------------

    def _cargar_pdf(self, ruta: str) -> None:
        try:
            doc = organizador.abrir_pdf(ruta)
        except ValueError as e:
            messagebox.showerror("PDF no válido", str(e))
            return

        if self._doc is not None:
            try:
                self._doc.close()
            except Exception:
                pass

        self._doc = doc
        self._ruta_pdf = ruta
        self._paginas = [
            organizador.PaginaEditada(indice_original=i)
            for i in range(len(doc))
        ]
        self.var_info_pdf.set(
            f"«{Path(ruta).name}» — {len(doc)} páginas. "
            "Los cambios se guardan en una copia; el original no se modifica."
        )
        self._refrescar_lista(seleccion=[0])
        self._actualizar_previa()

    def _refrescar_lista(self, seleccion: list[int] | None = None) -> None:
        if seleccion is None:
            seleccion = list(self.lista.curselection())
        self.lista.delete(0, "end")
        for p in self._paginas:
            etiqueta = f"Página {p.indice_original + 1}"
            if p.rotacion_extra % 360:
                etiqueta += f"   (girada {p.rotacion_extra % 360}°)"
            self.lista.insert("end", etiqueta)
        for idx in seleccion:
            if 0 <= idx < self.lista.size():
                self.lista.selection_set(idx)
        if seleccion and 0 <= seleccion[0] < self.lista.size():
            self.lista.see(seleccion[0])

    # ------------------------------------------------------------------
    # Operaciones sobre las páginas
    # ------------------------------------------------------------------

    def _seleccion(self) -> list[int]:
        return list(self.lista.curselection())

    def _mover(self, delta: int) -> None:
        seleccion = self._seleccion()
        if not seleccion:
            return
        # No mover si el bloque seleccionado ya toca el borde
        if delta < 0 and seleccion[0] == 0:
            return
        if delta > 0 and seleccion[-1] == len(self._paginas) - 1:
            return
        iterar = seleccion if delta < 0 else list(reversed(seleccion))
        nuevas_posiciones = []
        for idx in iterar:
            destino = idx + delta
            if destino < 0 or destino >= len(self._paginas):
                nuevas_posiciones.append(idx)
                continue
            self._paginas[idx], self._paginas[destino] = (
                self._paginas[destino], self._paginas[idx]
            )
            nuevas_posiciones.append(destino)
        self._refrescar_lista(seleccion=nuevas_posiciones)
        self._actualizar_previa()

    def _rotar(self, grados: int) -> None:
        seleccion = self._seleccion()
        if not seleccion:
            messagebox.showinfo("Sin selección", "Selecciona las páginas a girar.")
            return
        for idx in seleccion:
            p = self._paginas[idx]
            p.rotacion_extra = (p.rotacion_extra + grados) % 360
        self._refrescar_lista(seleccion=seleccion)
        self._actualizar_previa()

    def _eliminar(self) -> None:
        seleccion = self._seleccion()
        if not seleccion:
            messagebox.showinfo("Sin selección", "Selecciona las páginas a eliminar.")
            return
        for idx in reversed(seleccion):
            del self._paginas[idx]
        siguiente = min(seleccion[0], len(self._paginas) - 1)
        self._refrescar_lista(
            seleccion=[siguiente] if siguiente >= 0 else []
        )
        self._actualizar_previa()

    def _restaurar(self) -> None:
        if self._doc is None:
            return
        self._paginas = [
            organizador.PaginaEditada(indice_original=i)
            for i in range(len(self._doc))
        ]
        self._refrescar_lista(seleccion=[0])
        self._actualizar_previa()

    # ------------------------------------------------------------------
    # Vista previa
    # ------------------------------------------------------------------

    def _al_seleccionar(self, _event=None) -> None:
        self._actualizar_previa()

    def _actualizar_previa(self) -> None:
        seleccion = self._seleccion()
        if self._doc is None or not seleccion or not self._paginas:
            self.lbl_previa.configure(image="", text="(Selecciona una página)")
            self.var_info_previa.set("")
            self._img_previa = None
            return

        idx = seleccion[0]
        if idx >= len(self._paginas):
            return
        p = self._paginas[idx]
        try:
            png = organizador.render_miniatura_png(
                self._doc, p.indice_original,
                rotacion_extra=p.rotacion_extra,
                max_px=self.ANCHO_PREVIA,
            )
            self._img_previa = tk.PhotoImage(data=png)
            self.lbl_previa.configure(image=self._img_previa, text="")
            detalle = f"Página {p.indice_original + 1} · posición {idx + 1} de {len(self._paginas)}"
            if p.rotacion_extra % 360:
                detalle += f" · girada {p.rotacion_extra % 360}°"
            self.var_info_previa.set(detalle)
        except Exception:
            self.lbl_previa.configure(image="", text="(Sin vista previa)")
            self.var_info_previa.set("")
            self._img_previa = None

    # ------------------------------------------------------------------
    # Guardar / extraer
    # ------------------------------------------------------------------

    def _pedir_carpeta(self) -> str | None:
        carpeta = self.var_carpeta_salida.get().strip()
        if not carpeta:
            carpeta = filedialog.askdirectory(title="Carpeta de salida")
            if not carpeta:
                return None
            self.var_carpeta_salida.set(carpeta)
        return carpeta

    def _copiar_paginas(self, indices: list[int]) -> list:
        return [
            organizador.PaginaEditada(
                indice_original=self._paginas[i].indice_original,
                rotacion_extra=self._paginas[i].rotacion_extra,
            )
            for i in indices
        ]

    def _guardar(self) -> None:
        if not self._ruta_pdf or self._doc is None:
            messagebox.showinfo("Sin archivo", "Carga primero un PDF.")
            return
        if not self._paginas:
            messagebox.showwarning(
                "Sin páginas",
                "Has eliminado todas las páginas. Restaura el original o deja "
                "al menos una.",
            )
            return
        carpeta = self._pedir_carpeta()
        if not carpeta:
            return
        self._lanzar_recomposicion(
            self._copiar_paginas(list(range(len(self._paginas)))),
            Path(carpeta),
            f"{Path(self._ruta_pdf).stem}_reordenado",
            "Reordenación finalizada",
        )

    def _extraer(self) -> None:
        if not self._ruta_pdf or self._doc is None:
            messagebox.showinfo("Sin archivo", "Carga primero un PDF.")
            return
        seleccion = self._seleccion()
        if not seleccion:
            messagebox.showinfo(
                "Sin selección",
                "Selecciona las páginas que quieres extraer a un PDF nuevo.",
            )
            return
        carpeta = self._pedir_carpeta()
        if not carpeta:
            return
        self._lanzar_recomposicion(
            self._copiar_paginas(seleccion),
            Path(carpeta),
            f"{Path(self._ruta_pdf).stem}_extracto",
            "Extracción finalizada",
        )

    def _lanzar_recomposicion(self, paginas, carpeta: Path, nombre: str,
                              titulo: str) -> None:
        opciones = organizador.OpcionesRecomposicion(
            anonimizar=self.var_anonimizar.get(),
            eliminar_anotaciones=self.var_eliminar_anotaciones.get(),
        )
        self.var_progreso.set(0.0)
        self.lbl_progreso.configure(text="Iniciando…")
        self.estado("Generando PDF…")
        self._lanzar_hilo(
            self._trabajo_recomponer,
            self._ruta_pdf, paginas, carpeta, nombre, opciones, titulo,
        )

    def _trabajo_recomponer(self, ruta, paginas, carpeta: Path, nombre: str,
                            opciones, titulo: str) -> None:
        carpeta.mkdir(parents=True, exist_ok=True)
        ruta_destino = ruta_salida_unica(carpeta, nombre, ".pdf")

        def cb(texto, indice, total):
            self._cola_eventos.put(
                ("progreso", indice * 100 / max(total, 1), texto)
            )

        resultado = organizador.recomponer_pdf(
            ruta, paginas, ruta_destino, opciones, progreso_callback=cb,
        )
        self._cola_eventos.put(("progreso", 100.0, "Completado"))
        self._cola_eventos.put(("fin", resultado, titulo))

    def _procesar_evento(self, evento) -> None:
        tipo = evento[0]
        if tipo == "progreso":
            _, valor, texto = evento
            self.var_progreso.set(valor)
            self.lbl_progreso.configure(text=texto)
        elif tipo == "fin":
            _, resultado, titulo = evento
            if messagebox.askyesno(
                titulo,
                f"Páginas: {resultado.num_paginas}\n"
                f"Tamaño: {formatear_tamano(resultado.tamano_final)}\n\n"
                f"Guardado como:\n{resultado.ruta_destino}\n\n"
                "¿Quieres abrir la carpeta de salida?",
            ):
                abrir_carpeta(resultado.ruta_destino.parent)
            self.estado("Listo.")
        else:
            super()._procesar_evento(evento)


# ===========================================================================
# Pestaña 4: PDF a Word (.docx)
# ===========================================================================

class PestanaPdfAWord(PestanaBase):

    def __init__(self, master):
        super().__init__(master)

        self.var_rangos = tk.StringVar(value="")
        self.var_progreso = tk.DoubleVar(value=0.0)

        self._construir_layout()

    def _construir_layout(self) -> None:
        pista = (
            "Arrastra PDFs aquí o pulsa «Añadir PDFs»: cada uno se convertirá "
            "en un documento Word (.docx)."
            if DND_DISPONIBLE
            else "Pulsa «Añadir PDFs»: cada uno se convertirá en un documento "
                 "Word (.docx)."
        )
        ttk.Label(self, text=pista, foreground="#555").pack(anchor="w", pady=(0, 2))
        ttk.Label(
            self,
            text="Nota: la conversión PDF → Word nunca es exacta al 100 %. "
                 "Los PDF escaneados (imagen) se convertirán sin texto editable "
                 "(requerirían OCR).",
            foreground="#777", wraplength=760, justify="left",
        ).pack(anchor="w", pady=(0, 6))

        # Lista de PDFs
        self.lista = ListaArchivos(self, extensiones_validas={".pdf"}, alto=7)
        self.lista.pack(fill="both", expand=True)

        botones = ttk.Frame(self)
        botones.pack(fill="x", pady=(6, 8))
        ttk.Button(botones, text="Añadir PDFs…", command=self._anadir_pdfs)\
            .pack(side="left")
        ttk.Button(botones, text="Quitar seleccionados",
                   command=self.lista.quitar_seleccionadas).pack(side="left", padx=4)
        ttk.Button(botones, text="Vaciar lista",
                   command=self.lista.vaciar).pack(side="left")

        # ---- Opciones ----
        opciones = ttk.LabelFrame(self, text="Opciones de conversión", padding=10)
        opciones.pack(fill="x", pady=4)

        ttk.Label(opciones, text="Páginas a convertir (opcional):")\
            .grid(row=0, column=0, sticky="w")
        ttk.Entry(opciones, textvariable=self.var_rangos, width=20)\
            .grid(row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Label(
            opciones,
            text="ej.: 1-3, 5 · vacío = todas · se aplica a cada PDF de la lista",
            foreground="#777",
        ).grid(row=1, column=1, sticky="w", padx=(8, 0))

        # ---- Anonimización (metadatos del Word resultante) ----
        marco_anon = ttk.LabelFrame(self, text="Anonimización", padding=10)
        marco_anon.pack(fill="x", pady=4)
        ttk.Checkbutton(
            marco_anon,
            text="No incluir metadatos personales en el Word (autor, título, asunto…)",
            variable=self.var_anonimizar,
        ).grid(row=0, column=0, sticky="w")

        # ---- Carpeta / progreso / botón ----
        self.construir_bloque_carpeta_salida(self).pack(fill="x", pady=4)

        marco_progreso = ttk.Frame(self)
        marco_progreso.pack(fill="x", pady=(8, 0))
        self.barra = ttk.Progressbar(
            marco_progreso, variable=self.var_progreso,
            maximum=100.0, mode="determinate",
        )
        self.barra.pack(fill="x")
        self.lbl_progreso = ttk.Label(marco_progreso, text="", foreground="#444")
        self.lbl_progreso.pack(anchor="w", pady=(2, 0))

        ttk.Button(
            self, text="Convertir a Word", style="Accion.TButton",
            command=self._iniciar_conversion,
        ).pack(pady=10)

    # ---- Acciones ----

    def _anadir_pdfs(self) -> None:
        rutas = filedialog.askopenfilenames(
            title="Selecciona PDFs",
            filetypes=[("Archivos PDF", "*.pdf")],
        )
        if rutas:
            self.lista.agregar(list(rutas))

    def _iniciar_conversion(self) -> None:
        rutas = self.lista.obtener_todas()
        if not rutas:
            messagebox.showinfo("Sin archivos", "Añade al menos un PDF a la lista.")
            return

        # Validación temprana de la sintaxis de los rangos (los límites
        # reales se comprueban por archivo, cada PDF tiene sus páginas).
        rangos = self.var_rangos.get().strip()
        if rangos:
            try:
                organizador.parsear_rangos(rangos, 10 ** 6)
            except ValueError as e:
                messagebox.showwarning("Rangos no válidos", str(e))
                return

        carpeta_salida = self.var_carpeta_salida.get().strip()
        if not carpeta_salida:
            carpeta_salida = filedialog.askdirectory(title="Carpeta de salida")
            if not carpeta_salida:
                return
            self.var_carpeta_salida.set(carpeta_salida)

        opciones = conversor_docx.OpcionesPdfADocx(
            rangos_texto=rangos,
            anonimizar=self.var_anonimizar.get(),
        )

        self.var_progreso.set(0.0)
        self.lbl_progreso.configure(text="Iniciando…")
        self.estado("Convirtiendo PDF a Word…")
        self._lanzar_hilo(
            self._trabajo_convertir, rutas, Path(carpeta_salida), opciones,
        )

    # ---- Trabajo en segundo plano ----

    def _trabajo_convertir(self, rutas, carpeta_salida: Path,
                           opciones: conversor_docx.OpcionesPdfADocx) -> None:
        carpeta_salida.mkdir(parents=True, exist_ok=True)

        total = len(rutas)
        resultados = []
        errores: list[tuple[str, str]] = []

        for i, ruta in enumerate(rutas, start=1):
            ruta_origen = Path(ruta)
            self._cola_eventos.put((
                "progreso",
                (i - 1) * 100 / total,
                f"Convirtiendo {i}/{total}: {ruta_origen.name} "
                "(puede tardar un poco)…",
            ))
            ruta_destino = ruta_salida_unica(
                carpeta_salida, ruta_origen.stem, ".docx"
            )
            try:
                resultados.append(
                    conversor_docx.convertir_pdf_a_docx(
                        ruta_origen, ruta_destino, opciones,
                    )
                )
            except Exception as e:
                errores.append((ruta_origen.name, str(e)))

        self._cola_eventos.put(("progreso", 100.0, "Completado"))
        self._cola_eventos.put(("fin", resultados, errores))

    # ---- Manejo de eventos ----

    def _procesar_evento(self, evento) -> None:
        tipo = evento[0]
        if tipo == "progreso":
            _, valor, texto = evento
            self.var_progreso.set(valor)
            self.lbl_progreso.configure(text=texto)
        elif tipo == "fin":
            _, resultados, errores = evento
            self._mostrar_resumen(resultados, errores)
            self.estado("Listo.")
        else:
            super()._procesar_evento(evento)

    def _mostrar_resumen(self, resultados, errores) -> None:
        if not resultados and not errores:
            return

        lineas = [f"Documentos Word generados: {len(resultados)}"]
        if resultados:
            total = sum(r.tamano_final for r in resultados)
            lineas.append(f"Tamaño total: {formatear_tamano(total)}")
            lineas.append("")
            lineas.append("Archivos:")
            for r in resultados:
                lineas.append(
                    f" • {r.ruta_destino.name}  "
                    f"({r.num_paginas} págs., {formatear_tamano(r.tamano_final)})"
                )

        avisos = [r.aviso for r in resultados if r.aviso]
        if avisos:
            lineas.append("")
            lineas.append("Avisos:")
            for a in avisos:
                lineas.append(f" ⚠ {a}")

        if errores:
            lineas.append("")
            lineas.append("Errores:")
            for nombre, err in errores:
                lineas.append(f" • {nombre}: {err}")

        mensaje = "\n".join(lineas)
        if resultados:
            if messagebox.askyesno(
                "Conversión finalizada",
                mensaje + "\n\n¿Quieres abrir la carpeta de salida?",
            ):
                abrir_carpeta(resultados[0].ruta_destino.parent)
        else:
            messagebox.showinfo("Conversión finalizada", mensaje)


# ===========================================================================
# Lanzador
# ===========================================================================

def lanzar() -> None:
    app = AplicacionPDF()
    app.mainloop()
