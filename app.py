import os
from datetime import date

from flask import Flask, flash, render_template, request

from krx_foreign_flow_fetcher import ForeignFlowRequest, KrxForeignFlowFetcher, MARKET_IDS


app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "change-this-secret-key")


SUMMARY_LABELS = {
    "sell_volume": "매도수량",
    "buy_volume": "매수수량",
    "net_buy_volume": "순매수수량",
    "sell_value": "매도금액",
    "buy_value": "매수금액",
    "net_buy_value": "순매수금액",
}


def normalize_date(value: str) -> str:
    digits = "".join(ch for ch in value if ch.isdigit())
    if len(digits) == 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
    return value


@app.template_filter("number")
def number_filter(value):
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return value


@app.route("/", methods=["GET", "POST"])
def index():
    today = date.today().isoformat()
    form = {
        "krx_id": "",
        "krx_pw": "",
        "trade_date": today,
        "market": "KOSPI",
        "stock_code": "",
    }
    result = None

    if request.method == "POST":
        form.update({
            "krx_id": request.form.get("krx_id", "").strip(),
            "krx_pw": request.form.get("krx_pw", ""),
            "trade_date": request.form.get("trade_date", today).strip(),
            "market": request.form.get("market", "KOSPI").strip().upper(),
            "stock_code": request.form.get("stock_code", "").strip(),
        })

        if not form["krx_id"] or not form["krx_pw"]:
            flash("KRX 아이디와 비밀번호를 모두 입력해야 조회할 수 있습니다.", "error")
        else:
            try:
                fetcher = KrxForeignFlowFetcher(form["krx_id"], form["krx_pw"])
                result = fetcher.fetch(
                    ForeignFlowRequest(
                        trade_date=form["trade_date"],
                        market=form["market"],
                        stock_code=form["stock_code"],
                    )
                )
                result["display_date"] = normalize_date(result["date"])
                flash("조회가 완료되었습니다.", "success")
            except Exception as exc:
                flash(str(exc), "error")

    return render_template(
        "index.html",
        form=form,
        markets=MARKET_IDS.keys(),
        result=result,
        summary_labels=SUMMARY_LABELS,
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
