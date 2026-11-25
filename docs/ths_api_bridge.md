# 同花顺内嵌 API 桥：让读写都由客户端自己发出

> 目标：不再靠"模拟点击 + 剪贴板"取数，也不去硬啃它那套私有加密协议。
> 做法是把一小段脚本放进客户端自己的脚本目录，由**客户端内嵌 Python 执行**，
> 在客户端进程里开一个只监听本机的 socket；我们这边只走 `127.0.0.1`。
> 数据来自 `ths_api` 导出的内存表（精确值），委托由 `xd.cmd(...)` 发出（就是客户端
> 自己的下单通道，会话/加密/流水/埋点与人工下单完全一致）。

---

## 1. 依据：客户端自带脚本接口（不是我们臆造的）

安装目录里有一整套官方脚本框架：

```
script\帮助文档\xiadan.py       # 交易接口文档：cmd / wait_update / get_order_from_htbh
script\帮助文档\HQ.py           # 行情接口
script\经典策略\*.py            # from ths_api import *
script\自定义组件\*.py          # dict_ui 驱动的自定义面板
RES\python\python35.dll         # 内嵌 CPython 3.5 (32-bit)
```

`xiadan.exe` 内部含 `ths_api` / `xiadan_impl` / `PythonWrapper.cpp` / `runpy` / `*.py`
等字符串，说明：**客户端自己 `exec` 这些 `.py`**，并把 C++ 侧的交易对象以
`xiadan_impl`（`xd`）暴露给脚本。

脚本里能拿到的东西（来自官方文档 `xiadan.py`）：

| 名称 | 含义 |
| --- | --- |
| `xd.cmd("buy 300059 dsj1 100 -notip")` | 下单 / 撤单命令通道（支持对手价、涨跌停价、金额、仓位、多账户） |
| `xd.wait_update(ret, status, timeout)` | 等委托状态推进 |
| `xd.get_order_from_htbh(htbh)` | 按合同编号查全量委托 |
| `g_money` | 资金（`zjye/kyje/kqje/zsz/zzc/...`） |
| `g_position` | 持仓（以证券代码为键：`gpye/kyye/djsl/cbj/sj/sz/yk/...`） |
| `g_order` / `g_fullorder` | 可撤委托 / 全量委托（以合同编号为键） |
| `g_ordersum` / `g_userdata` | 汇总 / 多账户数据 |

**这正是"API 从软件里发出"**：我们既没有伪造协议包，也没有破解加密——只是让客户端
用它自己的接口，把结果回给本机。

---

## 2. 架构

```
┌──────────────────────── xiadan.exe（客户端进程）────────────────────────┐
│  内嵌 CPython 3.5                                                        │
│   script\经典策略\easytrader_bridge.py                                   │
│     ├─ from ths_api import *   → xd / g_money / g_position / g_order     │
│     └─ 后台线程：TCP 127.0.0.1:19801  （token 校验，JSON 行协议）         │
│  真正的委托：xd.cmd("buy ...")  →  客户端自己的交易通道（hssl/会话/埋点）│
└──────────────────────────────────────────────────────────────────────────┘
                                  ▲  本机回环 JSON
                                  │
┌──────────────────────── easytrader / MCP（本仓库）──────────────────────┐
│  easytrader/ths_api_bridge.py                                           │
│     ThsApiBridge.call()  + 字段映射（zqdm→证券代码 等）                  │
│  mcp_server.py                                                          │
│     get_balance / get_position / get_today_entrusts / buy / sell / cancel│
│     优先走桥；桥不可用才回退 GUI(wmcopy)                                  │
└──────────────────────────────────────────────────────────────────────────┘
```

* 桥只监听 `127.0.0.1`，且要求 token（`%TEMP%\easytrader_bridge.token`）；
* 默认 `DRY_RUN = True`，桥收到下单命令只打印不发送；MCP 侧还有 `DRY_RUN` 与
  `MAX_ORDER_AMOUNT` 两道闸；
* 桥不可用时，MCP 自动回退到已实现的无头 GUI 通道（`wmcopy`），不会中断服务。

---

## 3. 安装与激活

```powershell
# 1) 安装（默认放进 script\经典策略\，并生成随机口令）
python install_bridge.py --subdir 经典策略

# 2) 激活（二选一）
#    a. 重启同花顺下单端 —— 客户端加载脚本时执行顶层代码，桥自动启动；
#    b. 打开客户端的“策略/自定义组件”面板，让它重新扫描脚本目录。

# 3) 自检
python -c "from easytrader.ths_api_bridge import ThsApiBridge; b=ThsApiBridge(); print(b.ping())"

# 卸载
python install_bridge.py --uninstall
```

激活后 `get_client_info()` 会显示：

```json
"data_channel": "api_bridge",
"api_bridge": {"available": true, "port": 19801}
```

此时 GUI 通道完全不被触达：不连窗口、不切页签、不读剪贴板。

---

## 4. 协议与命令

一行一个 JSON（请求/响应各自一行，UTF-8）：

```json
{"id": 3, "cmd": "position", "token": "<token>"}
{"id": 3, "ok": true, "data": {"512480": {"zqdm": "512480", "gpye": 1600, ...}}}
```

| cmd | 参数 | 返回 |
| --- | --- | --- |
| `ping` | — | `{pong, python, api_ready, dry_run, port}` |
| `balance` | — | `{money, accounts}` |
| `position` | — | `{证券代码: {…}}` |
| `entrusts` | — | `{order, fullorder}` |
| `trades` | — | `{userdata}`（成交字段不稳定，MCP 仍走 GUI） |
| `cancel` | `entrust_no`, `dry_run` | 客户端 `xd.cmd("cancel -h …")` 的返回 |
| `buy` / `sell` | `stock_code`, `price`, `amount`, `dry_run`, `notip` | 同上 |

字段映射（客户端内存字段 → MCP 现有中文列名）：

```text
zqdm→证券代码  zqmc→证券名称  gpye→股票余额  kyye→可用余额  djsl→冻结数量
cbj→成本价    sj→市价        yk→盈亏        sz→市值        jysc→交易市场
htbh→合同编号  cz→操作        wtsj→委托时间   wtjg→委托价格   wtsl→委托数量
cjsl→成交数量  bjj→成交均价    bz→状态说明     wtrq→操作日期
```

---

## 5. 防封禁与"看起来像人"的设计

| 措施 | 说明 |
| --- | --- |
| 走客户端自己的 `xd.cmd` | 传输层、会话、加密、流水号、埋点全部与人工一致；服务端看不到"外来协议包" |
| 只读优先、按需调用 | 桥不主动轮询，只在 MCP 被调用时取内存快照（`g_*` 是客户端已维护好的，不触发新请求） |
| 长连接复用 | 单条 TCP 连接，不做"每请求新建连接"的特征 |
| 下单节流 + 抖动 | 两次委托最小间隔 1200ms + 0–400ms 随机抖动（`MIN_ORDER_INTERVAL_MS`/`JITTER_MS`） |
| 登录后延迟启动 | `START_DELAY_SEC = 8`，避免客户端刚启动就抢跑 |
| 双重演练闸 | 桥内 `DRY_RUN`（默认 True）+ MCP `DRY_RUN` + `MAX_ORDER_AMOUNT` |
| 审计日志 | 每条命令/委托写入 `%TEMP%\easytrader_bridge.log`，可回溯 |
| 端口/口令收敛 | 只监听回环、token 校验、随机 32 位口令写在本机 `%TEMP%` |
| 不改协议、不注入 | 没有 DLL 注入、没有内存补丁、没有伪造包；脚本是客户端官方支持的扩展点 |

> 说明：这里是"减少被识别为异常客户端"的工程措施，不是保证不被风控。真正的账号
> 安全仍取决于使用方式（频率、时段、账户隔离）。建议自动化跑在专用/只读账户上。

---

## 6. 验证状态（诚实交代）

已经验证（本机，离线 mock）：

* `tests/test_ths_api_bridge.py`：协议、token 校验、字段映射、断连降级；
* `tests/test_mcp_bridge_integration.py`：`get_balance` / `get_position` /
  `get_today_entrusts` / `buy(DRY_RUN)` / 风控拦截 / `get_client_info` 全部走桥，
  且桥可用时**完全不触达 GUI**（无 pywinauto 调用）。

尚未验证（需要一次客户端重载）：

* 客户端真的会在启动/面板加载时 `exec` 该脚本 —— 依赖官方框架行为；
* 内嵌 Python 3.5 是否允许 `socket` / `threading`（标准库，理论上允许）。

激活后一条自检即可确认：

```powershell
python -c "from easytrader.ths_api_bridge import ThsApiBridge as B; b=B(); print(b.ping())"
# {'pong': True, 'python': '3.5.0 ...', 'api_ready': True, ...}
```

若 `api_ready` 为 False，说明脚本执行了但 `ths_api` 尚未就绪（桥会按 3s 间隔重试 20 次，
并把过程写进 `%TEMP%\easytrader_bridge.log`）。

---

## 7. 回滚与 Plan B

* **回滚**：`python install_bridge.py --uninstall`（删脚本 + 删口令），客户端下次启动即恢复原状；
  桥本身不写任何客户端配置，不动二进制。
* **Plan B（若该脚本通道被权限封锁）**：
  1. 进程内调用：定位 `xiadan_impl.cmd` 背后的 C++ 入口，注入线程直接调用（同样"从软件里发出"，但要处理 VMProtect 与对象生命周期）；
  2. 协议解析：`sub_7B26E0` 那类函数组包 + `hssl.dll` 加密发往券商——工作量最大，且等于重造客户端，不推荐。
