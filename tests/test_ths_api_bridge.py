# -*- coding: utf-8 -*-
"""``easytrader.ths_api_bridge`` 的协议与字段映射单元测试（用本地 mock 桥，不需客户端）。

运行::

    python -m easytrader.ths_api_bridge
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading

from easytrader import ths_api_bridge as bridge


MOCK_MONEY = {
    "hbdw": "0",
    "zjye": 1302.28,
    "kyje": 1302.28,
    "kqje": 1302.28,
    "zsz": 3334.7,
    "zzc": 4636.98,
    "sz": 3334.7,
}

MOCK_POSITION = {
    "512480": {
        "zqdm": "512480",
        "zqmc": "半导体ETF国联安",
        "gpye": 1600,
        "kyye": 1600,
        "djsl": 0,
        "cbj": 1.364,
        "sj": 0.721,
        "yk": -1028.62,
        "sz": 1153.6,
        "jysc": "上海Ａ股",
        "gdzh": "A123",
    }
}

MOCK_ORDER = {
    "1262365187": {
        "zqdm": "000005",
        "zqmc": "世纪星源",
        "cz": "买入",
        "wtsj": "14:27:16",
        "wtrq": "20261125",
        "wtjg": 3.04,
        "wtsl": 100,
        "cjsl": 0,
        "cjjj": 0.0,
        "cxsl": 0,
        "bz": "未成交",
        "jysc": "深圳Ａ股",
        "gdzh": "0071",
    }
}


class MockBridgeServer:
    """最小复刻 bridge/ths_bridge.py 的协议，用于离线联调。"""

    def __init__(self, token: str = "test-token"):
        self.token = token
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.sock.listen(4)
        self._stop = False
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def _client(self, conn):
        fp = conn.makefile("rwb")
        try:
            for line in fp:
                req = json.loads(line.decode("utf-8"))
                if req.get("token") != self.token:
                    reply = {"id": req.get("id"), "ok": False, "error": "bad token"}
                else:
                    reply = self._handle(req)
                fp.write((json.dumps(reply) + "\n").encode())
                fp.flush()
        except Exception:
            pass
        finally:
            fp.close()
            conn.close()

    def _handle(self, req):
        cmd = req.get("cmd")
        rid = req.get("id")
        if cmd == "ping":
            data = {"pong": True, "api_ready": True, "dry_run": True}
        elif cmd == "balance":
            data = {"money": MOCK_MONEY}
        elif cmd == "position":
            data = MOCK_POSITION
        elif cmd == "entrusts":
            data = {"order": {}, "fullorder": MOCK_ORDER}
        elif cmd in ("buy", "sell", "cancel"):
            data = {"dry_run": True, "command": " ".join(str(v) for v in req.values())}
        else:
            return {"id": rid, "ok": False, "error": "unknown"}
        return {"id": rid, "ok": True, "data": data}

    def close(self):
        self._stop = True
        try:
            self.sock.close()
        except OSError:
            pass


def _make_client(port: int, token: str) -> bridge.ThsApiBridge:
    token_path = os.path.join(tempfile.mkdtemp(), bridge.TOKEN_FILENAME)
    with open(token_path, "w", encoding="utf-8") as fp:
        fp.write(token)
    return bridge.ThsApiBridge(port=port, token_file=token_path, timeout=3.0)


def test_protocol_and_mapping():
    server = MockBridgeServer()
    client = _make_client(server.port, "test-token")
    try:
        assert client.available() is True

        balance = bridge.money_to_balance(client.balance())
        assert balance["资金余额"] == 1302.28
        assert balance["总资产"] == 4636.98
        assert balance["股票市值"] == 3334.7

        records = bridge.position_to_records(client.position())
        assert len(records) == 1
        row = records[0]
        assert row["证券代码"] == "512480"
        assert row["证券名称"] == "半导体ETF国联安"
        assert row["股票余额"] == 1600
        assert row["成本价"] == 1.364

        orders = bridge.orders_to_records(client.entrusts()["fullorder"])
        assert len(orders) == 1
        assert orders[0]["合同编号"] == "1262365187"
        assert orders[0]["委托价格"] == 3.04
        assert orders[0]["状态说明"] == "未成交"

        result = client.buy("159915", 2.259, 100, dry_run=True)
        assert result["dry_run"] is True
    finally:
        client.close()
        server.close()


def test_bad_token_is_rejected():
    server = MockBridgeServer(token="right")
    client = _make_client(server.port, "wrong")
    try:
        try:
            client.ping()
            raise AssertionError("应当因口令错误抛错")
        except bridge.BridgeError:
            pass
    finally:
        client.close()
        server.close()


def test_unreachable_port_reports_unavailable():
    client = bridge.ThsApiBridge(port=1, token_file=os.devnull, timeout=0.5)
    assert client.available() is False
    client.close()


def test_env_switch_off_returns_none():
    old = os.environ.get("EASYTRADER_THS_BRIDGE")
    os.environ["EASYTRADER_THS_BRIDGE"] = "off"
    try:
        assert bridge.bridge_from_env() is None
    finally:
        if old is None:
            os.environ.pop("EASYTRADER_THS_BRIDGE", None)
        else:
            os.environ["EASYTRADER_THS_BRIDGE"] = old


if __name__ == "__main__":
    test_protocol_and_mapping()
    test_bad_token_is_rejected()
    test_unreachable_port_reports_unavailable()
    test_env_switch_off_returns_none()
    print("ths_api_bridge 自检通过")
