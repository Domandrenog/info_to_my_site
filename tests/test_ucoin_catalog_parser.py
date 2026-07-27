from __future__ import annotations

import unittest

from ucoin_to_mysite.catalog_parser import parse_ucoin_catalogue


def page(*body: str) -> str:
    return """
    <html>
      <head><link rel="canonical" href="https://pt.ucoin.net/catalog/?country=canada"></head>
      <body>
        <h1>Canadá</h1>
        <main>
          <header><h2><span>Canadá › Rainha Isabel II › 1953 - 2026</span></h2></header>
          %s
        </main>
      </body>
    </html>
    """ % "\n".join(body)


def coin(
    value: str = "5 cêntimos, 1982-1989",
    tid: str = "3266",
    pid: str = "24",
    detail: str = "/coin/canada-5-cents-1982-1989/?tid=3266",
    info: str = "Cupro-Níquel, 4.6g, ø 21.2mm<br>KM# 60.2a · Circulação normal",
    subject: str = "",
    obverse: str = "https://i.ucoin.net/coin/46/974/46974204-1s/canada-5-cents-1984.jpg",
    reverse: str = "https://i.ucoin.net/coin/46/974/46974204-2s/canada-5-cents-1984.jpg",
) -> str:
    subject_html = f'<div class="subject">{subject}</div>' if subject else ""
    obverse_html = (
        f'<td class="coin-img"><a href="{detail}"><img src="{obverse}" alt="Canadá 5 cêntimos - Obverse" loading="lazy"></a></td>'
        if obverse
        else '<td class="coin-img"></td>'
    )
    reverse_html = (
        f'<td class="coin-img"><a href="{detail}"><img data-src="{reverse}" alt="Canadá 5 cêntimos - Reverse" loading="lazy"></a></td>'
        if reverse
        else '<td class="coin-img"></td>'
    )
    return f"""
    <table class="coin">
      <tbody>
        <tr>
          {obverse_html}
          {reverse_html}
          <td class="coin-info">
            <a href="{detail}" class="value">{value}</a>
            <br>
            {subject_html}
            <div class="info">{info}</div>
            <div class="coin-stat" data-tid="{tid}" data-pid="{pid}"></div>
          </td>
        </tr>
        <tr id="d{tid}-{pid}"><td colspan="2"></td><td class="detail"></td></tr>
      </tbody>
    </table>
    """


class UCoinCatalogueParserTests(unittest.TestCase):
    def parse_one(self, html: str) -> dict[str, object]:
        result = parse_ucoin_catalogue(html)
        self.assertGreaterEqual(len(result["coins"]), 1)
        return result["coins"][0]

    def test_coin_with_year_range(self) -> None:
        parsed = self.parse_one(page(coin()))
        self.assertEqual(parsed["issuePeriod"], {"displayValue": "1982-1989", "startYear": 1982, "endYear": 1989})
        self.assertEqual(parsed["ucoinTypeId"], 3266)

    def test_coin_with_single_year(self) -> None:
        parsed = self.parse_one(page(coin(value="1 cêntimo, 1979", detail="/coin/canada-1-cent-1979/?tid=191845", tid="191845")))
        self.assertEqual(parsed["issuePeriod"], {"displayValue": "1979", "startYear": 1979, "endYear": 1979})

    def test_km_catalogue_number(self) -> None:
        parsed = self.parse_one(page(coin(info="Cupro-Níquel, 4.6g, ø 21.2mm<br>KM# 182b · Circulação normal")))
        self.assertEqual(parsed["catalogue"], {"type": "KM", "number": "182b", "displayValue": "KM# 182b"})

    def test_uc_catalogue_number(self) -> None:
        parsed = self.parse_one(page(coin(info="Bimetálica, 6.99g, ø 28mm<br>UC# 14 · Circulação normal")))
        self.assertEqual(parsed["catalogue"], {"type": "UC", "number": "14", "displayValue": "UC# 14"})

    def test_subject(self) -> None:
        parsed = self.parse_one(page(coin(subject="125 anos do Canadá")))
        self.assertEqual(parsed["subject"], "125 anos do Canadá")

    def test_nested_silver_composition(self) -> None:
        parsed = self.parse_one(page(coin(info='<span class="Ag">Ag</span>Prata 0.800, 2.33g, ø 18.03mm<br>KM# 51 · Circulação normal')))
        self.assertEqual(parsed["composition"], "AgPrata 0.800")

    def test_two_different_same_denomination_and_year(self) -> None:
        html = page(
            coin(
                value="10 cêntimos, 1968",
                tid="38282",
                detail="/coin/canada-10-cents-1968/?tid=38282",
                info='<span class="Ag">Ag</span>Prata 0.500, 2.33g, ø 18.03mm<br>KM# 72 · Circulação normal',
            ),
            coin(
                value="10 cêntimos, 1968",
                tid="12300",
                detail="/coin/canada-10-cents-1968/?tid=12300",
                info="Níquel, 2.07g, ø 18.03mm<br>KM# 73 · Circulação normal",
            ),
        )
        result = parse_ucoin_catalogue(html)
        self.assertEqual(len(result["coins"]), 2)
        self.assertEqual({coin["ucoinTypeId"] for coin in result["coins"]}, {38282, 12300})

    def test_missing_image_warning(self) -> None:
        result = parse_ucoin_catalogue(page(coin(obverse="", reverse="")))
        fields = {warning["field"] for warning in result["warnings"]}
        self.assertIn("images.obverse", fields)
        self.assertIn("images.reverse", fields)

    def test_missing_catalogue_warning(self) -> None:
        result = parse_ucoin_catalogue(page(coin(info="Níquel, 5.05g, ø 23.88mm<br>Circulação normal")))
        self.assertIn("catalogue", {warning["field"] for warning in result["warnings"]})

    def test_malformed_coin_warning_without_stopping(self) -> None:
        html = page('<table class="coin"><tr><td class="coin-info"><div class="info">Níquel</div></td></tr></table>', coin())
        result = parse_ucoin_catalogue(html)
        self.assertEqual(len(result["coins"]), 2)
        self.assertTrue(result["warnings"])

    def test_issue_period_and_image_year_are_distinct(self) -> None:
        parsed = self.parse_one(page(coin()))
        self.assertEqual(parsed["issuePeriod"]["displayValue"], "1982-1989")
        self.assertEqual(parsed["imageExampleYear"], 1984)

    def test_issue_period_and_ruler_period_are_distinct(self) -> None:
        result = parse_ucoin_catalogue(page(coin()))
        parsed = result["coins"][0]
        period = result["periods"][0]
        self.assertEqual(parsed["issuePeriod"]["startYear"], 1982)
        self.assertEqual(period["rulerOrPeriodName"], "Rainha Isabel II")
        self.assertEqual(period["startYear"], 1953)
        self.assertEqual(period["endYear"], 2026)
        self.assertEqual(period["periodId"], 24)

    def test_decimal_numbers_using_comma(self) -> None:
        parsed = self.parse_one(page(coin(info="Níquel, 5,05g, ø 23,88mm<br>KM# 74 · Circulação normal")))
        self.assertEqual(parsed["weightGrams"], 5.05)
        self.assertEqual(parsed["diameterMm"], 23.88)

    def test_lazy_loaded_images(self) -> None:
        parsed = self.parse_one(page(coin()))
        self.assertEqual(parsed["images"]["reverse"], "https://i.ucoin.net/coin/46/974/46974204-2s/canada-5-cents-1984.jpg")

    def test_relative_and_absolute_urls(self) -> None:
        parsed = self.parse_one(page(coin()))
        self.assertEqual(parsed["detailPath"], "/coin/canada-5-cents-1982-1989/?tid=3266")
        self.assertEqual(parsed["detailUrl"], "https://pt.ucoin.net/coin/canada-5-cents-1982-1989/?tid=3266")


if __name__ == "__main__":
    unittest.main()