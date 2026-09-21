from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts import update_ucoin_image_sources


def difference_payload() -> list[dict[str, object]]:
    return [
        {
            "country": "filipinas",
            "country_name": "Filipinas",
            "coins_with_issues": [
                {
                    "denomination": "1 cêntimo",
                    "issuePeriod": "1995 - 2016",
                    "issues": [
                        {
                            "type": "non_ucoin_external_image",
                            "field": "links-externos.frente",
                            "value": "https://base44.app/front.jpg",
                            "missing_value": "https://i.ucoin.net/front.jpg",
                        },
                        {
                            "type": "non_ucoin_external_image",
                            "field": "links-externos.tras",
                            "value": "https://base44.app/back.jpg",
                            "missing_value": "https://i.ucoin.net/back.jpg",
                        },
                    ],
                }
            ],
        }
    ]


class UpdateUcoinImageSourcesTests(unittest.TestCase):
    def test_extracts_and_counts_safe_source_replacements(self) -> None:
        replacements = update_ucoin_image_sources.image_source_replacements(difference_payload())

        self.assertEqual(update_ucoin_image_sources.replacement_counts(replacements), (1, 2))
        self.assertEqual([item["side"] for item in replacements], ["frente", "tras"])
        self.assertEqual(replacements[0]["country_name"], "Filipinas")

    def test_prepares_exact_links_without_changing_other_entries(self) -> None:
        replacements = update_ucoin_image_sources.image_source_replacements(difference_payload())
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            normal = root / "fotos" / "paises" / "Asia" / "Filipinas" / "normal"
            normal.mkdir(parents=True)
            path = normal / "links-externos.txt"
            original = (
                "filipinas-1-cent-1995-2016:\n"
                "  frente: https://base44.app/front.jpg\n"
                "  tras: https://base44.app/back.jpg\n\n"
                "filipinas-5-cent-1995-2017:\n"
                "  frente: https://example.test/keep.jpg\n"
            )
            path.write_text(original, encoding="utf-8")

            prepared, errors = update_ucoin_image_sources.prepare_file_updates(replacements, root)

            self.assertEqual(errors, [])
            self.assertEqual(path.read_text(encoding="utf-8"), original)
            update_ucoin_image_sources.apply_file_updates(prepared)
            updated = path.read_text(encoding="utf-8")

        self.assertIn("frente: https://i.ucoin.net/front.jpg", updated)
        self.assertIn("tras: https://i.ucoin.net/back.jpg", updated)
        self.assertIn("frente: https://example.test/keep.jpg", updated)

    def test_validation_error_blocks_every_file_write(self) -> None:
        replacements = update_ucoin_image_sources.image_source_replacements(difference_payload())
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            normal = root / "fotos" / "paises" / "Asia" / "Filipinas" / "normal"
            normal.mkdir(parents=True)
            path = normal / "links-externos.txt"
            original = (
                "filipinas-1-cent-1995-2016:\n"
                "  frente: https://base44.app/front.jpg\n"
                "  tras: https://different.test/back.jpg\n"
            )
            path.write_text(original, encoding="utf-8")

            prepared, errors = update_ucoin_image_sources.prepare_file_updates(replacements, root)

            self.assertEqual(prepared, {})
            self.assertEqual(len(errors), 1)
            self.assertIn("encontrados 0", errors[0])
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_main_requires_explicit_confirmation_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            report_path = root / "differences.json"
            report_path.write_text(json.dumps(difference_payload()), encoding="utf-8")
            normal = root / "All_Coins" / "fotos" / "paises" / "Asia" / "Filipinas" / "normal"
            normal.mkdir(parents=True)
            links_path = normal / "links-externos.txt"
            links_path.write_text(
                "filipinas-1-cent-1995-2016:\n"
                "  frente: https://base44.app/front.jpg\n"
                "  tras: https://base44.app/back.jpg\n",
                encoding="utf-8",
            )
            args = type(
                "Args",
                (),
                {"input": str(report_path), "all_coins_dir": str(root / "All_Coins"), "apply": True},
            )()

            with (
                patch.object(update_ucoin_image_sources, "parse_args", return_value=args),
                patch("builtins.input", return_value="n"),
                redirect_stdout(io.StringIO()) as output,
            ):
                result = update_ucoin_image_sources.main()

            unchanged = links_path.read_text(encoding="utf-8")

        self.assertEqual(result, 0)
        self.assertIn("Operação cancelada", output.getvalue())
        self.assertIn("https://base44.app/front.jpg", unchanged)


if __name__ == "__main__":
    unittest.main()
