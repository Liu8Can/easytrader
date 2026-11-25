# -*- coding: utf-8 -*-
"""
easytrader Stdio MCP Server

将同花顺 PC 下单客户端（xiadan.exe / 网上股票交易系统5.0）封装为标准 Stdio MCP 服务。
设计原则：
1. 严禁向 stdout 打印任何非 MCP 协议内容，所有日志与警告强制走 stderr / 日志文件；
2. 默认开启 DRY_RUN=true（只校验参数与控件，不点击确认下单），并强制单笔金额上限 MAX_ORDER_AMOUNT；
3. 优先直接 connect 接管用户已手动登录好的 xiadan.exe，无需在配置中保存账号密码。
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
import warnings
from typing import Any, Dict, Optional, Tuple

# ---------------------------------------------------------------------------
# 1. 日志与警告隔离（必须在导入 pywinauto / easytrader 之前完成）
# ---------------------------------------------------------------------------
LOG_LEVEL = os.environ.get("EASYTRADER_LOG_LEVEL", "INFO").upper()
LOG_FILE = os.environ.get("EASYTRADER_LOG_FILE", "").strip()

_log_handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
if LOG_FILE:
    _log_handlers.append(logging.FileHandler(LOG_FILE, encoding="utf-8"))

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=_log_handlers,
    force=True,
)
logger = logging.getLogger("easytrader.mcp")


def _warning_to_logger(message, category, filename, lineno, file=None, line=None):
    logger.warning("%s:%s: %s: %s", filename, lineno, category.__name__, message)


warnings.showwarning = _warning_to_logger

# 确保从当前仓库根目录优先导入本地改造后的 easytrader
_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from mcp.server.fastmcp import FastMCP  # noqa: E402
import easytrader  # noqa: E402
from easytrader import grid_strategies  # noqa: E402

# ---------------------------------------------------------------------------
# 2. 安全开关与运行配置（通过环境变量注入）
# ---------------------------------------------------------------------------
def _parse_bool(val: str, default: bool = True) -> bool:
    if val is None or val == "":
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _parse_float(val: str, default: float) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def get_config() -> Dict[str, Any]:
    """动态读取环境变量配置，便于测试与热切换。"""
    background_mode = _parse_bool(
        os.environ.get("EASYTRADER_BACKGROUND_MODE", "false"), default=False
    )
    # 实测同花顺 5.0 对剪贴板复制(copy/wmcopy)易弹风控验证码，而 xls(Ctrl+S导出)稳定无验证码
    default_grid = "xls"
    return {
        "dry_run": _parse_bool(os.environ.get("DRY_RUN", "true"), default=True),
        "max_order_amount": _parse_float(
            os.environ.get("MAX_ORDER_AMOUNT", "10000"), default=10000.0
        ),
        "exe_path": os.environ.get("EASYTRADER_EXE_PATH", "").strip() or None,
        "broker": os.environ.get("EASYTRADER_BROKER", "ths").strip().lower(),
        "grid_strategy": os.environ.get("EASYTRADER_GRID_STRATEGY", default_grid)
        .strip()
        .lower(),
        "enable_type_keys": _parse_bool(
            os.environ.get("EASYTRADER_TYPE_KEYS", "false"), default=False
        ),
        "background_mode": background_mode,
        "cache_ttl": _parse_float(
            os.environ.get("EASYTRADER_CACHE_TTL", "4.0"), default=4.0
        ),
    }


# ---------------------------------------------------------------------------
# 3. 同花顺客户端会话管理（线程安全 + 懒连接 + 防抖短缓存）
# ---------------------------------------------------------------------------
_gui_lock = threading.Lock()
_trader_instance: Optional[Any] = None
# 短缓存：key -> (timestamp, data)，用于防止高频轮询触发同花顺验证码（Issue #448/#452）
_read_cache: Dict[str, Tuple[float, Any]] = {}


def _get_cached(key: str, ttl: float) -> Tuple[bool, Any]:
    if ttl <= 0:
        return False, None
    entry = _read_cache.get(key)
    if entry is None:
        return False, None
    ts, val = entry
    if time.monotonic() - ts <= ttl:
        return True, val
    return False, None


def _set_cached(key: str, val: Any) -> None:
    _read_cache[key] = (time.monotonic(), val)


def _invalidate_read_cache() -> None:
    _read_cache.clear()


def _apply_grid_strategy(trader: Any, strategy_name: str) -> None:
    strategy_map = {
        "copy": grid_strategies.Copy,
        "wmcopy": grid_strategies.WMCopy,
        "xls": grid_strategies.Xls,
    }
    strategy_cls = strategy_map.get(strategy_name)
    if strategy_cls is not None:
        trader.grid_strategy = strategy_cls
        trader._grid_strategy_instance = None


def _get_trader(force_reconnect: bool = False) -> Any:
    """获取已连接到同花顺下单端的 ClientTrader 实例。"""
    global _trader_instance
    cfg = get_config()

    if _trader_instance is not None and not force_reconnect:
        try:
            if _trader_instance.main and _trader_instance.main.exists(timeout=0.5):
                return _trader_instance
        except Exception:
            logger.info("已有同花顺连接失效，准备重新连接...")
        _trader_instance = None

    trader = easytrader.use(cfg["broker"])
    _apply_grid_strategy(trader, cfg["grid_strategy"])
    if hasattr(trader, "enable_background_mode"):
        trader.enable_background_mode(cfg["background_mode"])
    if cfg["enable_type_keys"] and hasattr(trader, "enable_type_keys_for_editor"):
        trader.enable_type_keys_for_editor()

    exe_path = cfg["exe_path"]
    if exe_path and not os.path.exists(exe_path):
        logger.warning(
            "EASYTRADER_EXE_PATH=%s 不存在，将自动探测运行中的同花顺下单窗口", exe_path
        )
        exe_path = None

    logger.info(
        "正在连接同花顺客户端 (broker=%s, grid_strategy=%s, exe_path=%s)...",
        cfg["broker"],
        cfg["grid_strategy"],
        exe_path or "auto-detect",
    )
    trader.connect(exe_path=exe_path)
    _trader_instance = trader
    return _trader_instance


def _json_ok(data: Any, **extra: Any) -> str:
    payload: Dict[str, Any] = {"ok": True, "data": data}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def _json_err(error: str, **extra: Any) -> str:
    payload: Dict[str, Any] = {"ok": False, "error": error}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def _validate_order_params(
    side: str, stock_code: str, price: float, amount: int, max_order_amount: float
) -> Optional[str]:
    """下单前参数与风控上限校验，返回错误信息字符串或 None。"""
    code = str(stock_code).strip()
    if not re.fullmatch(r"(sh|sz|bj)?\d{6}", code, re.IGNORECASE):
        return f"无效的证券代码: {stock_code!r}（应为 6 位数字代码，如 '600519'）"

    if price <= 0:
        return f"无效的委托价格: {price}（必须大于 0）"

    if amount <= 0:
        return f"无效的委托数量: {amount}（必须大于 0）"

    if side == "buy" and amount % 100 != 0:
        return f"A 股买入数量必须为 100 股的整数倍，当前为 {amount}"

    order_value = round(float(price) * int(amount), 2)
    if max_order_amount > 0 and order_value > max_order_amount:
        return (
            f"触发单笔金额风控上限：本次委托金额 {order_value:.2f} 元 "
            f"超过 MAX_ORDER_AMOUNT={max_order_amount:.2f} 元，已拦截！"
        )

    return None


# ---------------------------------------------------------------------------
# 4. FastMCP 工具定义
# ---------------------------------------------------------------------------
mcp = FastMCP("easytrader-ths")


@mcp.tool()
def get_balance() -> str:
    """只读查询：获取当前同花顺账户的资金信息（资金余额、可用金额、可取金额、股票市值、总资产），内置 4 秒防抖缓存。"""
    cfg = get_config()
    with _gui_lock:
        hit, cached_val = _get_cached("balance", cfg["cache_ttl"])
        if hit:
            return _json_ok(cached_val, cached=True)
        try:
            trader = _get_trader()
            data = trader.balance
            _set_cached("balance", data)
            return _json_ok(data, cached=False)
        except Exception as ex:
            logger.exception("get_balance 失败")
            return _json_err(str(ex))


@mcp.tool()
def get_position() -> str:
    """只读查询：获取当前同花顺账户的股票持仓明细列表，内置 4 秒防抖缓存以防频繁刷新触发验证码。"""
    cfg = get_config()
    with _gui_lock:
        hit, cached_val = _get_cached("position", cfg["cache_ttl"])
        if hit:
            return _json_ok(
                cached_val,
                count=len(cached_val) if isinstance(cached_val, list) else 0,
                cached=True,
            )
        try:
            trader = _get_trader()
            data = trader.position
            _set_cached("position", data)
            return _json_ok(
                data, count=len(data) if isinstance(data, list) else 0, cached=False
            )
        except Exception as ex:
            logger.exception("get_position 失败")
            return _json_err(str(ex))


@mcp.tool()
def get_today_entrusts() -> str:
    """只读查询：获取当日全部委托记录列表。"""
    with _gui_lock:
        try:
            trader = _get_trader()
            data = trader.today_entrusts
            return _json_ok(data, count=len(data) if isinstance(data, list) else 0)
        except Exception as ex:
            logger.exception("get_today_entrusts 失败")
            return _json_err(str(ex))


@mcp.tool()
def get_today_trades() -> str:
    """只读查询：获取当日全部成交记录列表。"""
    with _gui_lock:
        try:
            trader = _get_trader()
            data = trader.today_trades
            return _json_ok(data, count=len(data) if isinstance(data, list) else 0)
        except Exception as ex:
            logger.exception("get_today_trades 失败")
            return _json_err(str(ex))


@mcp.tool()
def buy(stock_code: str, price: float, amount: int) -> str:
    """
    限价买入委托（受 DRY_RUN 与 MAX_ORDER_AMOUNT 安全开关保护）。

    参数：
    - stock_code: 6位证券代码（如 "600519"、"000001"）
    - price: 限价委托价格（元）
    - amount: 买入股数（需为 100 的整数倍）
    """
    cfg = get_config()
    err = _validate_order_params("buy", stock_code, price, amount, cfg["max_order_amount"])
    if err:
        logger.warning("buy 参数/风控拦截: %s", err)
        return _json_err(err, dry_run=cfg["dry_run"])

    order_value = round(float(price) * int(amount), 2)
    with _gui_lock:
        try:
            trader = _get_trader()
            if cfg["dry_run"]:
                result = trader.buy(stock_code, price, amount, dry_run=True)
                result["estimated_amount_rmb"] = order_value
                result["max_order_amount_rmb"] = cfg["max_order_amount"]
                return _json_ok(result, dry_run=True)

            result = trader.buy(stock_code, price, amount, dry_run=False)
            _invalidate_read_cache()
            return _json_ok(result, dry_run=False, estimated_amount_rmb=order_value)
        except Exception as ex:
            logger.exception("buy 执行失败")
            return _json_err(str(ex), dry_run=cfg["dry_run"])


@mcp.tool()
def sell(stock_code: str, price: float, amount: int) -> str:
    """
    限价卖出委托（受 DRY_RUN 与 MAX_ORDER_AMOUNT 安全开关保护）。

    参数：
    - stock_code: 6位证券代码（如 "600519"、"000001"）
    - price: 限价委托价格（元）
    - amount: 卖出股数（正整数）
    """
    cfg = get_config()
    err = _validate_order_params("sell", stock_code, price, amount, cfg["max_order_amount"])
    if err:
        logger.warning("sell 参数/风控拦截: %s", err)
        return _json_err(err, dry_run=cfg["dry_run"])

    order_value = round(float(price) * int(amount), 2)
    with _gui_lock:
        try:
            trader = _get_trader()
            if cfg["dry_run"]:
                result = trader.sell(stock_code, price, amount, dry_run=True)
                result["estimated_amount_rmb"] = order_value
                result["max_order_amount_rmb"] = cfg["max_order_amount"]
                return _json_ok(result, dry_run=True)

            result = trader.sell(stock_code, price, amount, dry_run=False)
            _invalidate_read_cache()
            return _json_ok(result, dry_run=False, estimated_amount_rmb=order_value)
        except Exception as ex:
            logger.exception("sell 执行失败")
            return _json_err(str(ex), dry_run=cfg["dry_run"])


@mcp.tool()
def cancel_entrust(entrust_no: str) -> str:
    """
    撤单操作（受 DRY_RUN 安全开关保护）。

    参数：
    - entrust_no: 委托合同编号（字符串）
    """
    cfg = get_config()
    target_no = str(entrust_no).strip()
    if not target_no:
        return _json_err("委托编号 entrust_no 不能为空", dry_run=cfg["dry_run"])

    if cfg["dry_run"]:
        logger.info("[DRY_RUN] 跳过实际撤单点击: entrust_no=%s", target_no)
        return _json_ok(
            {
                "dry_run": True,
                "entrust_no": target_no,
                "message": "DRY_RUN 模式已开启：已校验撤单编号，未实际执行撤单",
            },
            dry_run=True,
        )

    with _gui_lock:
        try:
            trader = _get_trader()
            result = trader.cancel_entrust(target_no)
            _invalidate_read_cache()
            return _json_ok(result, dry_run=False)
        except Exception as ex:
            logger.exception("cancel_entrust 执行失败")
            return _json_err(str(ex), dry_run=False)


if __name__ == "__main__":
    cfg = get_config()
    logger.info(
        "启动 easytrader Stdio MCP Server (DRY_RUN=%s, MAX_ORDER_AMOUNT=%.2f, BROKER=%s, GRID=%s)",
        cfg["dry_run"],
        cfg["max_order_amount"],
        cfg["broker"],
        cfg["grid_strategy"],
    )
    mcp.run(transport="stdio")
