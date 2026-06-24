import base64
import json
import os
import re
import secrets
import threading
import time
import webbrowser
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from http.cookiejar import CookieJar
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib import parse, request
from urllib.error import HTTPError

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
from flask import Flask, flash, render_template_string, request as flask_request, send_file, session
from markupsafe import escape


GET_JSON_URL = "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
LOGIN_PAGE_URL = "https://data.krx.co.kr/contents/MDC/COMS/client/MDCCOMS001.cmd"
LOGIN_JSP_URL = "https://data.krx.co.kr/contents/MDC/COMS/client/view/login.jsp?site=mdc"
LOGIN_URL = "https://data.krx.co.kr/contents/MDC/COMS/client/MDCCOMS001D1.cmd"
REFERER_URL = "https://data.krx.co.kr/contents/MDC/MDI/mdiLoader/index.cmd?menuId=MDC0201020303"

INVESTOR_CODES = {
    "기관합계": "7050",
    "외국인": "9000",
}

MARKET_CODES = {
    "ALL": "ALL",
    "KOSPI": "STK",
    "KOSDAQ": "KSQ",
    "KONEX": "KNX",
}

BASE_COLUMNS = [
    "종목코드",
    "종목명",
    "거래량_매도",
    "거래량_매수",
    "거래량_순매수",
    "거래대금_매도",
    "거래대금_매수",
    "거래대금_순매수",
]

METADATA_COLUMNS = [
    "d_today_year",
    "d_today_month",
    "d_today_day",
    "period(D-00)_start",
    "period(D-00)_end",
    "buyer",
]

BASE_DIR = Path(__file__).resolve().parent
IS_VERCEL = bool(os.environ.get("VERCEL"))
OUTPUT_ROOT = Path("/tmp/outputs") if IS_VERCEL else BASE_DIR / "outputs"
DOC_PATH = BASE_DIR / "프로그램_상세설명.md"
DOWNLOADS: dict[str, tuple[Path, float]] = {}
DOWNLOAD_TTL_SECONDS = 30 * 60
RECENT_TRADING_DAYS = 5
MAX_CALENDAR_LOOKBACK = 20
REQUEST_TIMEOUT = 30
MAX_CREDENTIAL_LENGTH = 200


@dataclass(frozen=True)
class FetchJob:
    buyer: str
    start_date: date
    end_date: date
    period_start: int
    period_end: int
    label: str


@dataclass(frozen=True)
class RunResult:
    output_path: Path
    base_rows: int
    last_rows: int
    trading_dates: list[str]
    charts: list[dict[str, str]]
    top_rows: list[dict[str, Any]]
    download_token: str = ""


class KrxInvestorApi:
    def __init__(self, krx_id: str, krx_pw: str, timeout: int = 30):
        self.krx_id = krx_id
        self.krx_pw = krx_pw
        self.timeout = timeout
        self.cookie_jar = CookieJar()
        self.opener = request.build_opener(request.HTTPCookieProcessor(self.cookie_jar))
        self.login()

    def login(self) -> None:
        self._get(LOGIN_PAGE_URL, {"User-Agent": "Mozilla/5.0"})
        self._get(LOGIN_JSP_URL, {"User-Agent": "Mozilla/5.0", "Referer": LOGIN_PAGE_URL})

        payload = {
            "mbrNm": "",
            "telNo": "",
            "di": "",
            "certType": "",
            "mbrId": self.krx_id,
            "pw": self.krx_pw,
        }
        data = self._post_login(payload)

        if data.get("_error_code") == "CD011":
            payload["skipDup"] = "Y"
            data = self._post_login(payload)

        if data.get("_error_code") != "CD001":
            message = data.get("_error_message") or data
            raise RuntimeError(f"KRX 로그인 실패: {message}")

    def fetch_net_buy_top_stocks(
        self,
        buyer: str,
        start_date: date,
        end_date: date,
        market: str = "ALL",
    ) -> pd.DataFrame:
        if buyer not in INVESTOR_CODES:
            raise ValueError(f"buyer는 {', '.join(INVESTOR_CODES)} 중 하나여야 합니다.")

        market = market.upper()
        if market not in MARKET_CODES:
            raise ValueError(f"market은 {', '.join(MARKET_CODES)} 중 하나여야 합니다.")

        payload = {
            "bld": "dbms/MDC/STAT/standard/MDCSTAT02401",
            "locale": "ko_KR",
            "mktId": MARKET_CODES[market],
            "strtDd": start_date.strftime("%Y%m%d"),
            "endDd": end_date.strftime("%Y%m%d"),
            "invstTpCd": INVESTOR_CODES[buyer],
            "trdVolVal": "1",
            "share": "1",
            "money": "1",
            "csvxls_isNo": "false",
        }
        data = self._post_json(payload)
        rows = self._extract_rows(data)
        return normalize_rows(rows)

    def _get(self, url: str, headers: dict[str, str]) -> None:
        req = request.Request(url, headers=headers, method="GET")
        with self.opener.open(req, timeout=self.timeout):
            pass

    def _post_login(self, payload: dict[str, str]) -> dict[str, Any]:
        req = request.Request(
            LOGIN_URL,
            data=parse.urlencode(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "Mozilla/5.0",
                "Referer": LOGIN_PAGE_URL,
            },
            method="POST",
        )
        with self.opener.open(req, timeout=self.timeout) as response:
            raw = response.read().decode("utf-8")
        return json.loads(raw)

    def _post_json(self, payload: dict[str, str]) -> dict[str, Any]:
        req = request.Request(
            GET_JSON_URL,
            data=parse.urlencode(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "User-Agent": "Mozilla/5.0",
                "Referer": REFERER_URL,
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Origin": "https://data.krx.co.kr",
            },
            method="POST",
        )

        try:
            with self.opener.open(req, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace").strip()
            if body == "LOGOUT":
                raise RuntimeError("KRX 로그인 세션이 없거나 만료되었습니다.")
            raise RuntimeError(f"KRX API 오류 HTTP {exc.code}: {body[:300]}") from exc

        if raw.strip() == "LOGOUT":
            raise RuntimeError("KRX 로그인 세션이 없거나 만료되었습니다.")
        return json.loads(raw)

    @staticmethod
    def _extract_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
        for key in ("output", "OutBlock_1", "block1"):
            rows = data.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
        return []


def parse_yyyymmdd(value: str) -> date:
    digits = re.sub(r"\D", "", value or "")
    if not re.fullmatch(r"\d{8}", digits):
        raise ValueError("날짜는 YYYYMMDD 또는 YYYY-MM-DD 형식이어야 합니다.")
    return datetime.strptime(digits, "%Y%m%d").date()


def validate_collection_input(krx_id: str, krx_pw: str, as_of: date, market: str) -> None:
    if not krx_id or not krx_pw:
        raise ValueError("KRX 아이디와 비밀번호를 모두 입력해야 합니다.")
    if len(krx_id) > MAX_CREDENTIAL_LENGTH or len(krx_pw) > MAX_CREDENTIAL_LENGTH:
        raise ValueError("로그인 정보의 길이가 올바르지 않습니다.")
    if market not in MARKET_CODES:
        raise ValueError(f"시장은 {', '.join(MARKET_CODES)} 중 하나여야 합니다.")
    if as_of > date.today():
        raise ValueError("기준일은 오늘 이후 날짜로 지정할 수 없습니다.")


def to_int(value: Any) -> int:
    cleaned = re.sub(r"[^0-9\-]", "", str(value or ""))
    if not cleaned or cleaned == "-":
        return 0
    return int(cleaned)


def pick_value(row: dict[str, Any], candidates: tuple[str, ...]) -> Any:
    normalized = {str(k).strip().upper(): v for k, v in row.items()}
    for candidate in candidates:
        key = candidate.upper()
        if key in normalized:
            return normalized[key]
    return ""


def normalize_rows(rows: list[dict[str, Any]]) -> pd.DataFrame:
    records = []
    for row in rows:
        record = {
            "종목코드": str(pick_value(row, ("ISU_SRT_CD", "ISU_CD", "종목코드"))).zfill(6),
            "종목명": pick_value(row, ("ISU_ABBRV", "ISU_NM", "ISU_KOR_NM", "종목명")),
            "거래량_매도": to_int(pick_value(row, ("ASK_TRDVOL", "ASK_TRD_VOL", "매도거래량", "거래량_매도"))),
            "거래량_매수": to_int(pick_value(row, ("BID_TRDVOL", "BID_TRD_VOL", "매수거래량", "거래량_매수"))),
            "거래량_순매수": to_int(pick_value(row, ("NETBID_TRDVOL", "NETBID_TRD_VOL", "순매수거래량", "거래량_순매수"))),
            "거래대금_매도": to_int(pick_value(row, ("ASK_TRDVAL", "ASK_TRD_VAL", "매도거래대금", "거래대금_매도"))),
            "거래대금_매수": to_int(pick_value(row, ("BID_TRDVAL", "BID_TRD_VAL", "매수거래대금", "거래대금_매수"))),
            "거래대금_순매수": to_int(pick_value(row, ("NETBID_TRDVAL", "NETBID_TRD_VAL", "순매수거래대금", "거래대금_순매수"))),
        }
        if record["종목코드"] and record["종목명"]:
            records.append(record)
    return pd.DataFrame(records, columns=BASE_COLUMNS)


def add_metadata(df: pd.DataFrame, job: FetchJob, now: datetime) -> pd.DataFrame:
    enriched = df.copy()
    enriched["d_today_year"] = now.year
    enriched["d_today_month"] = now.month
    enriched["d_today_day"] = now.day
    enriched["period(D-00)_start"] = job.period_start
    enriched["period(D-00)_end"] = job.period_end
    enriched["buyer"] = job.buyer
    return enriched[BASE_COLUMNS + METADATA_COLUMNS]


def build_jobs(as_of: date) -> list[FetchJob]:
    jobs = []
    for buyer in INVESTOR_CODES:
        jobs.append(FetchJob(buyer, as_of - timedelta(days=180), as_of, -180, 0, "6개월"))
        jobs.append(FetchJob(buyer, as_of - timedelta(days=90), as_of, -90, 0, "3개월"))
        jobs.append(FetchJob(buyer, as_of - timedelta(days=30), as_of, -30, 0, "1개월"))
    return jobs


def collect_recent_trading_day_jobs(
    api: KrxInvestorApi,
    as_of: date,
    market: str,
    trading_days: int,
    max_calendar_lookback: int,
) -> tuple[list[FetchJob], list[str]]:
    jobs = []
    found_dates: list[tuple[date, int]] = []
    day_offset = 0

    while len(found_dates) < trading_days and day_offset < max_calendar_lookback:
        day_offset += 1
        target = as_of - timedelta(days=day_offset)
        probe = api.fetch_net_buy_top_stocks("외국인", target, target, market)
        if probe.empty:
            continue
        found_dates.append((target, -day_offset))

    if len(found_dates) < trading_days:
        raise RuntimeError(f"최근 영업일 {trading_days}개를 찾지 못했습니다.")

    for target, period in found_dates:
        for buyer in INVESTOR_CODES:
            jobs.append(FetchJob(buyer, target, target, period, period, target.strftime("%Y%m%d")))
    return jobs, [target.strftime("%Y%m%d") for target, _ in found_dates]


def create_pivot(base_df: pd.DataFrame) -> pd.DataFrame:
    last_df = pd.pivot_table(
        base_df,
        index=["종목코드", "종목명"],
        columns=["period(D-00)_start", "buyer"],
        values="거래량_순매수",
        aggfunc="sum",
    )
    last_df = last_df.sort_index(axis=1, ascending=False)

    def convert_period_label(period: int) -> str:
        period = abs(int(period))
        if period == 30:
            return "1개월누적"
        if period == 90:
            return "3개월누적"
        if period == 180:
            return "6개월누적"
        return f"{period}일전"

    last_df.columns = pd.MultiIndex.from_tuples(
        [(convert_period_label(period), buyer) for period, buyer in last_df.columns],
        names=last_df.columns.names,
    )
    return last_df


def flatten_columns(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        flat_columns = []
        for column in df.columns:
            parts = [str(part) for part in column if str(part) and not str(part).startswith("Unnamed")]
            flat_columns.append("_".join(parts) if parts else "")
        df = df.copy()
        df.columns = flat_columns
    return df


def stock_names_from_index(index_obj: pd.Index) -> list[str]:
    names = []
    for item in index_obj:
        if isinstance(item, tuple) and len(item) >= 2:
            names.append(str(item[1]))
        else:
            names.append(str(item))
    return names


def fig_to_base64(fig: plt.Figure) -> str:
    buffer = BytesIO()
    fig.savefig(buffer, format="png", dpi=140, bbox_inches="tight")
    plt.close(fig)
    buffer.seek(0)
    return base64.b64encode(buffer.read()).decode("ascii")


def add_chart(charts: list[dict[str, str]], title: str, fig: plt.Figure) -> None:
    charts.append({"title": title, "image": fig_to_base64(fig)})


def create_visualizations(last_df: pd.DataFrame) -> list[dict[str, str]]:
    charts: list[dict[str, str]] = []
    if last_df.empty:
        return charts

    plt.rcParams["font.family"] = ["Malgun Gothic", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    df_clean = last_df.fillna(0).copy()

    if ("1개월누적", "외국인") in df_clean.columns and ("1개월누적", "기관합계") in df_clean.columns:
        top10_idx = df_clean[("1개월누적", "외국인")].nlargest(10).index
        plot_df = df_clean.loc[top10_idx, [("1개월누적", "외국인"), ("1개월누적", "기관합계")]].copy()
        plot_df.columns = ["외국인", "기관합계"]
        fig, ax = plt.subplots(figsize=(11, 5.8))
        plot_df.plot(kind="bar", ax=ax, color=["#0b8069", "#3858a8"])
        ax.set_title("1개월누적 외국인 순매수 TOP10")
        ax.set_xlabel("")
        ax.set_ylabel("순매수 수량")
        ax.set_xticklabels(stock_names_from_index(plot_df.index), rotation=35, ha="right")
        ax.axhline(0, color="#263238", linewidth=1)
        ax.grid(axis="y", alpha=0.25)
        add_chart(charts, "1개월누적 외국인 TOP10", fig)

        fig, ax = plt.subplots(figsize=(6.8, 6.2))
        ax.scatter(
            df_clean[("1개월누적", "외국인")],
            df_clean[("1개월누적", "기관합계")],
            alpha=0.35,
            color="#0b8069",
            s=18,
        )
        ax.axhline(0, color="#263238", linewidth=1)
        ax.axvline(0, color="#263238", linewidth=1)
        ax.set_title("외국인 vs 기관합계 산점도")
        ax.set_xlabel("외국인 순매수")
        ax.set_ylabel("기관합계 순매수")
        ax.grid(alpha=0.25)
        add_chart(charts, "외국인 vs 기관합계", fig)

        top20_idx = df_clean[("1개월누적", "외국인")].nlargest(20).index
        heat_df = df_clean.loc[top20_idx].copy()
        heat_df.columns = [f"{period}_{buyer}" for period, buyer in heat_df.columns]
        heat_df.index = stock_names_from_index(heat_df.index)
        fig, ax = plt.subplots(figsize=(13, 8))
        image = ax.imshow(heat_df.values, aspect="auto", cmap="coolwarm")
        ax.set_title("외국인 순매수 TOP20 기간별 수급 히트맵")
        ax.set_xticks(range(len(heat_df.columns)))
        ax.set_xticklabels(heat_df.columns, rotation=45, ha="right")
        ax.set_yticks(range(len(heat_df.index)))
        ax.set_yticklabels(heat_df.index)
        fig.colorbar(image, ax=ax, label="순매수 수량")
        add_chart(charts, "TOP20 기간별 수급 히트맵", fig)

    recent_periods = [
        period
        for period in ["10일전", "9일전", "8일전", "7일전", "6일전", "5일전", "4일전", "3일전", "2일전", "1일전"]
        if (period, "외국인") in df_clean.columns and (period, "기관합계") in df_clean.columns
    ]
    if recent_periods:
        foreign_mean = df_clean.xs("외국인", level=1, axis=1)[recent_periods].mean()
        inst_mean = df_clean.xs("기관합계", level=1, axis=1)[recent_periods].mean()
        fig, ax = plt.subplots(figsize=(10, 5.4))
        x = range(len(recent_periods))
        ax.bar([v - 0.18 for v in x], foreign_mean.values, width=0.36, label="외국인", color="#0b8069")
        ax.bar([v + 0.18 for v in x], inst_mean.values, width=0.36, label="기관합계", color="#3858a8")
        ax.set_xticks(list(x))
        ax.set_xticklabels(recent_periods)
        ax.set_title("최근 영업일 평균 순매수")
        ax.axhline(0, color="#263238", linewidth=1)
        ax.grid(axis="y", alpha=0.25)
        ax.legend()
        add_chart(charts, "최근 영업일 평균", fig)

    acc_periods = [
        period
        for period in ["1개월누적", "3개월누적", "6개월누적"]
        if (period, "외국인") in df_clean.columns and (period, "기관합계") in df_clean.columns
    ]
    if acc_periods:
        foreign_acc = df_clean.xs("외국인", level=1, axis=1)[acc_periods].mean()
        inst_acc = df_clean.xs("기관합계", level=1, axis=1)[acc_periods].mean()
        fig, ax = plt.subplots(figsize=(8.5, 5.2))
        x = range(len(acc_periods))
        ax.bar([v - 0.18 for v in x], foreign_acc.values, width=0.36, label="외국인", color="#0b8069")
        ax.bar([v + 0.18 for v in x], inst_acc.values, width=0.36, label="기관합계", color="#3858a8")
        ax.set_xticks(list(x))
        ax.set_xticklabels(acc_periods)
        ax.set_title("1개월 / 3개월 / 6개월 누적 평균 순매수")
        ax.axhline(0, color="#263238", linewidth=1)
        ax.grid(axis="y", alpha=0.25)
        ax.legend()
        add_chart(charts, "누적 평균 비교", fig)

    return charts


def create_top_rows(last_df: pd.DataFrame, limit: int = 15) -> list[dict[str, Any]]:
    flat_df = flatten_columns(last_df.reset_index())
    if "1개월누적_외국인" in flat_df.columns:
        flat_df = flat_df.sort_values("1개월누적_외국인", ascending=False)
    fixed_cols = {
        "종목코드",
        "종목명",
        "1개월누적_외국인",
        "1개월누적_기관합계",
        "3개월누적_외국인",
        "3개월누적_기관합계",
        "6개월누적_외국인",
        "6개월누적_기관합계",
    }
    recent_cols = [
        col
        for col in flat_df.columns
        if re.match(r"^\d+일전_(외국인|기관합계)$", str(col))
    ]
    recent_cols = sorted(recent_cols, key=lambda col: int(str(col).split("일전_")[0]))
    display_cols = [col for col in flat_df.columns if col in fixed_cols]
    display_cols.extend([col for col in recent_cols if col not in display_cols])
    return flat_df[display_cols].head(limit).to_dict("records")


def write_outputs(base_df: pd.DataFrame, last_df: pd.DataFrame, out_dir: Path, now: datetime) -> tuple[Path, int]:
    out_dir.mkdir(parents=True, exist_ok=True)
    last_df_reset = flatten_columns(last_df.reset_index())

    filename = f"{now.strftime('%Y-%m-%d')}_통합파일.xlsx"
    output_path = out_dir / filename
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        base_df.to_excel(writer, index=False, sheet_name="base")
        last_df_reset.to_excel(writer, index=False, sheet_name="last")
    return output_path, len(last_df_reset)


def run_collection(
    krx_id: str,
    krx_pw: str,
    as_of: date,
    market: str,
    output_root: Path,
) -> RunResult:
    validate_collection_input(krx_id, krx_pw, as_of, market)

    now = datetime.now()
    out_dir = output_root / now.strftime("%Y%m%d")
    api = KrxInvestorApi(krx_id, krx_pw, timeout=REQUEST_TIMEOUT)

    jobs = build_jobs(as_of)
    recent_jobs, trading_dates = collect_recent_trading_day_jobs(
        api=api,
        as_of=as_of,
        market=market,
        trading_days=RECENT_TRADING_DAYS,
        max_calendar_lookback=MAX_CALENDAR_LOOKBACK,
    )
    jobs.extend(recent_jobs)

    frames = []
    for job in jobs:
        df = api.fetch_net_buy_top_stocks(job.buyer, job.start_date, job.end_date, market)
        if not df.empty:
            frames.append(add_metadata(df, job, now))

    if not frames:
        raise RuntimeError("수집된 데이터가 없습니다.")

    base_df = pd.concat(frames, ignore_index=True)
    last_df = create_pivot(base_df)
    charts = create_visualizations(last_df)
    top_rows = create_top_rows(last_df)
    output_path, last_rows = write_outputs(base_df, last_df, out_dir, now)
    return RunResult(
        output_path=output_path,
        base_rows=len(base_df),
        last_rows=last_rows,
        trading_dates=trading_dates,
        charts=charts,
        top_rows=top_rows,
    )


def register_download(output_path: Path) -> str:
    now = time.time()
    expired = [token for token, (_, created_at) in DOWNLOADS.items() if now - created_at > DOWNLOAD_TTL_SECONDS]
    for token in expired:
        old_path, _ = DOWNLOADS.pop(token)
        try:
            old_path.unlink(missing_ok=True)
        except OSError:
            pass

    token = secrets.token_urlsafe(24)
    DOWNLOADS[token] = (output_path, now)
    return token


app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "ghj-codex-local-secret")
app.config.update(
    MAX_CONTENT_LENGTH=32 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=IS_VERCEL,
)


@app.after_request
def add_security_headers(response):
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; "
        "form-action 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'"
    )
    return response


def get_csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def validate_csrf_token(submitted_token: str) -> None:
    expected_token = session.get("csrf_token", "")
    if not expected_token or not submitted_token or not secrets.compare_digest(expected_token, submitted_token):
        raise ValueError("요청 인증이 만료되었습니다. 페이지를 새로고침한 뒤 다시 실행해 주세요.")

PAGE_TEMPLATE = """
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>GHJ KRX 수급 자동화</title>
  <style>
    :root {
      --bg: #f3f6f5;
      --panel: #fff;
      --text: #1d282b;
      --muted: #637176;
      --line: #d8e2e3;
      --accent: #0b8069;
      --accent-dark: #096653;
      --danger: #b3261e;
      --success: #166534;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--text);
      font-family: "Segoe UI", "Malgun Gothic", Arial, sans-serif;
    }
    main {
      width: min(1080px, calc(100% - 32px));
      margin: 0 auto;
      padding: 34px 0;
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: 0 16px 36px rgba(29, 40, 43, .08);
      padding: 28px;
    }
    .eyebrow {
      margin: 0 0 8px;
      color: var(--accent);
      font-size: 13px;
      font-weight: 800;
    }
    h1 { margin: 0; font-size: clamp(28px, 4vw, 40px); letter-spacing: 0; }
    .sub { margin: 10px 0 0; color: var(--muted); line-height: 1.6; }
    form {
      display: grid;
      grid-template-columns: repeat(6, minmax(0, 1fr));
      gap: 16px;
      margin-top: 26px;
      align-items: end;
    }
    .field { display: grid; gap: 7px; }
    .span2 { grid-column: span 2; }
    .span3 { grid-column: span 3; }
    .span6 { grid-column: span 6; }
    label {
      color: var(--muted);
      font-size: 13px;
      font-weight: 800;
    }
    .help {
      min-height: 42px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
    }
    .fixed-scope {
      grid-column: span 6;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #f8fbfa;
      padding: 14px 16px;
      color: var(--muted);
      line-height: 1.6;
    }
    .fixed-scope strong {
      color: var(--text);
    }
    input, select, button {
      width: 100%;
      min-height: 44px;
      border-radius: 6px;
      font: inherit;
    }
    input, select {
      border: 1px solid var(--line);
      padding: 0 12px;
      background: #fff;
    }
    button {
      border: 0;
      background: var(--accent);
      color: #fff;
      font-weight: 900;
      cursor: pointer;
    }
    button:hover { background: var(--accent-dark); }
    .messages { display: grid; gap: 8px; margin-top: 20px; }
    .message { margin: 0; padding: 12px 14px; border-radius: 6px; font-weight: 800; }
    .error { color: var(--danger); background: #fff1f1; }
    .success { color: var(--success); background: #edf9f2; }
    .result {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 14px;
      margin-top: 18px;
    }
    .dashboard-title {
      margin: 26px 0 0;
      font-size: 24px;
      letter-spacing: 0;
    }
    .metric {
      background: #f8fbfa;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
    }
    .metric dt { color: var(--muted); font-weight: 800; font-size: 13px; }
    .metric dd { margin: 6px 0 0; font-size: 20px; font-weight: 900; }
    .download {
      display: inline-flex;
      margin-top: 18px;
      min-height: 44px;
      align-items: center;
      justify-content: center;
      padding: 0 18px;
      border-radius: 6px;
      background: var(--accent);
      color: #fff;
      font-weight: 900;
      text-decoration: none;
    }
    .top-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 18px;
    }
    .secondary-link {
      display: inline-flex;
      min-height: 44px;
      align-items: center;
      justify-content: center;
      padding: 0 18px;
      border-radius: 6px;
      border: 1px solid var(--accent);
      color: var(--accent-dark);
      font-weight: 900;
      text-decoration: none;
      background: #fff;
    }
    .visuals {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 16px;
      margin-top: 22px;
    }
    .chart-card {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      padding: 16px;
    }
    .chart-card h3 {
      margin: 0 0 12px;
      font-size: 17px;
      letter-spacing: 0;
    }
    .chart-card img {
      display: block;
      width: 100%;
      height: auto;
    }
    .table-wrap {
      margin-top: 18px;
      overflow: auto;
      border: 1px solid var(--line);
      border-radius: 8px;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      min-width: 760px;
      background: #fff;
    }
    th, td {
      border-bottom: 1px solid var(--line);
      padding: 10px 12px;
      text-align: left;
      white-space: nowrap;
      font-size: 13px;
    }
    th {
      background: #eef5f4;
      color: #24423d;
      font-weight: 900;
    }
    .loading {
      position: fixed;
      inset: 0;
      z-index: 20;
      display: none;
      align-items: center;
      justify-content: center;
      background: rgba(15, 23, 42, .45);
      padding: 20px;
    }
    .loading.active { display: flex; }
    .loading-box {
      width: min(460px, 100%);
      border-radius: 8px;
      background: #fff;
      padding: 24px;
      box-shadow: 0 24px 70px rgba(15, 23, 42, .28);
    }
    .loading-title {
      margin: 0 0 8px;
      font-size: 20px;
      font-weight: 900;
    }
    .bar {
      height: 12px;
      overflow: hidden;
      border-radius: 999px;
      background: #dbe4e7;
      margin-top: 16px;
    }
    .bar-fill {
      width: 0%;
      height: 100%;
      background: var(--accent);
      transition: width .35s ease;
    }
    .percent {
      margin-top: 10px;
      font-size: 28px;
      font-weight: 900;
      color: var(--accent-dark);
    }
    .guide {
      margin-top: 22px;
      padding-top: 22px;
      border-top: 1px solid var(--line);
    }
    .guide h2 {
      margin: 0 0 12px;
      font-size: 21px;
      letter-spacing: 0;
    }
    .guide ol {
      margin: 0;
      padding-left: 22px;
      color: var(--text);
      line-height: 1.7;
    }
    .guide a {
      color: var(--accent-dark);
      font-weight: 900;
    }
    .note {
      margin: 12px 0 0;
      color: var(--muted);
      line-height: 1.6;
      font-size: 14px;
    }
    @media (max-width: 860px) {
      main { width: min(100% - 20px, 1080px); padding: 16px 0; }
      .panel { padding: 18px; }
      form, .result, .visuals { grid-template-columns: 1fr; }
      .span2, .span3, .span6, .fixed-scope { grid-column: auto; }
    }
  </style>
</head>
<body>
  <main>
    <section class="panel">
      <p class="eyebrow">GHJ Codex V.03</p>
      <h1>오늘 기준 KRX 수급 분석</h1>
      <p class="sub">KRX 아이디와 비밀번호를 입력하면 기존 노트북처럼 누적 구간과 최근 일별 수급을 한 번에 조회하고 기본 시각화까지 바로 보여줍니다.</p>
      <div class="top-actions">
        <a class="secondary-link" href="/docs" target="_blank" rel="noopener">프로그램 상세 설명 보기</a>
      </div>

      {% with messages = get_flashed_messages(with_categories=true) %}
        {% if messages %}
          <div class="messages">
            {% for category, message in messages %}
              <p class="message {{ category }}">{{ message }}</p>
            {% endfor %}
          </div>
        {% endif %}
      {% endwith %}

      <form method="post" id="collect-form">
        <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
        <div class="field span2">
          <label for="krx_id">KRX 아이디</label>
          <input id="krx_id" name="krx_id" value="{{ form.krx_id }}" autocomplete="username" required>
          <span class="help">KRX 정보데이터시스템 계정 아이디입니다.</span>
        </div>
        <div class="field span2">
          <label for="krx_pw">KRX 비밀번호</label>
          <input id="krx_pw" name="krx_pw" type="password" autocomplete="current-password" required>
          <span class="help">조회 때만 사용하고 파일에 저장하지 않습니다.</span>
        </div>
        <div class="field span2">
          <label for="as_of">기준일</label>
          <input id="as_of" name="as_of" type="date" value="{{ form.as_of }}" required>
          <span class="help">기본값은 오늘입니다. 기존 노트북의 오늘 기준 실행과 같습니다.</span>
        </div>
        <div class="field span2">
          <label for="market">시장</label>
          <select id="market" name="market">
            {% for market in markets %}
              <option value="{{ market }}" {% if form.market == market %}selected{% endif %}>{{ market }}</option>
            {% endfor %}
          </select>
          <span class="help">전체, KOSPI, KOSDAQ, KONEX 중 조회 범위입니다.</span>
        </div>
        <div class="field span2">
          <button type="submit">전체 분석 실행</button>
          <span class="help">클릭하면 데이터 수집, 통합 엑셀 생성, 기본 차트 생성이 한 번에 진행됩니다.</span>
        </div>

        <div class="fixed-scope">
          <strong>분석 기간은 자동 고정됩니다.</strong>
          기준일 기준 6개월 누적, 3개월 누적, 1개월 누적을 조회하고, 1개월보다 짧은 구간은 기존 노트북처럼 최근 거래일 5개를 일별로 조회합니다. 주말과 휴장일은 프로그램이 자동으로 건너뜁니다.
          Vercel에서는 결과 파일이 임시 저장되므로 분석 완료 직후 내려받는 것을 권장합니다.
        </div>
      </form>

      {% if result %}
        <h2 class="dashboard-title">노트북형 자동 분석 결과</h2>
        <dl class="result">
          <div class="metric"><dt>Base 행수</dt><dd>{{ "{:,}".format(result.base_rows) }}</dd></div>
          <div class="metric"><dt>Last 행수</dt><dd>{{ "{:,}".format(result.last_rows) }}</dd></div>
          <div class="metric"><dt>최근 거래일 분석</dt><dd>{{ result.trading_dates | length }}개</dd></div>
          <div class="metric"><dt>저장 파일</dt><dd>완료</dd></div>
        </dl>
        <p class="sub">자동 분석 범위: 기관합계/외국인 6개월 누적, 3개월 누적, 1개월 누적, 최근 거래일 {{ result.trading_dates | length }}개 일별 데이터. 최근 거래일: {{ ", ".join(result.trading_dates) }}</p>
        <a class="download" href="/download/{{ result.download_token }}">엑셀 다운로드</a>

        {% if result.charts %}
          <section class="visuals">
            {% for chart in result.charts %}
              <article class="chart-card">
                <h3>{{ chart.title }}</h3>
                <img src="data:image/png;base64,{{ chart.image }}" alt="{{ chart.title }}">
              </article>
            {% endfor %}
          </section>
        {% endif %}

        {% if result.top_rows %}
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  {% for key in result.top_rows[0].keys() %}
                    <th>{{ key }}</th>
                  {% endfor %}
                </tr>
              </thead>
              <tbody>
                {% for row in result.top_rows %}
                  <tr>
                    {% for value in row.values() %}
                      <td>{{ "{:,}".format(value) if value is number else value }}</td>
                    {% endfor %}
                  </tr>
                {% endfor %}
              </tbody>
            </table>
          </div>
        {% endif %}
      {% endif %}

      <section class="guide">
        <h2>처음 사용하는 사람 준비 방법</h2>
        <ol>
          <li><a href="https://data.krx.co.kr/" target="_blank" rel="noopener">KRX 정보데이터시스템</a>에서 회원가입 후 아이디와 비밀번호를 준비합니다.</li>
          <li>이 화면에 KRX 아이디와 비밀번호를 입력합니다. 비밀번호는 해당 조회 요청 동안만 사용됩니다.</li>
          <li>기준일과 시장을 선택한 뒤 전체 분석 실행을 누르면 누적·최근 거래일 데이터가 수집됩니다.</li>
          <li>분석이 끝나면 차트와 주요 종목을 확인하고 통합 Excel 파일을 내려받습니다.</li>
        </ol>
        <p class="note">이 버전은 Selenium이나 별도의 OpenAPI 키 없이 KRX 로그인 세션과 정보데이터시스템 JSON 요청을 사용합니다. KRX 사이트 정책이나 요청 형식이 변경되면 수집 모듈도 함께 수정해야 합니다.</p>
      </section>
    </section>
  </main>
  <div class="loading" id="loading">
    <div class="loading-box">
      <p class="loading-title">KRX 데이터를 수집하고 있습니다</p>
      <p class="sub">로그인, 6개월/3개월/1개월 수집, 최근 거래일 자동 탐색, 시각화 생성 순서로 진행됩니다.</p>
      <div class="bar"><div class="bar-fill" id="bar-fill"></div></div>
      <div class="percent" id="percent">0%</div>
    </div>
  </div>
  <script>
    const form = document.getElementById("collect-form");
    const loading = document.getElementById("loading");
    const fill = document.getElementById("bar-fill");
    const percent = document.getElementById("percent");
    if (form) {
      form.addEventListener("submit", () => {
        const submitButton = form.querySelector('button[type="submit"]');
        if (submitButton) {
          submitButton.disabled = true;
          submitButton.textContent = "분석 실행 중...";
        }
        loading.classList.add("active");
        let value = 0;
        const timer = setInterval(() => {
          const step = value < 45 ? 4 : value < 75 ? 2 : 1;
          value = Math.min(value + step, 95);
          fill.style.width = value + "%";
          percent.textContent = value + "%";
          if (value >= 95) clearInterval(timer);
        }, 450);
      });
    }
  </script>
</body>
</html>
"""

DOC_TEMPLATE = """
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>프로그램 상세 설명</title>
  <style>
    body {
      margin: 0;
      background: #f3f6f5;
      color: #1d282b;
      font-family: "Segoe UI", "Malgun Gothic", Arial, sans-serif;
      line-height: 1.65;
    }
    main {
      width: min(960px, calc(100% - 32px));
      margin: 0 auto;
      padding: 32px 0 56px;
    }
    article {
      background: #fff;
      border: 1px solid #d8e2e3;
      border-radius: 8px;
      padding: 30px;
      box-shadow: 0 16px 36px rgba(29, 40, 43, .08);
    }
    h1, h2, h3 { letter-spacing: 0; line-height: 1.3; }
    h1 { margin-top: 0; font-size: 34px; }
    h2 { margin-top: 30px; padding-top: 18px; border-top: 1px solid #d8e2e3; }
    code, pre {
      font-family: Consolas, "Courier New", monospace;
      background: #eef5f4;
      border-radius: 6px;
    }
    code { padding: 2px 5px; }
    pre { padding: 14px; overflow: auto; }
    a { color: #096653; font-weight: 800; }
    .back {
      display: inline-flex;
      margin-bottom: 16px;
      min-height: 42px;
      align-items: center;
      padding: 0 16px;
      border-radius: 6px;
      background: #0b8069;
      color: #fff;
      text-decoration: none;
      font-weight: 900;
    }
  </style>
</head>
<body>
  <main>
    <a class="back" href="/">분석 화면으로 돌아가기</a>
    <article>{{ content|safe }}</article>
  </main>
</body>
</html>
"""


def default_form() -> dict[str, Any]:
    return {
        "krx_id": "",
        "as_of": date.today().isoformat(),
        "market": "ALL",
    }


@app.route("/", methods=["GET", "POST"])
def index():
    form = default_form()
    result = None

    if flask_request.method == "POST":
        form.update({
            "krx_id": flask_request.form.get("krx_id", "").strip(),
            "as_of": flask_request.form.get("as_of", form["as_of"]).strip(),
            "market": flask_request.form.get("market", "ALL").strip().upper(),
        })
        krx_pw = flask_request.form.get("krx_pw", "")

        try:
            validate_csrf_token(flask_request.form.get("csrf_token", ""))
            result = run_collection(
                krx_id=form["krx_id"],
                krx_pw=krx_pw,
                as_of=parse_yyyymmdd(form["as_of"]),
                market=form["market"],
                output_root=OUTPUT_ROOT,
            )
            result = RunResult(
                output_path=result.output_path,
                base_rows=result.base_rows,
                last_rows=result.last_rows,
                trading_dates=result.trading_dates,
                charts=result.charts,
                top_rows=result.top_rows,
                download_token=register_download(result.output_path),
            )
            flash("수집과 분석이 완료되었습니다. 엑셀 파일은 30분 동안 다운로드할 수 있습니다.", "success")
        except (ValueError, RuntimeError) as exc:
            flash(str(exc), "error")
        except Exception:
            app.logger.exception("Unexpected collection failure")
            flash("처리 중 예상하지 못한 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.", "error")

    return render_template_string(
        PAGE_TEMPLATE,
        form=form,
        markets=MARKET_CODES.keys(),
        result=result,
        csrf_token=get_csrf_token(),
    )


@app.route("/download/<token>")
def download(token: str):
    entry = DOWNLOADS.get(token)
    if not entry:
        return "다운로드 링크가 만료되었거나 다른 서버 인스턴스로 전환되었습니다. 분석을 다시 실행해 주세요.", 404

    output_path, created_at = entry
    if time.time() - created_at > DOWNLOAD_TTL_SECONDS or not output_path.exists():
        DOWNLOADS.pop(token, None)
        return "다운로드 링크가 만료되었습니다. 분석을 다시 실행해 주세요.", 410
    return send_file(output_path, as_attachment=True, download_name=output_path.name)


@app.errorhandler(413)
def request_too_large(_error):
    return "요청 크기가 허용 범위를 초과했습니다.", 413


@app.route("/health")
def health():
    return {
        "status": "ok",
        "runtime": "vercel" if IS_VERCEL else "local",
        "recent_trading_days": RECENT_TRADING_DAYS,
    }


def markdown_to_html(markdown_text: str) -> str:
    html_parts = []
    in_list = False
    in_code = False
    code_lines = []

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            html_parts.append("</ul>")
            in_list = False

    for raw_line in markdown_text.splitlines():
        line = raw_line.rstrip()

        if line.startswith("```"):
            if in_code:
                html_parts.append(f"<pre><code>{escape(chr(10).join(code_lines))}</code></pre>")
                code_lines = []
                in_code = False
            else:
                close_list()
                in_code = True
            continue

        if in_code:
            code_lines.append(line)
            continue

        if not line:
            close_list()
            continue

        if line.startswith("# "):
            close_list()
            html_parts.append(f"<h1>{escape(line[2:])}</h1>")
        elif line.startswith("## "):
            close_list()
            html_parts.append(f"<h2>{escape(line[3:])}</h2>")
        elif line.startswith("### "):
            close_list()
            html_parts.append(f"<h3>{escape(line[4:])}</h3>")
        elif line.startswith("- "):
            if not in_list:
                html_parts.append("<ul>")
                in_list = True
            html_parts.append(f"<li>{escape(line[2:])}</li>")
        elif re.match(r"^\d+\. ", line):
            close_list()
            text = re.sub(r"^\d+\. ", "", line)
            html_parts.append(f"<p>{escape(text)}</p>")
        else:
            close_list()
            safe_line = escape(line)
            if line.startswith("http://") or line.startswith("https://"):
                safe_line = f'<a href="{safe_line}" target="_blank" rel="noopener">{safe_line}</a>'
            html_parts.append(f"<p>{safe_line}</p>")

    close_list()
    return "\n".join(str(part) for part in html_parts)


@app.route("/docs")
def docs():
    if DOC_PATH.exists():
        content = DOC_PATH.read_text(encoding="utf-8")
    else:
        content = "# 프로그램 상세 설명\n\n문서 파일을 찾을 수 없습니다."
    return render_template_string(DOC_TEMPLATE, content=markdown_to_html(content))


def open_browser(port: int) -> None:
    webbrowser.open_new(f"http://127.0.0.1:{port}")


if __name__ == "__main__":
    port = int(os.environ.get("GHJ_PORT", "5000"))
    threading.Timer(1.0, open_browser, args=(port,)).start()
    app.run(host="127.0.0.1", port=port, debug=False)
