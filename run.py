"""
Lanzador raíz utilizado tanto en desarrollo como por PyInstaller.

Mantenerlo en la raíz del proyecto permite que `src` se trate como un
paquete real (con sus imports relativos intactos) tanto al ejecutar
`python run.py` desde el repositorio como al empaquetar con PyInstaller.
"""

from src.main import main


if __name__ == "__main__":
    main()
