"""
dashboard/api_server.py — FastAPI dashboard serving live bot state.

Endpoints:
  GET /health          — liveness probe
  GET /state           — mode, bankroll, PnL, positions, wallet summary
  GET /live-markets    — active markets with signal data
  GET /whale-activity  — last 50 whale events
  GET /trades          — paper trade history
  GET /signals         — last 100 signals generated
  GET /analytics       — full performance metrics (Sharpe, drawdown, etc.)
  GET /equity-curve    — NAV history for chart
  GET /journal         — trade journal with full signal context
  GET /wallet          — wallet risk state (cash, exposure, cooldown, daily loss)

Runs on port 8090 via uvicorn.  Static files in dashboard/static/.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from core import database
from execution.analytics import compute_analytics

if TYPE_CHECKING:
    from core.interfaces import MarketFeed, WhaleDetectorInterface
    from execution.order_executor import PaperOrderExecutor
    from execution.paper_wallet import PaperWallet
    from execution.position_manager import PositionManager

logger = logging.getLogger(__name__)

app = FastAPI(title="Polymarket Crypto Agents", version="0.2.0")

_state: dict[str, Any] = {
    "mode": "unknown",
    "start_ts": time.time(),
    "market_feed": None,
    "whale_detector": None,
    "position_manager": None,
    "wallet": None,
}


def configure(
    mode: str,
    market_feed: "MarketFeed",
    whale_detector: "WhaleDetectorInterface",
    position_manager: "PositionManager",
    wallet: "PaperWallet",
) -> None:
    _state["mode"] = mode
    _state["start_ts"] = time.time()
    _state["market_feed"] = market_feed
    _state["whale_detector"] = whale_detector
    _state["position_manager"] = position_manager
    _state["wallet"] = wallet


# ── Static ────────────────────────────────────────────────────────────────────

_static_dir = Path(__file__).parent / "static"
if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


@app.get("/")
async def index():
    index_path = _static_dir / "index.html"
    if index_path.exists():
        return FileResponse(str(index_path))
    return JSONResponse({"message": "Polymarket Crypto Agents", "docs": "/docs"})


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "uptime_s": round(time.time() - _state["start_ts"])}


@app.get("/state")
async def state():
    pm: PositionManager | None = _state.get("position_manager")
    wallet: PaperWallet | None = _state.get("wallet")
    open_trades = pm.get_open_trades() if pm else []
    unrealized = pm.get_unrealized_pnl() if pm else 0.0
    db_trades = await database.get_all_trades(limit=5_000)
    realized_pnl = sum(float(r["pnl"]) for r in db_trades if r["status"] == "CLOSED")
    nav = wallet.nav(unrealized) if wallet else 0.0
    return {
        "mode": _state["mode"],
        "nav": round(nav, 2),
        "cash": round(wallet.cash, 2) if wallet else 0.0,
        "open_positions": len(open_trades),
        "realized_pnl": round(realized_pnl, 2),
        "unrealized_pnl": round(unrealized, 2),
        "total_pnl": round(realized_pnl + unrealized, 2),
        "uptime_s": round(time.time() - _state["start_ts"]),
    }


@app.get("/wallet")
async def wallet_state():
    wallet: PaperWallet | None = _state.get("wallet")
    pm: PositionManager | None = _state.get("position_manager")
    if wallet is None:
        return {}
    unrealized = pm.get_unrealized_pnl() if pm else 0.0
    return wallet.summary(unrealized)


@app.get("/live-markets")
async def live_markets():
    mf = _state.get("market_feed")
    if not mf:
        return []
    return [
        {
            "condition_id": m.condition_id,
            "question": m.question,
            "crypto_symbol": m.crypto_symbol,
            "implied_prob": round(m.implied_prob, 4),
            "best_bid": round(m.best_bid, 4),
            "best_ask": round(m.best_ask, 4),
            "spread": round(m.spread, 4),
            "volume_24h": round(m.volume_24h, 0),
            "open_interest": round(m.open_interest, 0),
            "time_to_expiry_h": round(m.time_to_expiry_s / 3600, 1),
            "fraction_elapsed": round(m.fraction_of_life_elapsed, 3),
        }
        for m in mf.get_active_markets()
    ]


@app.get("/whale-activity")
async def whale_activity():
    rows = await database.get_recent_whale_events(limit=50)
    return [
        {
            "trade_id": r["trade_id"],
            "condition_id": r["condition_id"],
            "direction": r["direction"],
            "size_usd": round(float(r["size_usd"]), 2),
            "price": round(float(r["price"]), 4),
            "timestamp": r["timestamp"],
        }
        for r in rows
    ]


@app.get("/trades")
async def trades():
    rows = await database.get_all_trades(limit=200)
    pm: PositionManager | None = _state.get("position_manager")
    open_live = {t.id: t for t in (pm.get_open_trades() if pm else [])}

    result = []
    for r in rows:
        live = open_live.get(r["id"])
        result.append({
            "id": r["id"],
            "condition_id": r["condition_id"],
            "side": r["side"],
            "size_usd": round(float(r["size_usd"]), 2),
            "entry_price": round(float(r["entry_price"]), 4),
            "current_price": round(live.current_price, 4) if live else None,
            "exit_price": round(float(r["exit_price"]), 4) if r["exit_price"] else None,
            "pnl": round(float(r["pnl"]), 2),
            "unrealized_pnl": round(live.unrealized_pnl, 2) if live else None,
            "status": r["status"],
            "close_reason": r["close_reason"],
            "fill_fraction": round(float(r["fill_fraction"] or 1.0), 2),
            "slippage_usd": round(float(r["slippage_usd"] or 0), 4),
            "hold_time_s": round(float(r["hold_time_s"]), 0) if r["hold_time_s"] else None,
            "open_ts": r["open_ts"],
            "close_ts": r["close_ts"],
        })
    return result


@app.get("/signals")
async def signals():
    rows = await database.get_recent_signals(limit=100)
    return [
        {
            "condition_id": r["condition_id"],
            "crypto_symbol": r["crypto_symbol"],
            "delta": round(float(r["delta"]), 4),
            "ev_net": round(float(r["ev_net"]), 4),
            "our_prob": round(float(r["our_prob"]), 4),
            "market_prob": round(float(r["market_prob"]), 4),
            "whale_score": round(float(r["whale_score"]), 3),
            "llm_validated": bool(r["llm_validated"]),
            "llm_reason": r["llm_reason"],
            "quality_score": round(float(r["quality_score"]), 3),
            "timestamp": r["timestamp"],
        }
        for r in rows
    ]


@app.get("/analytics")
async def analytics():
    wallet: PaperWallet | None = _state.get("wallet")
    pm: PositionManager | None = _state.get("position_manager")
    if wallet is None:
        return {}
    unrealized = pm.get_unrealized_pnl() if pm else 0.0
    nav = wallet.nav(unrealized)
    open_count = pm.get_open_count() if pm else 0
    result = await compute_analytics(
        wallet_nav=nav,
        wallet_cash=wallet.cash,
        initial_bankroll=wallet.initial_bankroll,
        open_count=open_count,
    )
    return {
        "total_trades": result.total_trades,
        "open_trades": result.open_trades,
        "win_rate_pct": round(result.win_rate * 100, 1),
        "profit_factor": result.profit_factor,
        "expectancy_usd": result.expectancy,
        "total_pnl": result.total_pnl,
        "total_fees_paid": result.total_fees_paid,
        "total_slippage": result.total_slippage,
        "sharpe_ratio": result.sharpe_ratio,
        "max_drawdown_pct": result.max_drawdown_pct,
        "max_drawdown_usd": result.max_drawdown_usd,
        "current_drawdown_pct": result.current_drawdown_pct,
        "avg_hold_time_s": result.avg_hold_time_s,
        "median_hold_time_s": result.median_hold_time_s,
        "current_streak": result.current_streak,
        "max_win_streak": result.max_win_streak,
        "max_loss_streak": result.max_loss_streak,
        "nav": result.nav,
        "cash": result.cash,
        "initial_bankroll": result.initial_bankroll,
        "total_return_pct": result.total_return_pct,
        "closes_by_reason": result.closes_by_reason,
        "by_symbol": result.by_symbol,
    }


@app.get("/equity-curve")
async def equity_curve():
    rows = await database.get_equity_curve(limit=500)
    return [
        {
            "timestamp": r["timestamp"],
            "nav": round(float(r["nav"]), 2),
            "cash": round(float(r["cash"]), 2),
            "unrealized_pnl": round(float(r["unrealized_pnl"]), 2),
            "realized_pnl_cumulative": round(float(r["realized_pnl_cumulative"]), 2),
            "open_positions": r["open_positions"],
        }
        for r in rows
    ]


@app.get("/journal")
async def journal():
    rows = await database.get_journal(limit=100)
    return [
        {
            "trade_id": r["trade_id"],
            "crypto_symbol": r["crypto_symbol"],
            "side": r["side"],
            "delta": round(float(r["delta"]), 4),
            "ev_net": round(float(r["ev_net"]), 4),
            "our_prob": round(float(r["our_prob"]), 4),
            "market_prob": round(float(r["market_prob"]), 4),
            "whale_score": round(float(r["whale_score"]), 3),
            "depth_score": round(float(r["depth_score"]), 3),
            "requested_price": round(float(r["requested_price"]), 4),
            "fill_price": round(float(r["fill_price"]), 4),
            "slippage": round(float(r["slippage"]), 5),
            "fill_fraction": round(float(r["fill_fraction"]), 3),
            "size_usd": round(float(r["size_usd"]), 2),
            "fee_usd": round(float(r["fee_usd"]), 3),
            "close_reason": r["close_reason"],
            "hold_time_s": round(float(r["hold_time_s"]), 0) if r["hold_time_s"] else None,
            "pnl": round(float(r["pnl"]), 2) if r["pnl"] is not None else None,
            "max_favorable_excursion": round(float(r["max_favorable_excursion"]), 2)
                if r["max_favorable_excursion"] is not None else None,
            "max_adverse_excursion": round(float(r["max_adverse_excursion"]), 2)
                if r["max_adverse_excursion"] is not None else None,
            "open_ts": r["open_ts"],
        }
        for r in rows
    ]
