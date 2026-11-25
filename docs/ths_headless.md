# 同花顺 MCP 无头化：调用不再把客户端拉到前台

> 一句话：真正的抢焦点源头不是 easytrader 里的 `SetForegroundWindow`，而是
> **pywinauto 的 `TreeViewWrapper.select()` 内部那句 `set_focus()`**。
> 把它换成纯消息 `TVM_SELECTITEM`，再把弹窗/输入框的键鼠回退关掉，
> 查询、下单（DRY_RUN）、资金读取全部可以在客户端被完全遮挡时静默完成。

---

## 1. 现象与上一次修复为什么无效

现象：任何 MCP 工具调用（`get_position` / `get_today_entrusts` / `buy` …）都会让
`网上股票交易系统5.0` 跳到最前面。

历史改动（本仓库 `background_mode`）只在 easytrader 自己的代码里挡了
`SetForegroundWindow`（`clienttrader._ensure_main_window_ready`、`grid_strategies._set_foreground`、
`pop_dialog_handler._set_foreground`），所以**没挡住真正的触发点**：

```python
# pywinauto/controls/common_controls.py  TreeViewWrapper.select()
def select(self, path):
    # http://stackoverflow.com/questions/14111333/...
    # non-focused TreeView can ignore TVM_SELECTITEM
    self.set_focus()                       # ← 这句会激活同花顺窗口
    retval = self.send_message(TVM_SELECTITEM, TVGN_CARET, elem.elem)
```

`HwndWrapper.set_focus()` 内部就是 `SetForegroundWindow`，但它属于 pywinauto，
不在 easytrader 的补丁范围里。

抓取方式（monkeypatch + 调用栈）：

```text
foreground before: 0x1019ae
set_focus  <- ['clienttrader.py:989', 'common_controls.py:1373']   # = _switch_left_menus 里的 get_item(path).select()
foreground after : 0x620cd2                                        # 同花顺主窗口
```

其余会抢焦点的点：

| 位置 | 原因 |
| --- | --- |
| `_switch_left_menus` → `tree.select(path)` | pywinauto 的 `set_focus()` |
| `_switch_left_menus` 非后台分支 → `type_keys("{F5}")` | 键盘输入需要前台 |
| `pop_dialog_handler._submit_by_shortcut` → `type_keys("%Y")` + `_set_foreground` | 同上 |
| `_set_and_verify_edit` 第 4 次回退 → `click_input()` + `type_keys` | 真实鼠标/键盘 |
| `_paste_into_edit` → `set_focus()` + `type_keys("^a^v")` | 同上 |
| `type_edit_control_keys` (`_editor_need_type_keys=True`) → `set_focus()` | 同上 |

---

## 2. 改造方案（全部改为纯 Win32 消息）

### 2.1 切页：`TVM_SELECTITEM`，不碰焦点

```python
def _select_left_menu_item(self, path):
    tree = self._get_left_menus_handle().wrapper_object()
    elem = tree.get_item(path)
    ret = tree.send_message(TVM_SELECTITEM, TVGN_CARET, elem.elem)   # 0x110B / 9
    if ret != TRUE:
        raise TradeError(...)
```

`_switch_left_menus` 在 `background_mode` 下走这条；失败才回退到原来的 `select()`。
实测：`资金股票` / `当日委托` / `当日成交` 三个页签都能切换（表头随页变化），
而前台窗口保持不变。

> 注意：pywinauto 注释里担心的 “non-focused TreeView can ignore TVM_SELECTITEM”
> 在本客户端不成立——`TVM_SELECTITEM` 返回 `TRUE` 且 `CTreeCtrl` 会照常向父窗口发
> `TVN_SELCHANGED`，页面确实切了。所以不需要为了“让它生效”去抢焦点。

### 2.2 弹窗：`BM_CLICK`，不按快捷键

`PopDialogHandler(app, background=True)`：

* `_submit_by_shortcut()` 在后台模式下改为 `_submit_by_click()`（给按钮发 `BM_CLICK`，
  这是消息，不需要前台）；
* 不再调用 `_set_foreground()`。

### 2.3 输入框：`WM_SETTEXT` / `EM_SETSEL + WM_CHAR`

* 主路径本来就是 `set_edit_text`（`WM_SETTEXT`），无焦点需求；
* 后台模式下 `_paste_into_edit()` 改走 `EM_SETSEL` + `WM_CHAR` 逐字符；
* 后台模式下禁用 `click_input()` / `type_keys()` 回退（日志明确说明“不做前台输入回退”）；
* `_editor_need_type_keys` 即使为真，后台模式也退回 `set_edit_text`。

### 2.4 复制/刷新

* 复制一直是 `WM_COMMAND 0xE122` + 读剪贴板（`wmcopy`），本来就不需要焦点；
* 刷新在后台模式用 `post_message(WM_KEYDOWN/WM_KEYUP, VK_F5)`。

---

## 3. 验证（2026-11-25，客户端被完全遮挡）

用一个 topmost 覆盖窗口把同花顺完全挡住，然后连续调用：

```text
client main 0x620cd2
position         ok=1  rows=5   fg=0x7918fc stolen=False
entrusts         ok=1  rows=0   fg=0x7918fc stolen=False
trades           ok=1  rows=0   fg=0x7918fc stolen=False
position#2       ok=1  rows=5   fg=0x7918fc stolen=False
buy(dry)         ok=1           fg=0x7918fc stolen=False
sell(dry)        ok=1           fg=0x7918fc stolen=False
balance          ok=1           fg=0x7918fc stolen=False
覆盖窗口期间，被抢到前台的操作: 无
```

插桩统计：整个过程 `set_focus` / `click_input` / `type_keys` **调用 0 次**。

页签确实切了（`entrusts/trades` 返回空、切回 `position` 仍是 5 行持仓；
若没切页，`entrusts` 会返回 5 行持仓表）。

---

## 4. 边界与已知限制

| 场景 | 结果 |
| --- | --- |
| 客户端被其他窗口遮挡 | ✅ 正常工作（消息驱动，不需要可见） |
| 客户端最小化到任务栏 | ✅ 后台模式用 `SW_SHOWNOACTIVATE` 静默还原后继续 |
| 客户端最小化到**托盘** | ❌ 窗口不存在，无法连接（这是客户端行为，非本方案限制） |
| 真实下单 | ⚠️ 若客户端弹「委托确认」，后台用 `BM_CLICK` 点按钮；若券商皮肤要求键盘确认，需要 `EASYTRADER_BACKGROUND_MODE=false` 走前台 |
| `xls` 策略 | ⚠️ 会弹「另存为」文件对话框，属于交互式窗口，建议用 `wmcopy`（默认） |
| 权限 | 同花顺以管理员运行而 Python 不是 → UIPI 拦截消息/剪贴板，需权限对等 |

---

## 5. 配置

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `EASYTRADER_BACKGROUND_MODE` | `true` | 无头/后台模式：不调 `SetForegroundWindow`、不 `type_keys`、切页走 `TVM_SELECTITEM` |
| `EASYTRADER_GRID_STRATEGY` | `wmcopy` | 后台消息复制 + 剪贴板 |

想回到旧行为（需要前台才能点某些皮肤控件）时，把 `EASYTRADER_BACKGROUND_MODE=false`。
