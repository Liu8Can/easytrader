# -*- coding: utf-8 -*-
"""同花顺内嵌 API 桥的客户端（本地回环 JSON 协议）。

配合 ``bridge/ths_bridge.py``（跑在 xiadan.exe 的内嵌 Python 里）使用：
桥把客户端内存里的 ``g_money`` / ``g_position`` / ``g_order`` / ``g_fullorder``
原样吐出来，并用 ``xd.cmd(...)`` 发单——也就是说数据与委托都由客户端自己产生，
我们这边只走本机回环 socket，不做 OCR、不模拟点击、不解析私有协议。

协议：TCP 长连接，一行一个 JSON。
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
from typing import Any, Dict, Optional

from easytrader.log import logger

DEFAULT_PORT = 19801
TOKEN_FILENAME = "easytrader_bridge.token"


class BridgeError(RuntimeError):
    """桥不可用 / 协议错误 / 超时。"""


class ThsApiBridge:
    """本地 API 桥连接（线程安全，复用单条长连接）。"""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = DEFAULT_PORT,
        token_file: Optional[str] = None,
        timeout: float = 5.0,
    ):
        self.host = host
        self.port = int(port)
        self.timeout = timeout
        self.token_file = token_file or os.path.join(
            tempfile.gettempdir(), TOKEN_FILENAME
        )
        self._sock: Optional[socket.socket] = None
        self._fp = None
        self._lock = threading.Lock()
        self._seq = 0

    # ------------------------------------------------------------------ 基础
    @property
    def token(self) -> str:
        try:
            with open(self.token_file, encoding="utf-8") as fp:
                return fp.read().strip()
        except OSError:
            return ""

    def _connect(self) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        sock.settimeout(self.timeout)
        self._sock = sock
        self._fp = sock.makefile("rwb")

    def close(self) -> None:
        for obj in (self._fp, self._sock):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        self._fp = self._sock = None

    def _roundtrip(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if self._fp is None:
            self._connect()
        line = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        self._fp.write(line)
        self._fp.flush()
        raw = self._fp.readline()
        if not raw:
            raise BridgeError("桥连接被关闭（客户端可能退出了）")
        reply = json.loads(raw.decode("utf-8"))
        if not reply.get("ok"):
            raise BridgeError(str(reply.get("error") or reply))
        return reply.get("data")

    def call(self, command: str, **params: Any) -> Any:
        """发一条命令并取回数据；连接异常时自动重连一次。"""
        params.setdefault("dry_run", False)
        with self._lock:
            for attempt in (1, 2):
                self._seq += 1
                payload = {
                    "id": self._seq,
                    "cmd": command,
                    "token": self.token,
                }
                payload.update(params)
                try:
                    return self._roundtrip(payload)
                except (OSError, ValueError) as ex:
                    self.close()
                    if attempt == 2:
                        raise BridgeError(f"桥调用失败 ({command}): {ex}") from ex
                    logger.debug("桥重连后重试: %s", ex)
        raise BridgeError("unreachable")  # pragma: no cover

    # ------------------------------------------------------------------ 便捷
    def ping(self) -> Dict[str, Any]:
        return self.call("ping")

    def available(self) -> bool:
        try:
            data = self.ping()
            return bool(data.get("pong"))
        except Exception:
            return False

    def balance(self) -> Dict[str, Any]:
        return self.call("balance").get("money") or {}

    def position(self) -> Dict[str, Any]:
        return self.call("position") or {}

    def entrusts(self) -> Dict[str, Any]:
        return self.call("entrusts") or {}

    def cancel(self, entrust_no: str, dry_run: bool = True) -> Dict[str, Any]:
        return self.call("cancel", entrust_no=str(entrust_no), dry_run=dry_run)

    def buy(
        self, stock_code: str, price: float, amount: int, dry_run: bool = True
    ) -> Dict[str, Any]:
        return self.call(
            "buy",
            stock_code=str(stock_code),
            price=price,
            amount=int(amount),
            dry_run=dry_run,
        )

    def sell(
        self, stock_code: str, price: float, amount: int, dry_run: bool = True
    ) -> Dict[str, Any]:
        return self.call(
            "sell",
            stock_code=str(stock_code),
            price=price,
            amount=int(amount),
            dry_run=dry_run,
        )


# ---------------------------------------------------------------------------
# 字段映射：把客户端内存字段名映射成 MCP 现有工具返回的中文列名
# ---------------------------------------------------------------------------
MONEY_FIELD_MAP = {
    "zjye": "资金余额",
    "djje": "冻结金额",
    "kyje": "可用金额",
    "kqje": "可取金额",
    # zsz 即“总市值/股票市值”，与 GUI 版 get_balance 的「股票市值」对齐
    "zsz": "股票市值",
    "sz": "市值",
    "zzc": "总资产",
}

POSITION_FIELD_MAP = {
    "zqdm": "证券代码",
    "zqmc": "证券名称",
    "gpye": "股票余额",
    "kyye": "可用余额",
    "djsl": "冻结数量",
    "cbj": "成本价",
    "sj": "市价",
    "yk": "盈亏",
    "sz": "市值",
    "jysc": "交易市场",
    "gdzh": "股东代码",
}

ORDER_FIELD_MAP = {
    "htbh": "合同编号",
    "zqdm": "证券代码",
    "zqmc": "证券名称",
    "cz": "操作",
    "wtsj": "委托时间",
    "wtrq": "操作日期",
    "wtjg": "委托价格",
    "wtsl": "委托数量",
    "cjsl": "成交数量",
    "cjjj": "成交均价",
    "cxsl": "撤消数量",
    "bz": "状态说明",
    "jysc": "交易市场",
    "gdzh": "股东代码",
}


def _map_fields(item: Dict[str, Any], mapping: Dict[str, str]) -> Dict[str, Any]:
    return {mapping.get(k, k): v for k, v in (item or {}).items()}


def money_to_balance(money: Dict[str, Any]) -> Dict[str, Any]:
    """``g_money`` → ``get_balance`` 的返回结构。"""
    out: Dict[str, Any] = {}
    for src, dst in MONEY_FIELD_MAP.items():
        if src in money:
            out[dst] = money[src]
    return out


def position_to_records(position: Dict[str, Any]) -> list:
    """``g_position``（以代码为键）→ 持仓记录列表。"""
    records = []
    for code, item in (position or {}).items():
        row = _map_fields(item, POSITION_FIELD_MAP)
        row.setdefault("证券代码", code)
        records.append(row)
    return records


def orders_to_records(orders: Dict[str, Any]) -> list:
    """``g_fullorder``/``g_order``（以合同编号为键）→ 委托记录列表。"""
    records = []
    for htbh, item in (orders or {}).items():
        row = _map_fields(item, ORDER_FIELD_MAP)
        row.setdefault("合同编号", htbh)
        records.append(row)
    return records


def bridge_from_env() -> Optional[ThsApiBridge]:
    """按环境变量构造桥客户端；``EASYTRADER_THS_BRIDGE=off`` 时返回 None。"""
    mode = os.environ.get("EASYTRADER_THS_BRIDGE", "auto").strip().lower()
    if mode in {"0", "false", "off", "no"}:
        return None
    try:
        port = int(os.environ.get("EASYTRADER_BRIDGE_PORT", DEFAULT_PORT))
    except ValueError:
        port = DEFAULT_PORT
    return ThsApiBridge(
        port=port,
        token_file=os.environ.get("EASYTRADER_BRIDGE_TOKEN_FILE") or None,
    )
