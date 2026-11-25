# -*- coding: utf-8 -*-
"""在同花顺下单端内嵌 Python 中运行的本地 API 桥。

放置位置
--------
把本文件复制到同花顺安装目录的脚本目录下（二选一）::

    <安装目录>\\script\\自定义组件\\easytrader_bridge.py
    <安装目录>\\script\\经典策略\\easytrader_bridge.py

客户端加载脚本时会执行本文件的**顶层代码**，桥随即在 ``127.0.0.1`` 上起一个
只监听本机回环的最小 JSON 服务，之后所有读写都从这里走——不需要再切页签、
不需要复制粘贴、不需要点任何按钮。

为什么这样最安全
----------------
* 订单是由 ``xd.cmd(...)`` 发出的，也就是**客户端自己的下单通道**：会话、
  加密（hssl）、流水号、埋点全部与人工下单一致，服务端看到的不是"外来协议包"；
* 持仓/资金/委托读的是 ``ths_api`` 导出的 ``g_money`` / ``g_position`` /
  ``g_order`` / ``g_fullorder`` —— 客户端内存里的精确值，不是 OCR、不是表格解析；
* 只监听 ``127.0.0.1``，带 token；默认 ``DRY_RUN=true``，不会误下单。

协议
----
TCP 长连接，一行一个 JSON 请求，一行一个 JSON 响应::

    {"id": 1, "cmd": "ping", "token": "..."}
    {"id": 1, "ok": true, "data": {...}}

命令：``ping`` / ``balance`` / ``position`` / ``entrusts`` / ``trades`` /
``cancel`` / ``buy`` / ``sell`` / ``globals``。

注意：本文件必须兼容 Python 3.5（客户端内嵌 ``python35.dll``），
所以不使用 f-string / 3.6+ 语法。
"""

import json
import os
import socket
import sys
import threading
import time
import traceback

HOST = "127.0.0.1"
PORT = int(os.environ.get("EASYTRADER_BRIDGE_PORT", "19801"))
#: 与客户端同机的本地口令文件（install_bridge.py 生成），防止别的本机进程乱调
TOKEN_FILE = os.path.join(
    os.environ.get("TEMP", r"C:\Windows\Temp"), "easytrader_bridge.token"
)
LOG_FILE = os.path.join(
    os.environ.get("TEMP", r"C:\Windows\Temp"), "easytrader_bridge.log"
)
STATUS_FILE = os.path.join(
    os.environ.get("TEMP", r"C:\Windows\Temp"), "easytrader_bridge.status"
)

#: 默认演练模式：只校验参数、不发真实委托。要真实下单改为 False
DRY_RUN = True
#: 两次委托之间的最小间隔（毫秒）+ 随机抖动，避免高频特征
MIN_ORDER_INTERVAL_MS = 1200
JITTER_MS = 400
#: 启动后延迟多久开始监听（等客户端完成登录/初始化）
START_DELAY_SEC = 8

_api = None
_xd = None
_last_order_ts = [0.0]
_order_lock = threading.Lock()
_token = None


def log(msg):
    try:
        line = "[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
        with open(LOG_FILE, "a") as fp:
            fp.write(line)
    except Exception:
        pass


def write_status(state, detail=""):
    try:
        with open(STATUS_FILE, "w") as fp:
            json.dump(
                {
                    "state": state,
                    "detail": detail,
                    "pid": os.getpid(),
                    "python": sys.version.split()[0],
                    "port": PORT,
                    "dry_run": DRY_RUN,
                    "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
                },
                fp,
            )
    except Exception:
        pass


def load_api(retries=20, interval=3.0):
    """延迟加载 ths_api：客户端可能在脚本加载后才把 xd 对象挂上去。"""
    global _api, _xd
    for attempt in range(retries):
        try:
            import ths_api  # noqa: F401  (由客户端注入)

            _api = ths_api
            _xd = getattr(ths_api, "xd", None)
            if _xd is None:
                try:
                    import xiadan_impl  # noqa: F401

                    _xd = getattr(ths_api, "xd", None) or getattr(xiadan_impl, "xd", None)
                except Exception:
                    pass
            if _xd is not None:
                log("ths_api 已就绪 (attempt=%d)" % (attempt + 1))
                return True
        except Exception:
            pass
        time.sleep(interval)
    log("ths_api 加载失败\n" + traceback.format_exc())
    return False


def _get(name, default=None):
    if _api is None:
        return default
    value = getattr(_api, name, default)
    return default if value is None else value


def _accounts():
    data = _get("g_userdata", {})
    try:
        return list(data.keys())
    except Exception:
        return []


def _read_globals():
    """返回客户端内存里的精确数据表。"""
    return {
        "money": _get("g_money", {}),
        "position": _get("g_position", {}),
        "order": _get("g_order", {}),
        "fullorder": _get("g_fullorder", {}),
        "ordersum": _get("g_ordersum", {}),
        "accounts": _accounts(),
    }


def _throttle_order():
    with _order_lock:
        now = time.time() * 1000.0
        wait_ms = MIN_ORDER_INTERVAL_MS + (hash(str(now)) % JITTER_MS)
        elapsed = now - _last_order_ts[0]
        if elapsed < wait_ms:
            time.sleep((wait_ms - elapsed) / 1000.0)
        _last_order_ts[0] = time.time() * 1000.0


def _exec_cmd(command, dry_run=False):
    """调用客户端自己的下单命令通道。"""
    _throttle_order()
    if DRY_RUN or dry_run:
        log("DRY_RUN 拦截: %s" % command)
        return {"dry_run": True, "command": command, "message": "演练模式，未发送委托"}
    try:
        ret = _xd.cmd(command)
        log("已发送: %s -> %s" % (command, ret))
        return ret if isinstance(ret, dict) else {"raw": str(ret)}
    except Exception:
        log("发送失败: %s\n%s" % (command, traceback.format_exc()))
        return {"error": traceback.format_exc()}


def handle(req):
    cmd = str(req.get("cmd", "")).lower()
    if cmd == "ping":
        return {
            "pong": True,
            "python": sys.version,
            "api_ready": _xd is not None,
            "dry_run": DRY_RUN,
            "port": PORT,
        }
    if cmd == "globals":
        return _read_globals()
    if cmd == "balance":
        return {"money": _get("g_money", {}), "accounts": _accounts()}
    if cmd == "position":
        return _get("g_position", {})
    if cmd == "entrusts":
        return {"order": _get("g_order", {}), "fullorder": _get("g_fullorder", {})}
    if cmd == "trades":
        # 成交明细由客户端导出到 g_userdata / 或通过 get_order 系列获取
        data = _get("g_userdata", {})
        return {"userdata": data}
    if cmd == "cancel":
        entrust_no = str(req.get("entrust_no", "")).strip()
        if not entrust_no:
            return {"error": "entrust_no 不能为空"}
        return _exec_cmd("cancel -h %s" % entrust_no, req.get("dry_run", False))
    if cmd in ("buy", "sell"):
        code = str(req.get("stock_code", "")).strip()
        price = req.get("price")
        amount = req.get("amount")
        notip = bool(req.get("notip", False))
        if not code or price is None or amount is None:
            return {"error": "stock_code/price/amount 必填"}
        command = "%s %s %s %s" % (cmd, code, price, amount)
        if notip:
            command += " -notip"
        return _exec_cmd(command, req.get("dry_run", False))
    return {"error": "未知命令: %s" % cmd}


def _serve_client(conn):
    fp = conn.makefile("rwb")
    try:
        while True:
            line = fp.readline()
            if not line:
                break
            try:
                req = json.loads(line.decode("utf-8"))
            except Exception:
                fp.write(b'{"ok": false, "error": "bad json"}\n')
                fp.flush()
                continue
            if _token and str(req.get("token", "")) != _token:
                fp.write(b'{"ok": false, "error": "bad token"}\n')
                fp.flush()
                continue
            try:
                data = handle(req)
                payload = {"id": req.get("id"), "ok": True, "data": data}
            except Exception:
                payload = {
                    "id": req.get("id"),
                    "ok": False,
                    "error": traceback.format_exc(),
                }
            fp.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            fp.flush()
    except Exception:
        pass
    finally:
        try:
            fp.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


def serve():
    global _token
    try:
        with open(TOKEN_FILE) as fp:
            _token = fp.read().strip()
    except Exception:
        _token = None

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, PORT))
    server.listen(8)
    write_status("listening", "port=%d api_ready=%s" % (PORT, _xd is not None))
    log("bridge 已监听 %s:%d (api_ready=%s, dry_run=%s)" % (HOST, PORT, _xd is not None, DRY_RUN))
    while True:
        try:
            conn, _addr = server.accept()
            conn.settimeout(30)
            threading.Thread(target=_serve_client, args=(conn,)).start()
        except Exception:
            log("accept 异常\n" + traceback.format_exc())
            time.sleep(0.5)


def _bridge_main():
    write_status("starting", "waiting ths_api")
    if not load_api():
        write_status("failed", "ths_api 不可用")
        return
    time.sleep(START_DELAY_SEC)  # 等客户端登录/初始化完成，避免抢跑特征
    try:
        serve()
    except Exception:
        log("serve 退出\n" + traceback.format_exc())
        write_status("failed", traceback.format_exc())


# ---------------------------------------------------------------------------
# 策略文件元数据：客户端加载时会 exec 本文件以读取这些变量，
# 顺带就执行了上面的顶层代码（桥线程随之启动），因此不需要任何按钮/点击。
# ---------------------------------------------------------------------------
dict_params = {}
dict_descs = {}


def main():
    """若客户端把本文件当策略启动，走这里：什么都不做。

    真正的服务线程在文件加载时已经起来了，绝不能把死循环放进客户端调用栈，
    否则会卡住客户端 UI。
    """
    log("客户端调用了 strategy main()：桥已在后台线程运行，此处直接返回")
    return None


if __name__ == "__main__" or True:
    try:
        threading.Thread(target=_bridge_main).start()
    except Exception:
        log("启动线程失败\n" + traceback.format_exc())
