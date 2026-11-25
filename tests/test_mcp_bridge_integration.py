# -*- coding: utf-8 -*-
"""MCP 工具走内嵌 API 桥的集成测试（mock 桥，不触碰真实客户端）。"""

import json
import os
import sys

import pytest

from tests.test_ths_api_bridge import MockBridgeServer


@pytest.fixture()
def bridge_env(monkeypatch):
    server = MockBridgeServer(token="test-token")
    token_dir = __import__("tempfile").mkdtemp()
    token_file = os.path.join(token_dir, "easytrader_bridge.token")
    with open(token_file, "w", encoding="utf-8") as fp:
        fp.write("test-token")
    # 指向测试专用口令文件，绝不覆盖真实安装使用的 %TEMP% 口令
    monkeypatch.setenv("EASYTRADER_BRIDGE_TOKEN_FILE", token_file)
    monkeypatch.setenv("EASYTRADER_THS_BRIDGE", "on")
    monkeypatch.setenv("EASYTRADER_BRIDGE_PORT", str(server.port))
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("EASYTRADER_CACHE_TTL", "0")
    yield server
    server.close()


def _mcp():
    import mcp_server

    mcp_server._reset_bridge()
    mcp_server._invalidate_read_cache()
    return mcp_server


def test_balance_comes_from_bridge(bridge_env):
    mcp = _mcp()
    payload = json.loads(mcp.get_balance())
    assert payload["ok"] is True
    assert payload["source"] == "api_bridge"
    assert payload["data"]["资金余额"] == pytest.approx(1302.28)
    assert payload["data"]["股票市值"] == pytest.approx(3334.7)


def test_position_comes_from_bridge(bridge_env):
    mcp = _mcp()
    payload = json.loads(mcp.get_position())
    assert payload["source"] == "api_bridge"
    assert payload["count"] == 1
    row = payload["data"][0]
    assert row["证券代码"] == "512480"
    assert row["股票余额"] == 1600
    assert row["成本价"] == pytest.approx(1.364)


def test_entrusts_come_from_bridge(bridge_env):
    mcp = _mcp()
    payload = json.loads(mcp.get_today_entrusts())
    assert payload["source"] == "api_bridge"
    assert payload["count"] == 1
    assert payload["data"][0]["合同编号"] == "1262365187"


def test_buy_dry_run_still_goes_through_guards(bridge_env):
    mcp = _mcp()
    payload = json.loads(mcp.buy("159915", 2.259, 100))
    assert payload["ok"] is True
    assert payload["source"] == "api_bridge"
    assert payload["dry_run"] is True

    # 金额上限拦截仍然生效（不进入桥）
    blocked = json.loads(mcp.buy("600519", 1500.0, 100))
    assert blocked["ok"] is False
    assert "MAX_ORDER_AMOUNT" in blocked["error"]


def test_invalid_code_rejected_before_bridge(bridge_env):
    mcp = _mcp()
    payload = json.loads(mcp.buy("abc", 1.0, 100))
    assert payload["ok"] is False
    assert "无效的证券代码" in payload["error"]


def test_client_info_reports_bridge_available(bridge_env):
    mcp = _mcp()
    payload = json.loads(mcp.get_client_info())
    assert payload["ok"] is True
    assert payload["data"]["data_channel"] == "api_bridge"
    assert payload["data"]["api_bridge"]["available"] is True
