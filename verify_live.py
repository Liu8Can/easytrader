# -*- coding: utf-8 -*-
r"""
同花顺 MCP 实机联调验收脚本（强制 DRY_RUN=true，绝不触发真实下单）

用法（在你自己的 PowerShell 终端中运行）：
    python d:\Documents\反重力\easytrader\verify_live.py
"""

import json
import os
import sys

# 强制开启安全演练模式（默认使用实测免验证码的 xls 导出策略）
os.environ["DRY_RUN"] = "true"
os.environ.setdefault("MAX_ORDER_AMOUNT", "10000")
os.environ.setdefault("EASYTRADER_EXE_PATH", r"D:\同花顺软件\同花顺\xiadan.exe")
os.environ.setdefault("EASYTRADER_BROKER", "ths")
os.environ.setdefault("EASYTRADER_BACKGROUND_MODE", "false")
os.environ.setdefault("EASYTRADER_GRID_STRATEGY", "xls")

import mcp_server


def run_step(title: str, func, *args):
    print(f"\n--- {title} ---")
    raw = func(*args)
    try:
        parsed = json.loads(raw)
        print(json.dumps(parsed, ensure_ascii=False, indent=2))
        return parsed
    except Exception:
        print(raw)
        return {"ok": False, "raw": raw}


def main():
    print("Python:", sys.version)
    print("当前配置:", json.dumps(mcp_server.get_config(), ensure_ascii=False))

    # 1. 只读：资金查询 + 防抖缓存验证
    run_step("1a. get_balance（首次查询）", mcp_server.get_balance)
    run_step("1b. get_balance（4秒内二次查询，应命中 cached=true）", mcp_server.get_balance)

    # 2. 只读：持仓查询（默认 xls 策略，免剪贴板验证码）
    run_step("2. get_position（持仓查询 - xls 策略）", mcp_server.get_position)

    # 3. 只读：当日委托与当日成交
    run_step("3. get_today_entrusts（当日委托）", mcp_server.get_today_entrusts)
    run_step("4. get_today_trades（当日成交）", mcp_server.get_today_trades)

    # 4. DRY_RUN=true 买卖演练（含 ETF 3 位小数 159915 @ 2.259，绝不点击确认下单）
    run_step("5. buy ETF 演练（DRY_RUN=true, 159915 @ 2.259）", mcp_server.buy, "159915", 2.259, 100)
    run_step("6. sell 演练（DRY_RUN=true, 000001 @ 10.24）", mcp_server.sell, "000001", 10.24, 100)

    # 5. 风控上限拦截测试
    run_step(
        "7. buy 超限拦截测试（150000 > MAX_ORDER_AMOUNT）",
        mcp_server.buy,
        "600519",
        1500.0,
        100,
    )

    print("\n=== 联调验收执行完毕 ===")


if __name__ == "__main__":
    main()
