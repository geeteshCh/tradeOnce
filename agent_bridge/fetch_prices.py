"""Fetch real daily closes from Yahoo Finance (free, no key) into recorded/closes.json.

    cd agent_bridge && uv run python fetch_prices.py            # defaults below
    uv run python fetch_prices.py --start 2024-06-03 --end 2024-06-28 --tickers AAPL,AMZN,MSFT,NVDA

Run once, commit the file: every later run (alone or through TradeOnce) reads the same recorded prices,
so results are identical and need no network. SPY is always added as the agent's benchmark.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from pathlib import Path

import yfinance as yf

OUT = Path(__file__).parent / "recorded" / "closes.json"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2024-06-03")
    ap.add_argument("--end", default="2024-06-07", help="inclusive")
    ap.add_argument("--tickers", default="AAPL,AMZN,MSFT,NVDA", help="stocks, or crypto as Alpaca writes it: ETH/USD")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()

    tickers = sorted({t.strip().upper() for t in args.tickers.split(",") if t.strip()} | {"SPY"})
    yahoo = {t: t.replace("/", "-") for t in tickers}          # Yahoo writes crypto as ETH-USD
    end_exclusive = (date.fromisoformat(args.end) + timedelta(days=1)).isoformat()
    df = yf.download(list(yahoo.values()), start=args.start, end=end_exclusive, progress=False, auto_adjust=False)["Close"]
    closes: dict[str, dict[str, float]] = {}
    for ts, row in df.iterrows():
        day = ts.date().isoformat()
        if row[yahoo["SPY"]] != row[yahoo["SPY"]]:              # keep the agent's trading-day grid (SPY's days)
            continue
        closes[day] = {t: round(float(row[y]), 2) for t, y in yahoo.items() if row[y] == row[y]}  # skip NaN
    if not closes:
        raise SystemExit("yfinance returned no data; check tickers and dates")
    payload = {"_note": f"Real daily closes (unadjusted) from Yahoo Finance via yfinance, {args.start} to {args.end}. "
                        "Fetched once and frozen so every run is identical.", **closes}
    args.out.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"wrote {len(closes)} days x {len(tickers)} tickers to {args.out}")


if __name__ == "__main__":
    main()
