# -*- coding: utf-8 -*-
"""模拟炒股数据层：JSON 文件持久化 + A股交易规则。

数据文件 sim_data.json 与 stock.py 同目录：
{
  "cash": 100000.0,
  "trades": [ {id, time, symbol, name, side, price, qty, amount, fee, ...} ],
  "positions": { "600519": {"name": "贵州茅台", "qty": 100, "cost": 1720.0,
                            "buy_date": "2026-11-04", "bought_today": 100} }
}
"""
import json
import threading
from datetime import datetime
from pathlib import Path

DATA_FILE = Path(__file__).parent / "sim_data.json"
INITIAL_CASH = 100000.0

# 费用模型（2026 年市场常见水平）
COMMISSION_RATE = 0.00025   # 佣金 万 2.5，双边
COMMISSION_MIN = 5.00       # 单笔最低 5 元
STAMP_RATE = 0.0005         # 印花税 0.05%，仅卖出
TRANSFER_RATE = 0.00001     # 过户费 0.001%，双边


def calc_fee(side: str, amount: float) -> dict:
    """按成交额计算费用明细（四舍五入到分）。"""
    commission = max(round(amount * COMMISSION_RATE, 2), COMMISSION_MIN)
    transfer = round(amount * TRANSFER_RATE, 2)
    stamp = round(amount * STAMP_RATE, 2) if side == "sell" else 0.0
    return {
        "commission": commission,
        "transfer": transfer,
        "stamp": stamp,
        "total": round(commission + transfer + stamp, 2),
    }

_lock = threading.Lock()
_last_trade_id = 0


def _load() -> dict:
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"cash": float(INITIAL_CASH), "trades": [], "positions": {}}


def _save(data: dict):
    DATA_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def reset():
    """重置为初始 10 万、清空持仓和记录。"""
    global _last_trade_id
    with _lock:
        _last_trade_id = 0
        _save({"cash": float(INITIAL_CASH), "trades": [], "positions": {}})


def get_overview() -> dict:
    """总资产 / 市值 / 盈亏 / 持仓数（市值用各持仓成本价估算，前端拿到实时价后自行刷新）。"""
    with _lock:
        data = _load()
    positions = data.get("positions", {})
    market_value = sum(
        p["qty"] * p.get("cost", 0.0) for p in positions.values())
    cash = data.get("cash", 0.0)
    total = cash + market_value
    pnl = total - INITIAL_CASH
    return {
        "cash": cash,
        "market_value": market_value,
        "total": total,
        "pnl": pnl,
        "pnl_pct": (pnl / INITIAL_CASH * 100) if INITIAL_CASH else 0.0,
        "pos_count": len(positions),
        "trade_count": len(data.get("trades", [])),
    }


def get_positions() -> list:
    """持仓列表。sellable = T+1 可卖数量（今日买入部分冻结）。"""
    today = datetime.now().strftime("%Y-%m-%d")
    with _lock:
        data = _load()
    out = []
    for code, p in data.get("positions", {}).items():
        qty = p["qty"]
        frozen = p.get("bought_today", 0) if p.get("buy_date") == today else 0
        out.append({
            "symbol": code,
            "name": p.get("name", code),
            "qty": qty,
            "cost": p.get("cost", 0.0),
            "cost_total": round(qty * p.get("cost", 0.0), 2),
            "frozen": frozen,                 # 今日买入、不可卖
            "sellable": qty - frozen,
        })
    return out


def get_statements(limit: int = 100) -> list:
    """成交记录，倒序。"""
    with _lock:
        data = _load()
    return list(reversed(data.get("trades", [])[:limit]))


def limit_pct(symbol: str) -> int:
    """涨跌停幅度（百分比），用于提示信息。"""
    return 20 if symbol.startswith(("688", "300", "301", "302")) else 10


def price_limits(symbol: str, prev_close: float):
    """按板块返回当日涨跌停价格区间 [下, 上]。

    - 科创板 688 / 创业板 300, 301, 302: ±20%
    - 主板 60 / 000, 001, 002, 003 / 北交所 8 / 4: ±10%
    - ST 股（名称带 ST）: 主板 ±5%（简化处理，这里只看代码 + 板块）
    prev_close 为 0 时返回 (0, inf) 不做限制。
    """
    if prev_close <= 0:
        return 0.0, float("inf")
    limit = 0.20 if symbol.startswith(("688", "300", "301", "302")) else 0.10
    lo = round(prev_close * (1 - limit), 2)
    hi = round(prev_close * (1 + limit), 2)
    return lo, hi


def trade(symbol: str, name: str, side: str, price: float, qty: int,
          prev_close: float = 0.0):
    """买入/卖出。返回 (ok: bool, message: str)。

    A股规则：
    - 只能买整手（100 股整数倍）
    - 卖出不能超过可卖持仓（T+1：当日买入次日才能卖）
    - 资金不足不能买（含手续费）
    - 价格必须在当日涨跌停区间内（主板 ±10%，创/科 ±20%）
    - 费用：佣金万 2.5（最低 5 元，双边）+ 印花税 0.05%（仅卖出）+ 过户费 0.001%（双边）
    """
    global _last_trade_id
    side = side.strip().lower()
    if side not in ("buy", "sell"):
        return False, "方向只能是 buy 或 sell"
    if price <= 0 or qty <= 0:
        return False, "价格和数量必须大于 0"
    if qty % 100 != 0:
        return False, "A股交易必须整手（100 股的整数倍）"

    lo, hi = price_limits(symbol, prev_close)
    if price < lo - 1e-9:
        return False, f"价格低于跌停价 {lo:.2f}（昨收 {prev_close:.2f}，{limit_pct(symbol)}% 限制）"
    if price > hi + 1e-9:
        return False, f"价格高于涨停价 {hi:.2f}（昨收 {prev_close:.2f}，{limit_pct(symbol)}% 限制）"

    amount = round(price * qty, 2)
    fee = calc_fee(side, amount)
    today = datetime.now().strftime("%Y-%m-%d")
    realized = None
    with _lock:
        data = _load()
        if side == "buy":
            need = round(amount + fee["total"], 2)
            if need > data["cash"] + 1e-6:
                return False, (f"资金不足：需要 ¥{need:,.2f}"
                               f"（含手续费 ¥{fee['total']:,.2f}），"
                               f"可用 ¥{data['cash']:,.2f}")
            pos = data["positions"].get(symbol, {"name": name, "qty": 0, "cost": 0.0})
            old_cost_total = pos["qty"] * pos.get("cost", 0.0)
            new_qty = pos["qty"] + qty
            # 成本含买入费用，更贴近实盘
            pos["cost"] = round((old_cost_total + amount + fee["total"]) / new_qty, 4)
            pos["qty"] = new_qty
            pos["name"] = name
            if pos.get("buy_date") == today:
                pos["bought_today"] = pos.get("bought_today", 0) + qty
            else:
                pos["buy_date"] = today
                pos["bought_today"] = qty
            data["positions"][symbol] = pos
            data["cash"] = round(data["cash"] - need, 2)
        else:
            pos = data["positions"].get(symbol)
            held = pos["qty"] if pos else 0
            frozen = pos.get("bought_today", 0) if (pos and pos.get("buy_date") == today) else 0
            sellable = held - frozen
            if qty > sellable:
                if held and not sellable:
                    return False, f"T+1 限制：{held} 股均为今日买入，明日才能卖出"
                return False, (f"可卖不足：可卖 {sellable} 股"
                               f"（持仓 {held} 股，今日买入 {frozen} 股受 T+1 冻结），"
                               f"试图卖出 {qty} 股")
            pos["qty"] -= qty
            proceeds = round(amount - fee["total"], 2)
            data["cash"] = round(data["cash"] + proceeds, 2)
            realized = round(amount - fee["total"] - pos.get("cost", 0.0) * qty, 2)
            if pos["qty"] == 0:
                del data["positions"][symbol]

        _last_trade_id += 1
        data.setdefault("trades", []).append({
            "id": _last_trade_id,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": symbol,
            "name": name,
            "side": side,
            "price": round(price, 4),
            "qty": qty,
            "amount": amount,
            "fee": fee["total"],
            "fee_detail": fee,
            "cash_flow": round(-amount - fee["total"], 2) if side == "buy"
                         else round(amount - fee["total"], 2),
            "realized": realized if side == "sell" else None,
        })
        _save(data)
        verb = "买入成交" if side == "buy" else "卖出成交"
        extra = (f"，支出 ¥{amount + fee['total']:,.2f}（手续费 ¥{fee['total']:,.2f}）"
                 if side == "buy" else
                 f"，到账 ¥{amount - fee['total']:,.2f}（手续费 ¥{fee['total']:,.2f}），"
                 f"本笔盈亏 {realized:+,.2f}")
        return True, verb + extra
