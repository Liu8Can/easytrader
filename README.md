# easytrader (Python 3.13 + 同花顺 5.0 + MCP 维护版)

> 本仓库 Fork 自 [shidenggui/easytrader](https://github.com/shidenggui/easytrader)（原仓库已停止维护），面向 **Python 3.13 + 新版同花顺 PC 下单端（网上股票交易系统 5.0）** 进行长期本地维护与现代化升级，并新增标准 **Stdio MCP Server** 供 AI Agent 调用。

---

## 本分支核心升级与修复

### 1. Python 3.13 与依赖现代化
- 清理 `requirements.txt` 与 `setup.py` 中 2018 年的旧版本强锁，升级兼容 **Python 3.10 ～ 3.13**、`pywinauto >= 0.6.8`、`pandas >= 2.1`、`Pillow >= 10.0`、`mcp >= 1.0`。
- 修复 `easytrader/utils/win_gui.py` 在 `pywinauto 0.6.9` 回退到 `win32gui` 时的句柄类型（`HWND`）转换问题。
- 替换 `pop_dialog_handler.py` 中已废弃的 `Window_()` / `ChildWindow()` API，并修复 Pillow 10+ 下 `captcha.py` 像素值越界问题。

### 2. 新版同花顺（`xiadan.exe` / 网上股票交易系统 5.0）适配与上游高频 Issue 修复
- **免密码直连**：`connect()` 支持直接接管已手动登录的 `xiadan.exe`（支持按路径、PID 或窗口标题自动定位，并附带 Windows UIPI 管理员权限隔离诊断提示）。
- **#457 / #506（假 `success` / 最小化报错）**：操作前自动还原最小化窗口；下单前逐项回读校验**代码、价格、数量** 3 个输入框；下单后禁止盲目返回 `success`，优先校验确认弹窗单号，无弹窗时反查 `today_entrusts` 核实，失败则抛出 `TradeError`。
- **#522（ETF 3 位小数价格出错）**：完整保留 `159915` 等 ETF 的 3 位小数（如 `2.259`），写入后自动回读校验，受输入法或行情回填干扰时自动切换剪贴板全选粘贴重试。
- **#376（弹窗干扰操作）**：查询与下单前自动检测并关闭广告、公告、新股提醒、成交回报等非交易干扰弹窗。
- **#529（`balance` 可用金额偶发为 0）**：读取资金栏若遇到全 0 或“可用金额为 0 但总资产非 0”的刷新延迟，自动等待 `0.3s` 最多重试 3 次。
- **#514 / #524（新版取消交易所下拉框）**：`_set_stock_exchange_type()` 检测控件不存在时安全跳过；修复 `joinquant_follower.py` `price` 字符串转换笔误。
- **多面板隐藏控件歧义修复**：绕过 `pywinauto` `.exists()` 强设 `visible_only=False` 的坑，通过 `descendants()` + `control_id()` + `is_visible()` 精准定位当前可见面板控件。

### 3. 标准 Stdio MCP Server（`mcp_server.py`）
- 基于 `mcp.server.fastmcp.FastMCP` 实现，所有日志与 `pywinauto` 警告强制输出至 `stderr`，保证 `stdout` 仅输出纯净 MCP JSON-RPC 协议。
- 暴露 8 个工具（均返回结构化 JSON）：
  - **只读**：`get_client_info`（连接状态 + 验证码风控自检）、`get_balance`、`get_position`、`get_today_entrusts`、`get_today_trades`
  - **交易**：`buy(stock_code, price, amount)`、`sell(stock_code, price, amount)`、`cancel_entrust(entrust_no)`
- 内置安全护栏与防抖：
  - `DRY_RUN`（默认 `true`）：仅校验参数与当前可见面板控件，绝不点击确认下单。
  - `MAX_ORDER_AMOUNT`（默认 `10000`）：单笔委托金额超限直接拦截。
  - `EASYTRADER_CACHE_TTL`（默认 `4.0` 秒）：`get_balance` / `get_position` 防抖短缓存，真实交易后自动失效。

### 4. 同花顺「复制/保存数据」验证码风控：彻底绕过（精确取值，非 OCR）

新版同花顺对表格的**复制**（`Ctrl+C` / `WM_COMMAND 0xE122`）与**另存为**（`Ctrl+S`）都会弹
「检测到您正在拷贝/保存数据，为保护您的账号数据安全，请先输入验证码」，对自动化是硬阻断。

本仓库通过逆向 `xiadan.exe` 定位到该风控的统一闸门（VA `0x246b530`），确认它每次操作都会读取
客户端自身配置 `xiadan.ini` 的 `[AI_TRADE] no_check_sorc`：

- **方案**：连接时幂等写入 `no_check_sorc=1`，闸门即直接放行 —— 不弹窗、不抢焦点、无需重启客户端；
- **精确**：表格仍由客户端自己导出为 GBK/TSV（16 列全字段），**不是截图 OCR，没有误读风险**；
- **可回滚**：首次修改自动备份 `xiadan.ini.bak.YYYYMMDD`，`ths_no_check.restore()` 一键恢复；
- **可关闭**：`EASYTRADER_THS_NO_CHECK=false` 时不触碰客户端配置。

实现位于 `easytrader/utils/ths_no_check.py`（`ClientTrader.enable_captcha_bypass()`），
完整逆向过程、指令级证据与压测记录见 **[docs/ths_captcha_bypass.md](docs/ths_captcha_bypass.md)**。

> 实测：`no_check_sorc=0` 复制必弹验证码；`=1` 后 10 次 0.3s 间隔的后台复制 10/10 成功、零弹窗。
>
> **注意：开关只免弹窗，免不了上报。** 复制/导出在闸门之前就会发 `0xFF1`/`0xFF5`
> 埋点，经 CBAS 打包成交易协议 `2318`（`cmd=bp_log`，携带 `userid` + 加密资金账号）
> 发往服务器。本仓库因此额外提供 `easytrader/utils/ths_cbas_mute.py`：在客户端进程内把
> 两处 `SendMessage` 调用替换为 `add esp,0xC; nop; nop`（5 字节，可还原），
> 复制/导出照常工作而不再产生 CBAS 记录（实测：复制导致的 CBAS 序号增量由 1 变 0）。
> 详见 [docs/ths_captcha_bypass.md](docs/ths_captcha_bypass.md) 第 9 节。

### 5. 无头/后台运行：调用不再把客户端拉到前台

之前那次“后台模式”只挡了 easytrader 自己的 `SetForegroundWindow`，**没挡 pywinauto
`TreeViewWrapper.select()` 内部的 `set_focus()`**，所以看不到效果。现在改为：

- **切页**：`TVM_SELECTITEM` 纯消息切换左侧菜单（不再 `select()` → `set_focus()`）；
- **弹窗**：`BM_CLICK` 点按钮，不按 `%Y` 快捷键、不做 `SetForegroundWindow`；
- **输入框**：`WM_SETTEXT` / `EM_SETSEL+WM_CHAR`，后台模式禁用 `click_input`/`type_keys` 回退；
- **复制**：`WM_COMMAND 0xE122` + 剪贴板（本来就不需要焦点）；刷新用 `post_message`。

实测：把同花顺完全遮挡后，`get_position` / `get_today_entrusts` / `get_today_trades` /
`buy(DRY_RUN)` / `sell(DRY_RUN)` / `get_balance` 全部正常，前台窗口始终不变，
插桩统计 `set_focus`/`click_input`/`type_keys` 调用 0 次。
详见 [docs/ths_headless.md](docs/ths_headless.md)。


---

## 快速开始

### 1. 安装依赖
```bash
pip install -r requirements.txt
```

### 2. 实机联调验收（安全演练模式）
先手动打开并登录同花顺下单端（`xiadan.exe`），再运行：
```powershell
python verify_live.py
```
> 注意：若同花顺以管理员身份运行，请确保 PowerShell / MCP 客户端也以同等权限运行，否则会受 Windows UIPI 权限隔离影响。

### 3. 客户端 `mcpServers` 配置示例
```json
{
  "mcpServers": {
    "easytrader-ths": {
      "command": "python",
      "args": [
        "D:\\Documents\\反重力\\easytrader\\mcp_server.py"
      ],
      "env": {
        "DRY_RUN": "true",
        "MAX_ORDER_AMOUNT": "10000",
        "EASYTRADER_EXE_PATH": "D:\\同花顺软件\\同花顺\\xiadan.exe",
        "EASYTRADER_BROKER": "ths",
        "EASYTRADER_GRID_STRATEGY": "wmcopy",
        "EASYTRADER_THS_NO_CHECK": "true",
        "EASYTRADER_THS_MUTE_CBAS": "true",
        "EASYTRADER_BACKGROUND_MODE": "true",
        "EASYTRADER_CACHE_TTL": "4.0",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8"
      }
    }
  }
}
```

### 4. 环境变量说明

| 环境变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `DRY_RUN` | `true` | 安全演练开关；设为 `false` 才允许真实点击下单/撤单 |
| `MAX_ORDER_AMOUNT` | `10000` | 单笔委托金额上限（元），超限直接拦截 |
| `EASYTRADER_EXE_PATH` | 自动探测 | `xiadan.exe` 路径；留空时自动匹配“网上股票交易系统5.0”窗口 |
| `EASYTRADER_BROKER` | `ths` | 客户端类型，支持 `ths` / `universal_client` / `miniqmt` 等 |
| `EASYTRADER_GRID_STRATEGY` | `wmcopy` | 表格读取策略，可选 `wmcopy`（后台消息复制，推荐）/ `copy` / `xls` |
| `EASYTRADER_THS_NO_CHECK` | `true` | 连接时写入 `xiadan.ini` 的 `[AI_TRADE] no_check_sorc=1`，关闭复制/保存验证码风控 |
| `EASYTRADER_THS_MUTE_CBAS` | `true` | 连接时静音客户端内 CBAS 复制/导出埋点（`0xFF1`/`0xFF5`），阻止行为随 `bp_log` 上报 |
| `EASYTRADER_CACHE_TTL` | `4.0` | `get_balance` / `get_position` 防抖缓存时间（秒） |
| `EASYTRADER_BACKGROUND_MODE` | `true` | 无头/后台模式：不抢前台焦点（切页走 `TVM_SELECTITEM`、弹窗用 `BM_CLICK`、输入走 `WM_SETTEXT`） |

---

## 原项目致谢
- 上游项目：[shidenggui/easytrader](https://github.com/shidenggui/easytrader)（BSD License）
- 原中文文档：[https://easytrader.readthedocs.io/](https://easytrader.readthedocs.io/)
