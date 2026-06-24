import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

import pandas as pd

import ghj_codex_V_03 as dashboard


class DashboardTests(unittest.TestCase):
    def test_health(self):
        client = dashboard.app.test_client()
        response = client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "ok")

    def test_parse_date(self):
        self.assertEqual(dashboard.parse_yyyymmdd("2026-06-24"), date(2026, 6, 24))

    def test_pivot_and_excel(self):
        rows = []
        for buyer in ("외국인", "기관합계"):
            rows.append({
                "종목코드": "005930",
                "종목명": "삼성전자",
                "거래량_매도": 10,
                "거래량_매수": 20,
                "거래량_순매수": 10,
                "거래대금_매도": 100,
                "거래대금_매수": 200,
                "거래대금_순매수": 100,
                "d_today_year": 2026,
                "d_today_month": 6,
                "d_today_day": 24,
                "period(D-00)_start": -30,
                "period(D-00)_end": 0,
                "buyer": buyer,
            })
        base_df = pd.DataFrame(rows)
        last_df = dashboard.create_pivot(base_df)
        self.assertIn(("1개월누적", "외국인"), last_df.columns)

        with tempfile.TemporaryDirectory() as temp_dir:
            output_path, row_count = dashboard.write_outputs(
                base_df,
                last_df,
                Path(temp_dir),
                datetime(2026, 6, 24),
            )
            self.assertTrue(output_path.exists())
            self.assertEqual(row_count, 1)


if __name__ == "__main__":
    unittest.main()
