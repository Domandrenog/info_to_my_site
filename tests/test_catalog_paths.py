from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.catalog_paths import (
    continent_label_for_country,
    country_directory,
    find_country_directory,
    iter_country_directories,
)


class CatalogPathsTests(unittest.TestCase):
    def test_known_country_uses_mapped_continent(self) -> None:
        self.assertEqual(country_directory("África do Sul"), Path("info/paises/africa/africa-do-sul"))
        self.assertEqual(continent_label_for_country("Nova Zelândia"), "Oceânia")

    def test_unknown_country_requires_continent(self) -> None:
        with self.assertRaisesRegex(ValueError, "Continente desconhecido"):
            country_directory("País Novo")

    def test_explicit_continent_is_normalized(self) -> None:
        self.assertEqual(country_directory("Japão", "Ásia"), Path("info/paises/asia/japao"))

    def test_existing_directory_alias_is_preserved(self) -> None:
        self.assertEqual(country_directory("Croácia"), Path("info/paises/europa/croatia"))
        self.assertEqual(country_directory("Egito"), Path("info/paises/africa/egipto"))
        self.assertEqual(country_directory("Maurícias"), Path("info/paises/africa/mauricia"))

    def test_find_and_iter_country_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            india = root / "asia" / "india"
            canada = root / "america" / "canada"
            india.mkdir(parents=True)
            canada.mkdir(parents=True)

            self.assertEqual(find_country_directory(root, "Índia"), india)
            self.assertEqual(list(iter_country_directories(root)), [canada, india])


if __name__ == "__main__":
    unittest.main()
