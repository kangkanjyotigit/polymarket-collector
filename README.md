# Polymarket arbitrage collector

An async collector that polls Polymarket's Gamma API, validates order-book
conditions, and raises alerts when a market clears a profitability threshold.

## What it does

- Polls the Polymarket Gamma API asynchronously (`asyncio` + `httpx`)
- Screens markets against a **minimum liquidity floor** before considering them
- Detects arbitrage opportunities and computes ROI **net of a configurable trading fee**
- Emits structured, rotating logs (`loguru`, 10 MB rotation) plus a dedicated
  high-ROI alert sink
- Exposes the collected data as pandas frames for downstream analysis

## Configuration

Thresholds live at the top of the module and are meant to be tuned:

| Setting | Meaning |
|---|---|
| `HIGH_ROI_ALERT_THRESHOLD` | ROI above which an alert fires |
| `MIN_LIQUIDITY_USD` | Order-book liquidity floor |
| `TRADING_FEE` | Fee applied to P/L before ROI is computed |

## Run

```bash
pip install -r requirements.txt
python polymarket_data_collector.py
```

## Notes

Read-only market-data collection. No wallet, no order placement, no keys required.
