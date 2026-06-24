"""Vercel and local WSGI entrypoint."""

import os

from ghj_codex_V_03 import app


if __name__ == "__main__":
    port = int(os.environ.get("PORT", os.environ.get("GHJ_PORT", "5000")))
    app.run(host="127.0.0.1", port=port, debug=False)
