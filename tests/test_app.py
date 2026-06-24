import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import ghj_codex_V_03 as dashboard


class DashboardTests(unittest.TestCase):
    def test_health(self):
        client = dashboard.app.test_client()
        response = client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "ok")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])

    def test_parse_date(self):
        self.assertEqual(dashboard.parse_yyyymmdd("2026-06-24"), date(2026, 6, 24))

    def test_future_date_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "오늘 이후"):
            dashboard.validate_collection_input(
                "user",
                "password",
                date(date.today().year + 1, 1, 1),
                "ALL",
            )

    def test_post_requires_csrf(self):
        client = dashboard.app.test_client()
        response = client.post("/", data={
            "krx_id": "user",
            "krx_pw": "password",
            "as_of": date.today().isoformat(),
            "market": "ALL",
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn("요청 인증이 만료되었습니다".encode("utf-8"), response.data)

    def test_valid_csrf_reaches_collection(self):
        client = dashboard.app.test_client()
        client.get("/")
        with client.session_transaction() as flask_session:
            csrf_token = flask_session["csrf_token"]

        fake_result = dashboard.RunResult(
            output_path=Path("result.xlsx"),
            base_rows=1,
            last_rows=1,
            trading_dates=["20260623"],
            charts=[],
            top_rows=[],
        )
        with (
            patch.object(dashboard, "run_collection", return_value=fake_result),
            patch.object(dashboard, "register_download", return_value="download-token"),
        ):
            response = client.post("/", data={
                "csrf_token": csrf_token,
                "krx_id": "user",
                "krx_pw": "password",
                "as_of": date.today().isoformat(),
                "market": "ALL",
            })

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"/download/download-token", response.data)

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
