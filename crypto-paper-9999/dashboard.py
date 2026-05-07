"""
Comparison Dashboard - Port 9999
Shows daily P&L stats for all trading ports in a spreadsheet-like table.
"""
from flask import Flask, jsonify, request
import sqlite3
import os
import json
import requests as req
from datetime import datetime, timedelta

DEFAULT_START = "2026-05-07 05:00:00"
DAY_START_HOUR = 5

app = Flask(__name__)

PORTS = [8090, 8091, 8092, 8093, 8094, 8095, 8096, 8097, 8098, 8099, 9000, 9001, 9002, 9010, 9011]

def get_db_path(port):
    return f"/opt/crypto-paper-{port}/paper_{port}.db"

def get_config(port):
    try:
        with open(f"/opt/crypto-paper-{port}/config.json") as f:
            return json.load(f)
    except Exception:
        return {}

def query_port(port, start_date=None):
    db = get_db_path(port)
    if not os.path.exists(db):
        return None
    try:
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row

        cfg = get_config(port)
        amount = cfg.get("trading", {}).get("amount_per_trade_eur", 0)
        mode = cfg.get("trading", {}).get("mode", "demo")
        window = cfg.get("detection", {}).get("pump_window_seconds", 0)

        date_filter = ""
        params = []
        if start_date:
            date_filter = " AND exit_time_str >= ?"
            params = [start_date]

        # Total stats (filtered by start_date)
        row = conn.execute("""
            SELECT
                COUNT(*) as total_closed,
                COALESCE(SUM(pnl_eur), 0) as total_pnl,
                COALESCE(SUM(CASE WHEN pnl_eur > 0 THEN 1 ELSE 0 END), 0) as wins,
                COALESCE(SUM(CASE WHEN pnl_eur <= 0 THEN 1 ELSE 0 END), 0) as losses
            FROM trades WHERE status='CLOSED'""" + date_filter, params).fetchone()

        total_closed = row["total_closed"]
        total_pnl = round(row["total_pnl"], 2)
        wins = row["wins"]
        losses = row["losses"]
        win_rate = round(wins / total_closed * 100, 1) if total_closed > 0 else 0

        # Current open
        open_count = conn.execute("SELECT COUNT(*) as c FROM trades WHERE status='OPEN'").fetchone()["c"]

        # Daily breakdown (filtered by start_date, day starts at 5:00 AM)
        days = conn.execute("""
            SELECT
                DATE(exit_time_str, '-5 hours') as day,
                COUNT(*) as trades,
                ROUND(SUM(pnl_eur), 2) as pnl,
                SUM(CASE WHEN pnl_eur > 0 THEN 1 ELSE 0 END) as w,
                SUM(CASE WHEN pnl_eur <= 0 THEN 1 ELSE 0 END) as l
            FROM trades
            WHERE status='CLOSED' AND exit_time_str IS NOT NULL""" + date_filter + """
            GROUP BY DATE(exit_time_str, '-5 hours')
            ORDER BY day
        """, params).fetchall()

        # Per-day max simultaneous (sampled every 5th trade for speed)
        day_max_sim = {}
        try:
            sim_q = """
                SELECT DATE(t1.entry_time_str, '-5 hours') as day,
                    MAX((SELECT COUNT(*) FROM trades t2
                        WHERE t2.entry_time <= t1.entry_time
                        AND (t2.exit_time IS NULL OR t2.exit_time > t1.entry_time)
                    )) as mx
                FROM trades t1
                WHERE t1.id % 5 = 0"""
            sim_p = []
            if start_date:
                sim_q += " AND t1.entry_time_str >= ?"
                sim_p = [start_date]
            sim_q += " GROUP BY DATE(t1.entry_time_str, '-5 hours')"
            for r in conn.execute(sim_q, sim_p).fetchall():
                day_max_sim[r["day"]] = r["mx"] or 0
        except Exception:
            pass

        daily = {}
        for d in days:
            wr = round(d["w"] / d["trades"] * 100, 1) if d["trades"] > 0 else 0
            ms = day_max_sim.get(d["day"], 0)
            mc = ms * amount
            daily[d["day"]] = {
                "pnl": d["pnl"],
                "trades": d["trades"],
                "wins": d["w"],
                "losses": d["l"],
                "win_rate": wr,
                "max_sim": ms,
                "max_capital": round(mc, 0)
            }

        # Max simultaneous (sampled, filtered)
        sim_filter = " AND entry_time_str >= ?" if start_date else ""
        sim_params = [start_date] if start_date else []
        max_sim = conn.execute("""
            SELECT MAX(oc) as mx FROM (
                SELECT (SELECT COUNT(*) FROM trades t2
                    WHERE t2.entry_time <= t1.entry_time
                    AND (t2.exit_time IS NULL OR t2.exit_time > t1.entry_time)
                ) as oc
                FROM trades t1 WHERE id % 20 = 0""" + sim_filter + """
            )
        """, sim_params).fetchone()["mx"] or 0

        max_capital = max_sim * amount

        conn.close()
        return {
            "port": port,
            "mode": mode,
            "amount": amount,
            "window": window,
            "total_pnl": total_pnl,
            "total_closed": total_closed,
            "open_count": open_count,
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
            "max_simultaneous": max_sim,
            "max_capital": round(max_capital, 0),
            "daily": daily
        }
    except Exception as e:
        return {"port": port, "error": str(e)}

@app.route("/")
def index():
    return HTML

LIVE_PORTS = [9000, 9010]

@app.route("/api/portfolio")
def api_portfolio():
    total_invested = 0
    total_unrealized = 0
    total_realized = 0
    usdc_balance = None
    port_details = []
    for port in LIVE_PORTS:
        try:
            r = req.get(f"http://127.0.0.1:{port}/api/data", timeout=5)
            d = r.json()
            p = d.get("portfolio", {})
            inv = p.get("total_invested", 0) or 0
            unr = p.get("unrealized_pnl", 0) or 0
            rea = p.get("realized_pnl", 0) or 0
            opn = d.get("stats", {}).get("open_trades", 0) or 0
            total_invested += inv
            total_unrealized += unr
            total_realized += rea
            if d.get("usdc_balance") is not None:
                usdc_balance = d["usdc_balance"]
            port_details.append({
                "port": port,
                "invested": round(inv, 2),
                "unrealized": round(unr, 4),
                "realized": round(rea, 4),
                "open_trades": opn
            })
        except Exception:
            pass
    total_portfolio = (usdc_balance or 0) + total_invested + total_unrealized
    binance_total_eur = 0
    binance_holdings = []
    for port in LIVE_PORTS:
        try:
            r2 = req.get(f"http://127.0.0.1:{port}/api/data", timeout=5)
            d2 = r2.json()
            if d2.get("binance_total_eur"):
                binance_total_eur = d2["binance_total_eur"]
                binance_holdings = d2.get("binance_holdings", [])
                break
        except Exception:
            pass
    return jsonify({
        "usdc_balance": usdc_balance,
        "total_invested": round(total_invested, 2),
        "total_unrealized": round(total_unrealized, 4),
        "total_realized": round(total_realized, 4),
        "total_portfolio": round(total_portfolio, 2),
        "ports": port_details,
        "binance_total_eur": binance_total_eur,
        "binance_holdings": binance_holdings
    })

@app.route("/api/data")
def api_data():
    start = request.args.get("start", DEFAULT_START)
    results = []
    all_days = set()
    for port in PORTS:
        data = query_port(port, start_date=start)
        if data:
            results.append(data)
            if "daily" in data:
                all_days.update(data["daily"].keys())
    return jsonify({
        "ports": results,
        "days": sorted(all_days),
        "start_date": start
    })

HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Strategy Comparison Dashboard</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { background: #0d1117; color: #c9d1d9; font-family: 'Consolas', 'Monaco', monospace; font-size: 12px; }
.header { background: #161b22; padding: 12px 20px; border-bottom: 1px solid #30363d; display: flex; align-items: center; justify-content: space-between; }
.header h1 { font-size: 16px; color: #58a6ff; }
.header .refresh { color: #8b949e; font-size: 11px; }
.container { padding: 15px; overflow-x: auto; }
table { border-collapse: collapse; width: 100%; min-width: 800px; }
th { background: #161b22; color: #8b949e; font-size: 11px; text-transform: uppercase; padding: 8px 6px; border: 1px solid #21262d; position: sticky; top: 0; z-index: 10; white-space: nowrap; }
td { padding: 6px; border: 1px solid #21262d; text-align: right; white-space: nowrap; }
td.port { text-align: left; font-weight: 700; color: #58a6ff; background: #161b22; position: sticky; left: 0; z-index: 5; }
td.total { font-weight: 700; background: #161b22; }
.pos { color: #3fb950; }
.neg { color: #f85149; }
.zero { color: #8b949e; }
.day-header { background: #1c2333; color: #58a6ff; font-weight: 700; text-align: center; }
.sub-header { background: #161b22; color: #8b949e; font-size: 10px; text-align: center; }
tr:hover td { background: #1c2128; }
tr:hover td.port { background: #1c2128; }
tr:hover td.total { background: #1c2128; }
.live { color: #f0883e; font-weight: 700; }
.summary { margin: 15px 0; display: flex; gap: 15px; flex-wrap: wrap; }
.summary-card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 12px 16px; min-width: 150px; }
.summary-card .label { color: #8b949e; font-size: 10px; text-transform: uppercase; }
.summary-card .value { font-size: 18px; font-weight: 700; margin-top: 4px; }
.totals-row td { background: #1c2333 !important; font-weight: 700; border-top: 2px solid #58a6ff; }
</style>
</head>
<body>
<div class="header">
    <h1>Strategy Comparison Dashboard</h1>
    <div style="display:flex;align-items:center;gap:12px">
        <label style="color:#8b949e;font-size:11px">From: <input type="datetime-local" id="startDate" value="2026-05-07T05:00" style="background:#0d1117;color:#c9d1d9;border:1px solid #30363d;padding:4px 8px;border-radius:4px;font-family:inherit;font-size:12px"></label>
        <div class="refresh" id="status">Loading...</div>
    </div>
</div>
<div class="container">
    <div class="summary" id="portfolio" style="margin-bottom:5px"></div>
    <div class="summary" id="summary"></div>
    <div id="tableContainer"></div>
</div>
<script>
function pnlClass(v) { return v > 0 ? 'pos' : v < 0 ? 'neg' : 'zero'; }
function fmt(v, d) { return v !== undefined && v !== null ? Number(v).toFixed(d || 2) : '--'; }

async function refresh() {
    try {
        let sd = document.getElementById('startDate').value.replace('T', ' ') + ':00';
        let r = await fetch('/api/data?start=' + encodeURIComponent(sd) + '&t=' + Date.now());
        let data = await r.json();
        render(data);
        document.getElementById('status').textContent = 'Updated: ' + new Date().toLocaleTimeString() + ' (auto-refresh 30s)';
    } catch(e) {
        document.getElementById('status').textContent = 'Error: ' + e.message;
    }
}

function render(data) {
    let ports = data.ports;
    let days = data.days;

    // Summary
    let totalPnl = ports.reduce((s, p) => s + (p.total_pnl || 0), 0);
    let bestPort = ports.reduce((best, p) => (!best || (p.total_pnl || 0) > (best.total_pnl || 0)) ? p : best, null);
    let totalTrades = ports.reduce((s, p) => s + (p.total_closed || 0), 0);
    let totalCapital = ports.reduce((s, p) => s + (p.max_capital || 0), 0);

    let sh = '';
    sh += '<div class="summary-card"><div class="label">Total Profit All Ports</div><div class="value ' + pnlClass(totalPnl) + '">' + fmt(totalPnl) + ' USDT</div></div>';
    sh += '<div class="summary-card"><div class="label">Best Port</div><div class="value" style="color:#58a6ff">' + (bestPort ? bestPort.port : '--') + ' (' + fmt(bestPort ? bestPort.total_pnl : 0) + ')</div></div>';
    sh += '<div class="summary-card"><div class="label">Total Trades</div><div class="value">' + totalTrades + '</div></div>';
    sh += '<div class="summary-card"><div class="label">Active Days</div><div class="value">' + days.length + '</div></div>';
    document.getElementById('summary').innerHTML = sh;

    // Table
    let h = '<table><thead><tr>';
    h += '<th style="position:sticky;left:0;z-index:15;min-width:60px">Port</th>';
    h += '<th>Mode</th>';
    h += '<th>Window</th>';
    h += '<th>Amount</th>';
    h += '<th style="min-width:80px">Total P&L</th>';
    h += '<th>Trades</th>';
    h += '<th>Open</th>';
    h += '<th>Max Sim</th>';
    h += '<th>Max Capital</th>';
    h += '<th>% P&L/Capital</th>';
    h += '<th>Win%</th>';

    days.forEach(d => {
        let parts = d.split('-');
        let label = parts[2] + '/' + parts[1];
        h += '<th colspan="5" class="day-header">' + label + '</th>';
    });
    h += '</tr><tr>';
    h += '<th style="position:sticky;left:0;z-index:15"></th>';
    h += '<th></th><th></th><th></th><th></th><th></th><th></th><th></th><th></th><th></th><th></th>';
    days.forEach(() => {
        h += '<th class="sub-header">P&L</th><th class="sub-header">Trades</th><th class="sub-header">Win%</th><th class="sub-header">Max Capital</th><th class="sub-header">%P&L/Cap</th>';
    });
    h += '</tr></thead><tbody>';

    // Per-day totals for the bottom row
    let dayTotals = {};
    days.forEach(d => { dayTotals[d] = { pnl: 0, trades: 0, wins: 0, losses: 0, max_capital: 0 }; });

    ports.forEach(p => {
        if (p.error) return;
        let modeClass = p.mode === 'live' ? ' live' : '';
        let pctCapital = p.max_capital > 0 ? (p.total_pnl / p.max_capital * 100) : 0;
        h += '<tr>';
        h += '<td class="port' + modeClass + '">' + p.port + '</td>';
        h += '<td' + (p.mode === 'live' ? ' class="live"' : '') + '>' + (p.mode || 'demo').toUpperCase() + '</td>';
        h += '<td>' + (p.window || '--') + 's</td>';
        h += '<td>' + fmt(p.amount, 0) + '</td>';
        h += '<td class="total ' + pnlClass(p.total_pnl) + '">' + fmt(p.total_pnl) + '</td>';
        h += '<td>' + (p.total_closed || 0) + '</td>';
        h += '<td>' + (p.open_count || 0) + '</td>';
        h += '<td>' + (p.max_simultaneous || '--') + '</td>';
        h += '<td>' + fmt(p.max_capital, 0) + '</td>';
        h += '<td class="' + pnlClass(pctCapital) + '">' + fmt(pctCapital, 1) + '%</td>';
        h += '<td>' + fmt(p.win_rate, 1) + '%</td>';

        days.forEach(d => {
            let dd = (p.daily || {})[d];
            if (dd) {
                let dmc = dd.max_capital || 0;
                let dayPct = dmc > 0 ? (dd.pnl / dmc * 100) : 0;
                h += '<td class="' + pnlClass(dd.pnl) + '">' + fmt(dd.pnl) + '</td>';
                h += '<td>' + dd.trades + '</td>';
                h += '<td>' + fmt(dd.win_rate, 0) + '%</td>';
                h += '<td>' + fmt(dmc, 0) + '</td>';
                h += '<td class="' + pnlClass(dayPct) + '">' + fmt(dayPct, 1) + '%</td>';
                dayTotals[d].pnl += dd.pnl || 0;
                dayTotals[d].trades += dd.trades || 0;
                dayTotals[d].wins += dd.wins || 0;
                dayTotals[d].losses += dd.losses || 0;
                dayTotals[d].max_capital += dmc;
            } else {
                h += '<td class="zero">--</td><td class="zero">--</td><td class="zero">--</td><td class="zero">--</td><td class="zero">--</td>';
            }
        });
        h += '</tr>';
    });

    // Totals row
    h += '<tr class="totals-row">';
    h += '<td class="port" style="color:#f0883e">TOTAL</td>';
    h += '<td></td><td></td><td></td>';
    h += '<td class="' + pnlClass(totalPnl) + '">' + fmt(totalPnl) + '</td>';
    h += '<td>' + totalTrades + '</td>';
    h += '<td></td><td></td>';
    h += '<td>' + fmt(totalCapital, 0) + '</td>';
    let totalPct = totalCapital > 0 ? (totalPnl / totalCapital * 100) : 0;
    h += '<td class="' + pnlClass(totalPct) + '">' + fmt(totalPct, 1) + '%</td>';
    h += '<td></td>';
    days.forEach(d => {
        let dt = dayTotals[d];
        let wr = dt.trades > 0 ? (dt.wins / dt.trades * 100) : 0;
        let dtPct = dt.max_capital > 0 ? (dt.pnl / dt.max_capital * 100) : 0;
        h += '<td class="' + pnlClass(dt.pnl) + '">' + fmt(dt.pnl) + '</td>';
        h += '<td>' + dt.trades + '</td>';
        h += '<td>' + fmt(wr, 0) + '%</td>';
        h += '<td>' + fmt(dt.max_capital, 0) + '</td>';
        h += '<td class="' + pnlClass(dtPct) + '">' + fmt(dtPct, 1) + '%</td>';
    });
    h += '</tr>';

    h += '</tbody></table>';
    document.getElementById('tableContainer').innerHTML = h;
}

async function refreshPortfolio() {
    try {
        let r = await fetch('/api/portfolio?t=' + Date.now());
        let d = await r.json();
        let h = '';
        h += '<div class="summary-card" style="border:1px solid #3fb950"><div class="label" style="color:#3fb950">USDC Balance</div><div class="value" style="color:#3fb950">' + (d.usdc_balance !== null ? fmt(d.usdc_balance) : '--') + '</div></div>';
        h += '<div class="summary-card" style="border:1px solid #f0883e"><div class="label" style="color:#f0883e">Total Invested (Live)</div><div class="value" style="color:#f0883e">' + fmt(d.total_invested) + '</div></div>';
        h += '<div class="summary-card"><div class="label">Unrealized P&L</div><div class="value ' + pnlClass(d.total_unrealized) + '">' + fmt(d.total_unrealized, 4) + '</div></div>';
        h += '<div class="summary-card"><div class="label">Realized P&L</div><div class="value ' + pnlClass(d.total_realized) + '">' + fmt(d.total_realized, 4) + '</div></div>';
        h += '<div class="summary-card" style="border:1px solid #58a6ff"><div class="label" style="color:#58a6ff">Real Total Portfolio</div><div class="value" style="color:#58a6ff">' + fmt(d.total_portfolio) + ' USDC</div></div>';
        if (d.binance_total_eur) {
            h += '<div class="summary-card" style="border:1px solid #f0c000"><div class="label" style="color:#f0c000">Binance Portfolio (EUR)</div><div class="value" style="color:#f0c000;font-size:18px">' + fmt(d.binance_total_eur) + ' EUR</div>';
            if (d.binance_holdings && d.binance_holdings.length > 0) {
                var parts = [];
                for (var i = 0; i < d.binance_holdings.length && i < 5; i++) {
                    var hh = d.binance_holdings[i];
                    parts.push(hh.asset + ': ' + fmt(hh.eur, 0) + ' EUR');
                }
                h += '<div style="font-size:10px;color:#8b949e;margin-top:4px">' + parts.join(' | ') + '</div>';
            }
            h += '</div>';
        }
        d.ports.forEach(p => {
            h += '<div class="summary-card"><div class="label">Port ' + p.port + ' (' + p.open_trades + ' open)</div><div class="value">' + fmt(p.invested) + ' USDC</div></div>';
        });
        document.getElementById('portfolio').innerHTML = h;
    } catch(e) {}
}

document.getElementById('startDate').addEventListener('change', refresh);
refresh();
refreshPortfolio();
setInterval(refresh, 30000);
setInterval(refreshPortfolio, 30000);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9999, debug=False)
