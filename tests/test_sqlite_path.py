"""Pruebas de `core.config.sqlite_path_from_url`.

Cubren los cuatro formatos de URL que maneja el backend:

1. `sqlite:///relativa.db`        -> relativa al CWD
2. `sqlite:///C:/ruta.db`         -> absoluta con unidad Windows (3 barras)
3. `sqlite:////ruta/absoluta.db`  -> absoluta POSIX (4 barras)
4. `sqlite:////C:/ruta.db`        -> absoluta con unidad Windows y 4 barras

Se ejecutan con `python -m unittest` (sin dependencias extra) y también con pytest.
"""
from __future__ import annotations

import unittest
from pathlib import Path

from src.core.config import DATA_DIR, _resolve_sqlite_db_path, sqlite_path_from_url


class SqliteUrlPathTests(unittest.TestCase):
    # 1) Ruta relativa: se resuelve contra el directorio de trabajo.
    def test_relativa_se_resuelve_contra_cwd(self) -> None:
        got = sqlite_path_from_url("sqlite:///relative/path.db")
        self.assertEqual(got, Path("relative/path.db").resolve())

    # 2) Ruta Windows con tres barras.
    def test_windows_drive_tres_barras(self) -> None:
        got = sqlite_path_from_url("sqlite:///C:/Users/x/data/arcanea.db")
        self.assertEqual(got, Path("C:/Users/x/data/arcanea.db"))

    # 3) Ruta absoluta POSIX: es el caso que antes se convertía en relativa.
    def test_posix_absoluta(self) -> None:
        got = sqlite_path_from_url("sqlite:////srv/media/arcanea.db")
        self.assertEqual(got, Path("/srv/media/arcanea.db"))
        self.assertTrue(got.is_absolute())

    # 4) Ruta Windows con cuatro barras ("/C:/..." -> "C:/...").
    def test_windows_drive_cuatro_barras(self) -> None:
        got = sqlite_path_from_url("sqlite:////C:/Users/x/data/arcanea.db")
        self.assertEqual(got, Path("C:/Users/x/data/arcanea.db"))

    # Regresión: la URL que genera el propio backend no debe duplicar el CWD.
    def test_url_generada_por_el_backend_no_duplica_la_ruta(self) -> None:
        url = f"sqlite:///{DATA_DIR / 'arcanea.db'}"
        got = sqlite_path_from_url(url)
        self.assertEqual(got, DATA_DIR / "arcanea.db")
        self.assertEqual(str(got).count(str(DATA_DIR)), 1)
        self.assertEqual(_resolve_sqlite_db_path(), DATA_DIR / "arcanea.db")

    def test_memoria_y_no_sqlite_devuelven_none(self) -> None:
        self.assertIsNone(sqlite_path_from_url("sqlite:///:memory:"))
        self.assertIsNone(sqlite_path_from_url("sqlite://"))
        self.assertIsNone(sqlite_path_from_url("postgresql://user:pass@host/db"))
        self.assertIsNone(sqlite_path_from_url(""))
        self.assertIsNone(sqlite_path_from_url(None))

    def test_urls_invalidas_no_lanzan(self) -> None:
        self.assertIsNone(sqlite_path_from_url("no es una url"))


if __name__ == "__main__":
    unittest.main()
