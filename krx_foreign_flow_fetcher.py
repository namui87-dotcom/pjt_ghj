import json
import re
from dataclasses import dataclass
from http.cookiejar import CookieJar
from typing import Any
from urllib import parse, request
from urllib.error import HTTPError


GET_JSON_URL = "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
LOGIN_PAGE_URL = "https://data.krx.co.kr/contents/MDC/COMS/client/MDCCOMS001.cmd"
LOGIN_JSP_URL = "https://data.krx.co.kr/contents/MDC/COMS/client/view/login.jsp?site=mdc"
LOGIN_URL = "https://data.krx.co.kr/contents/MDC/COMS/client/MDCCOMS001D1.cmd"

MARKET_IDS = {
    "ALL": "ALL",
    "KOSPI": "STK",
    "KOSDAQ": "KSQ",
    "KONEX": "KNX",
}


@dataclass(frozen=True)
class ForeignFlowRequest:
    trade_date: str
    market: str = "KOSPI"
    stock_code: str = ""


class KrxForeignFlowFetcher:
    def __init__(self, krx_id: str, krx_pw: str, timeout: int = 20):
        self.krx_id = krx_id
        self.krx_pw = krx_pw
        self.timeout = timeout
        self.cookie_jar = CookieJar()
        self.opener = request.build_opener(request.HTTPCookieProcessor(self.cookie_jar))

        self.market_flow_bld = "dbms/MDC/STAT/standard/MDCSTAT02201"
        self.stock_flow_bld = "dbms/MDC/STAT/standard/MDCSTAT02301"
        self.issue_master_bld = "dbms/MDC/STAT/standard/MDCSTAT01901"

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
            message = data.get("_error_message") or "KRX login failed."
            raise RuntimeError(f"KRX 로그인 실패: {message}")

    def fetch(self, req: ForeignFlowRequest) -> dict[str, Any]:
        market = req.market.upper()
        if market not in MARKET_IDS:
            raise ValueError("시장은 ALL, KOSPI, KOSDAQ, KONEX 중 하나여야 합니다.")

        trade_date = re.sub(r"\D", "", req.trade_date)
        if not re.fullmatch(r"\d{8}", trade_date):
            raise ValueError("조회일은 YYYYMMDD 또는 YYYY-MM-DD 형식이어야 합니다.")

        stock_code = req.stock_code.strip()

        if stock_code:
            if not re.fullmatch(r"\d{6}", stock_code):
                raise ValueError("종목코드는 숫자 6자리여야 합니다.")

            isin = self._find_isin(stock_code, market)
            payload = {
                "bld": self.stock_flow_bld,
                "strtDd": trade_date,
                "endDd": trade_date,
                "isuCd": isin,
                "inqTpCd": "1",
                "trdVolVal": "1",
                "askBid": "1",
            }
        else:
            payload = {
                "bld": self.market_flow_bld,
                "strtDd": trade_date,
                "endDd": trade_date,
                "mktId": MARKET_IDS[market],
                "etf": "",
                "etn": "",
                "elw": "",
            }

        data = self._post_json(payload)
        rows = self._extract_rows(data)
        foreign_rows = [row for row in rows if self._is_foreign_row(row)]

        return {
            "date": trade_date,
            "market": market,
            "stock_code": stock_code,
            "row_count": len(rows),
            "foreign_rows": foreign_rows,
            "summary": self._summarize(foreign_rows),
            "all_rows": rows,
        }

    def _find_isin(self, stock_code: str, market: str) -> str:
        data = self._post_json({
            "bld": self.issue_master_bld,
            "mktId": MARKET_IDS[market],
            "segTpCd": "ALL",
        })

        for row in self._extract_rows(data):
            if row.get("ISU_SRT_CD") == stock_code:
                return row.get("ISU_CD", "")

        raise RuntimeError(f"KRX 종목 마스터에서 {stock_code} 종목을 찾지 못했습니다.")

    def _post_json(self, payload: dict[str, str]) -> dict[str, Any]:
        payload.setdefault("locale", "ko_KR")

        req = request.Request(
            GET_JSON_URL,
            data=parse.urlencode(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://data.krx.co.kr/contents/MDC/MDI/outerLoader/index.cmd",
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
                raise RuntimeError("KRX 로그인 세션이 만료되었거나 로그인에 실패했습니다.")
            raise

        if raw.strip() == "LOGOUT":
            raise RuntimeError("KRX 로그인 세션이 만료되었거나 로그인에 실패했습니다.")

        return json.loads(raw)

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

    @staticmethod
    def _extract_rows(data: dict[str, Any]) -> list[dict[str, str]]:
        for key in ("output", "OutBlock_1", "block1"):
            rows = data.get(key)
            if isinstance(rows, list):
                return [
                    {str(k).strip(): str(v).strip() for k, v in row.items()}
                    for row in rows
                    if isinstance(row, dict)
                ]
        return []

    @staticmethod
    def _is_foreign_row(row: dict[str, str]) -> bool:
        text = " ".join(row.values())
        return "외국인" in text or "외인" in text

    @staticmethod
    def _to_int(value: str) -> int | None:
        cleaned = re.sub(r"[^0-9\-]", "", value or "")
        if not cleaned or cleaned == "-":
            return None
        return int(cleaned)

    def _summarize(self, rows: list[dict[str, str]]) -> dict[str, int]:
        columns = {
            "ASK_TRDVOL": "sell_volume",
            "BID_TRDVOL": "buy_volume",
            "NETBID_TRDVOL": "net_buy_volume",
            "ASK_TRDVAL": "sell_value",
            "BID_TRDVAL": "buy_value",
            "NETBID_TRDVAL": "net_buy_value",
        }

        summary: dict[str, int] = {}

        for row in rows:
            for source_col, target_col in columns.items():
                number = self._to_int(row.get(source_col, ""))
                if number is not None:
                    summary[target_col] = summary.get(target_col, 0) + number

        return summary
