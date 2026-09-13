"""
Smoke test de la GUI bajo Xvfb: instancia la app, comprueba las pestañas
nuevas y ejerce el flujo de la pestaña Reordenar sin usuario.
Ejecutar:  xvfb-run -a python3.12 test_gui_smoke.py
"""
import sys
import tempfile
from pathlib import Path

try:
    import pymupdf as fitz
except ImportError:
    import fitz

sys.path.insert(0, str(Path(__file__).parent))

from src import gui, organizador  # noqa: E402

FALLOS = []


def check(nombre, cond, detalle=""):
    print(("  ✓ " if cond else "  ✗ ") + nombre + ("" if cond else f"  {detalle}"))
    if not cond:
        FALLOS.append(nombre)


tmp = Path(tempfile.mkdtemp())
pdf = tmp / "prueba.pdf"
doc = fitz.open()
for i in range(1, 6):
    p = doc.new_page(width=595, height=842)
    p.insert_text((72, 100), f"P{i}", fontsize=24)
doc.save(pdf)
doc.close()

print(f"DnD disponible en este entorno: {gui.DND_DISPONIBLE}")

app = gui.AplicacionPDF()
app.update_idletasks()

check("ventana creada con versión",
      app.title().startswith("ARAEMKA PDF 1.2"), app.title())
check("pestaña organizar existe", hasattr(app, "pestana_organizar"))
check("pestaña PDF a Word existe", hasattr(app, "pestana_pdf_a_word"))

# ---- PDF a Word: filtro de lista y valores por defecto ----
paw = app.pestana_pdf_a_word
anadidos_paw = paw.lista.agregar([str(pdf), __file__])
check("pdf a word: filtra no-PDF", anadidos_paw == 1, f"anadidos={anadidos_paw}")
check("pdf a word: rangos vacío por defecto", paw.var_rangos.get() == "")
check("pdf a word: anonimizar desactivado por defecto",
      not paw.var_anonimizar.get())

org = app.pestana_organizar
check("sub-pestañas creadas",
      hasattr(org, "pestana_unir") and hasattr(org, "pestana_dividir")
      and hasattr(org, "pestana_reordenar"))

# ---- Unir: lista acepta solo PDFs ----
unir = org.pestana_unir
anadidos = unir.lista.agregar([str(pdf), str(tmp / "no_existe.pdf"), __file__])
check("unir: filtra no-PDF y no-existentes", anadidos == 1,
      f"anadidos={anadidos}")

# ---- Unir: arrastrar una carpeta añade sus PDFs ----
carpeta_pdfs = tmp / "lote"
carpeta_pdfs.mkdir()
for n in ("uno", "dos"):
    d2 = fitz.open(); d2.new_page(); d2.save(carpeta_pdfs / f"{n}.pdf"); d2.close()
(carpeta_pdfs / "nota.txt").write_text("no soy pdf")
anadidos = unir.lista.agregar([str(carpeta_pdfs)])
check("unir: carpeta arrastrada → añade sus PDFs", anadidos == 2,
      f"anadidos={anadidos}")

# ---- Dividir: carga de PDF actualiza info ----
div = org.pestana_dividir
div.selector._establecer(str(pdf))
check("dividir: cuenta páginas", "5 páginas" in div.var_info_pdf.get(),
      div.var_info_pdf.get())
div.var_modo.set("cada_n")
div._actualizar_estado_modo()
check("dividir: entrada rangos deshabilitada",
      str(div._entrada_rangos.cget("state")) == "disabled")
check("dividir: subcarpeta activada por defecto", div.var_subcarpeta.get())

# ---- Reordenar: flujo completo sobre el modelo ----
reo = org.pestana_reordenar
reo.selector._establecer(str(pdf))
app.update_idletasks()
check("reordenar: modelo con 5 páginas", len(reo._paginas) == 5)
check("reordenar: doc abierto", reo._doc is not None)
check("reordenar: lista poblada", reo.lista.size() == 5)
check("reordenar: vista previa renderizada", reo._img_previa is not None)

# mover página 1 hacia arriba (ya está en el borde): no debe cambiar nada
reo.lista.selection_clear(0, "end")
reo.lista.selection_set(0)
reo._mover(-1)
check("reordenar: tope superior respetado",
      [p.indice_original for p in reo._paginas] == [0, 1, 2, 3, 4])

# mover página 1 abajo
reo.lista.selection_clear(0, "end")
reo.lista.selection_set(0)
reo._mover(+1)
check("reordenar: mover abajo",
      [p.indice_original for p in reo._paginas] == [1, 0, 2, 3, 4])

# rotar la seleccionada (ahora en posición 1)
reo._rotar(+90)
check("reordenar: rotación aplicada", reo._paginas[1].rotacion_extra == 90)
check("reordenar: etiqueta muestra giro",
      "girada 90°" in reo.lista.get(1))

# eliminar la última
reo.lista.selection_clear(0, "end")
reo.lista.selection_set(4)
reo._eliminar()
check("reordenar: eliminación", len(reo._paginas) == 4
      and [p.indice_original for p in reo._paginas] == [1, 0, 2, 3])

# guardar (recomposición directa, sin hilo, usando el mismo camino de datos)
paginas = reo._copiar_paginas(list(range(len(reo._paginas))))
salida = tmp / "salida_gui.pdf"
res = organizador.recomponer_pdf(str(pdf), paginas, salida)
d = fitz.open(salida)
textos = [d[i].get_text().strip() for i in range(len(d))]
rotaciones = [d[i].rotation for i in range(len(d))]
d.close()
check("reordenar: PDF final correcto", textos == ["P2", "P1", "P3", "P4"],
      str(textos))
check("reordenar: rotación en PDF final", rotaciones == [0, 90, 0, 0],
      str(rotaciones))

# restaurar
reo._restaurar()
check("reordenar: restaurar original",
      len(reo._paginas) == 5
      and [p.indice_original for p in reo._paginas] == [0, 1, 2, 3, 4]
      and all(p.rotacion_extra == 0 for p in reo._paginas))

app.destroy()

print()
if FALLOS:
    print(f"SMOKE GUI: {len(FALLOS)} FALLOS -> {FALLOS}")
    sys.exit(1)
print("SMOKE GUI: TODO OK")
