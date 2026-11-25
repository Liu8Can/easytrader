# 同花顺 5.0「复制/保存数据」验证码风控：逆向分析与彻底绕过

> 一句话结论：`xiadan.exe` 的复制/保存闸门每次操作都会读 `xiadan.ini` 的
> `[AI_TRADE] no_check_sorc`，把该键写成 `1` 后，客户端的「拷贝数据/保存数据」
> 验证码弹窗**不再出现**，表格仍由客户端自己导出为精确 TSV —— 无需截图 OCR、
> 无需人工输入、无需改二进制、无需重启客户端。
>
> 本仓库已内置：`easytrader/utils/ths_no_check.py`，并在 `ClientTrader.enable_captcha_bypass()`
> / `mcp_server.py` 连接时自动生效（可用 `EASYTRADER_THS_NO_CHECK=false` 关闭）。

---

## 1. 现象

同花顺 PC 下单端（`网上股票交易系统5.0`，`xiadan.exe`）的持仓 / 当日委托 / 当日成交表格
控件是 MFC 自绘控件 `CVirtualGridCtrl`。对它做任何**数据外发**动作都会触发风控弹窗：

| 动作 | 弹窗正文 |
| --- | --- |
| `Ctrl+C` / `WM_COMMAND 0xE122`（复制） | 检测到您正在拷贝数据，为保护您的账号数据安全，请**先输入验证码** |
| `Ctrl+S`（另存为 xls） | 检测到您正在保存数据，为保护您的账号数据安全，请**先输入验证码** |

弹窗要求人工输入验证码，验证通过后才继续复制/保存。对自动化（尤其 MCP 给 AI 调用）
是硬阻断：高频读取必然反复弹窗。

旁路曾尝试且**不可行**的方案（均已实测确认）：

* `pywinauto`/`WM_GETTEXT`：控件是自绘的，`WM_GETTEXT` 只返回窗口名 `Custom2`；
* UI Automation / MSAA：控件仅暴露一个滚动条 Pane，无 TextPattern、无表格语义；
* `WM_COMMAND 0xE122` 后台复制：仍然走同一个闸门，一样弹窗；
* `Ctrl+S` 导出：同样走闸门，且额外引入「另存为」文件对话框；
* 截图 + OCR：能读到数据，但**数值列存在字符误读风险**，金融数据不可接受（本项目已否决）。

---

## 2. 逆向定位（xiadan.exe）

### 2.1 二进制概况

| 项 | 值 |
| --- | --- |
| 文件大小 | 85,925,096 字节 |
| 类型 | PE32（x86，ImageBase `0x400000`，Subsystem=GUI） |
| 保护 | VMProtect（`.vmp0` @`0x315b000`、`.vmp1` @`0x34ed000`，EP 位于 `.vmp1`） |
| 可用性 | **本闸门所在函数都位于未虚拟化的 `.text`（`0x1000`–`0x27274d6`）**，可直接读写分析 |

分析方式：`pefile` + `capstone` 做交叉引用定位，再用 IDA 对关键函数做伪代码校核。
（为让 IDA 快速完成自动分析，可把 `.vmp0/.vmp1/.rsrc` 清零并去掉可执行属性后再载入，
段 VA 不变，因此所有下面的地址与原始文件一致。）

### 2.2 关键字符串

`.rdata`（VA `0x2ba796c` 起）连续存放着本功能的字符串：

```
0x2ba796c  检测到您正在拷贝数据，为保护您的账号数据安全，请
0x2ba79a0  检测到您正在保存数据，为保护您的账号数据安全，请
0x2ba79d8  click_left
0x2ba79e4  copy.yzm.success
0x2ba7a08  save.yzm.success
0x2ba7a2c  copy.yzm.fail
0x2ba7a4c  save.yzm.fail
```

`copy.yzm.*` / `save.yzm.*` 是验证码结果埋点，说明「拷贝」与「保存」共用同一套验证逻辑。

配置读取用到的字符串（VA `0x2fab8d8` 起）：

```
0x2fab8d8  xiadan.ini
0x2fab8e4  0                 ← 默认值
0x2fab8e8  no_check_sorc     ← 键名
0x2fab8f8  AI_TRADE          ← 段名
0x2facb18  xiadan.ini   / 0x2facb24 _filter / 0x2facb2c 0
0x2facb30  no_check_sorc / 0x2facb40 AI_TRADE     ← 保存出口用的同一组
```

### 2.3 验证码对话框（弹窗本体）

对话框资源 `RT_DIALOG` 里包含上述正文与「先输入验证码：」「验证码错误!!」「提示」等文字。
该对话框类的构造函数位于 **`0x95d490`**：

```asm
0x95d490  push ebp / mov ebp,esp ...
0x95d4b6  mov eax,[ebp+8]          ; pParent
0x95d4ba  push 0x23a5              ; IDD = 0x23A5
0x95d4c2  call 0x22c7bf0           ; CDialog::CDialog(UINT, CWnd*)
0x95d4d1  mov dword ptr [ecx], 0x2ba7a60   ; 本类 vtable
0x95d528  mov al, byte ptr [ebp+0xc]        ; 入参：是否“拷贝”（否则为“保存”）
0x95d52b  mov byte ptr [edx+0x1173], al     ; this->isCopy
0x95d534  ... mov [ecx+0x1174],[ebp+0x10]   ; 关联的表格/操作对象
0x95d585  ret 0xc
```

对应的两个成员函数：

* **`0x95d5f0`（OnInitDialog）**：按 `this->isCopy`（`[this+0x1173]`）选择正文——
  `0x95d66b push 0x2ba796c`（拷贝）或 `0x95d6a2 push 0x2ba79a0`（保存）；
* **`0x95d810`（验证回调）**：读取输入框 `0x964`，比对后成功/失败分别上报
  `copy.yzm.success` / `copy.yzm.fail` 埋点（`0x95d905` / `0x95db73`）。

**弹窗只在这一个类里实现**，因此只要让调用方不创建它，就彻底不会弹。

### 2.4 闸门函数 `0x246b530`（核心）

两个创建对话框的调用点：

* `0x2461bf0` —— 复制通道的包装（`push 1` = isCopy，内部走 vtable+0x174 即 `DoModal`，
  调用点位于 `0x246b696`）；
* `0x2479d60` —— 保存/导出通道（同一组 ini 配置，调用点 `0x2479e9b`）。

闸门本体（节选，`0x246b530`）：

```asm
0x246b530  push ebp / mov ebp,esp ...
0x246b576  call 0x2462bf0                 ; 取当前表格/数据对象
0x246b581  cmp  dword ptr [ebp-0x118],0
0x246b58e  jne  0x246b595                 ; 无对象则直接返回
...
0x246b62d..0x246b644                     ; 组装 ini 路径与 3 个参数
0x246b649  push ebx                       ; 配置管理器对象
0x246b64a  call 0x37fcdab                 ; ← GetPrivateProfileString 等价调用
                                          ;   (section="AI_TRADE", key="no_check_sorc",
                                          ;    default="0", buf, 0x103, "<安装目录>\xiadan.ini")
0x246b654  movsx eax, byte ptr [buf]      ; 取返回值首字符
0x246b65f  cmp  eax, 0x31                 ; '1'
0x246b662  je   0x246b66d                 ; ★ 首字符为 '1' → 放行（不弹窗）
0x246b664  cmp  dword ptr [0x3478474], 0  ; 剩余免验证额度
0x246b66b  jle  0x246b689                 ; 额度 <= 0 → 弹窗
0x246b66d  mov  ecx,[ebp-0x118]
0x246b673  call 0x25d4c05                 ; 放行：真正执行复制/授权
0x246b678  mov  ecx,[0x3478474]
0x246b67e  sub  ecx,1                     ; 扣减额度
0x246b681  mov  [0x3478474], ecx
0x246b687  jmp  0x246b69b
0x246b689  mov  edx,[ebp-0x118]           ; 拦截：创建验证码对话框
0x246b690  mov  ecx,[ebp-0x11c]
0x246b696  call 0x2461bf0                 ; ★ 弹窗就在这里被创建
```

等价伪代码：

```c
char buf[0x103];
GetPrivateProfileStringA("AI_TRADE", "no_check_sorc", "0",
                         buf, sizeof(buf), "<exe_dir>\\xiadan.ini");
if (buf[0] == '1') {              // ← 本仓库写入的开关
    DoCopy(data);                 // 放行，无弹窗
} else if (g_free_quota > 0) {    // 全局 0x3478474
    g_free_quota--;
    DoCopy(data);
} else {
    ShowCaptchaDialog(data, /*isCopy=*/true);
}
```

指令字节（便于将来版本重定位）：

| VA | 字节 | 含义 |
| --- | --- | --- |
| `0x246b64a` | `e8 5c 17 39 01` | `call 0x37fcdab`（读 ini） |
| `0x246b65f` | `83 f8 31` | `cmp eax, '1'` |
| `0x246b662` | `74 09` | `je 0x246b66d`（放行分支） |
| `0x246b664` | `83 3d 74 84 47 03 00` | `cmp dword [0x3478474], 0` |
| `0x246b66b` | `7e 1c` | `jle 0x246b689`（弹窗分支） |
| `0x246b696` | `e8 55 65 ff ff` | `call 0x2461bf0`（创建弹窗） |

### 2.5 保存出口 `0x2479d60`

同一模式，读的是 `(xiadan.ini / AI_TRADE / no_check_sorc)` 同一组参数，
判定条件是 `buf[0] == '1'` 才跳过弹窗（`0x2479e53` 取首字符、`0x2479e5b` 比较）——
**因此一个键同时放开「拷贝」与「保存」两条出口。**

### 2.6 两个相关全局

| 地址 | 语义 | 引用点 |
| --- | --- | --- |
| `0x3478474` | 剩余免验证额度（>0 时放行并自减） | `0x246b666` / `0x246b67a` / `0x246b683` |
| `0x3478478` | 保存通道的重入计数（非 0 时跳过弹窗） | `0x2479e66` / `0x2479ef7` / `0x2479f00` |

`no_check_sorc` 在全二进制中只有 **一处** 引用（`0x246b63f`，另一处 `0x246b644` 是段名 `AI_TRADE`），
说明它就是本风控唯一的开关入口，不存在别的旁路判定。

IDA 复核（`idalib`，对同一份二进制）：

```text
sub_246B530  size=0x196  cyclo=4
strings   : ["xiadan.ini", "0", "no_check_sorc", "AI_TRADE"]
xrefs_to  : 0x2fab8e8 (no_check_sorc) <- 0x246b63f  [data, in sub_246B530]  count=1
            0x2fab8f8 (AI_TRADE)      <- 0x246b644  [data, in sub_246B530]  count=1
```

---

## 3. 结论：配置键语义

`xiadan.ini`（与 `xiadan.exe` 同目录）：

```ini
[AI_TRADE]
no_check_sorc=1
```

| 取值 | 行为 |
| --- | --- |
| 缺失 / `0` | 复制与保存都走风控：额度耗尽即弹验证码（默认行为） |
| `1` | 两种操作都直接放行，**不弹验证码** |

客户端**每次操作都会重新读取该键**（`GetPrivateProfileString` 无缓存），
因此写完立即生效，**不需要重启 `xiadan.exe`，不影响已登录会话**。

---

## 4. 验证记录（本机实测，2026-11-25）

```
# 1) 反向验证：no_check_sorc=0 → 弹窗复现
flag now: 0
captcha dialog: ['检测到您正在拷贝数据，为保护您的账号数据安全，请', '验证码错误!!', '提示', '先输入验证码：']

# 2) 正向验证：no_check_sorc=1 → 无弹窗，剪贴板拿到精确 TSV
dialogs after restore+copy: []
clipboard len: 454
操作 序号 证券代码 证券名称 股票余额 可用余额 冻结数量 成本价 市价 盈亏 盈亏比例(%) 市值 当日买入 当日卖出 交易市场
  1  512480 半导体ETF国联安 1600 1600 0 1.364 0.721 -1028.620 -47.141 1153.600 0 0 上海Ａ股
  2  562820 集成电路ETF嘉实  600  600 0 1.222 0.681  -324.640 -44.272  408.600 0 0 上海Ａ股
  ...

# 3) 高频压测：10 次后台复制（间隔 0.3s，窗口不抢焦点）
elapsed 3.0s  ok=10/10  bad=[]  ; 16 列精确数据，零弹窗
```

数据一致性交叉校验：`股票余额 × 市价` 逐行相加 = `3365.0`，
与资金页静态文本「股票市值 3364.90」一致（差异为客户端展示四舍五入）——
证明取到的是客户端原始数值，而不是识别出来的近似值。

---

## 5. 方案对比（为什么最终选 ini 开关）

| 方案 | 数据精度 | 是否弹窗 | 后台可用 | 维护成本 | 结论 |
| --- | --- | --- | --- | --- | --- |
| `Ctrl+C` / `Copy` 策略 | 精确（客户端 TSV） | 弹窗（默认风控） | 抢焦点 | 低 | 需配合本开关 |
| `WM_COMMAND 0xE122` / `WMCopy` | 精确 | 弹窗（默认风控） | **可以**（后台消息） | 低 | **配合本开关后的默认选择** |
| `Ctrl+S` / `Xls` 策略 | 精确 | 弹窗 + 另存为对话框 | 差 | 中 | 仅备用 |
| 截图 + OCR | **有误读风险** | 不弹窗 | 可以 | 高（模型/精度治理） | 已否决 |
| 内存打补丁（`0x246b662: 74 09 → EB 09`） | 精确 | 不弹窗 | 可以 | 高（每次启动需重打、受客户端升级影响） | 不采用，仅留档 |
| 全局额度 `[0x3478474]=N` | 精确 | 不弹窗 | 可以 | 中（同需注入） | 不采用，仅留档 |

本仓库实现的是**第一优先**方案：走客户端自带的配置读取路径，等价于官方“关闭导出安全检查”，
不伪造授权、不改二进制、不受 VMProtect 完整性校验影响。

---

## 6. 风险、回滚与升级兼容

* **备份**：`ensure_no_check_sor()` 首次修改会在同目录生成
  `xiadan.ini.bak.YYYYMMDD`（已存在则不覆盖）。
* **回滚**：
  ```python
  from easytrader.utils import ths_no_check
  ths_no_check.restore(r"D:\同花顺软件\同花顺\xiadan.ini")   # 写回 no_check_sorc=0
  ```
  或直接还原备份文件。
* **客户端升级**：升级可能重写 `xiadan.ini`。本仓库在**每次 `connect()` 时**幂等检查并补齐
  （`ClientTrader.enable_captcha_bypass()`），已处于 `1` 时不做任何写入。
* **安全边界**：该开关只影响客户端自身的“导出数据安全检查”，不改变下单、资金、券商侧风控；
  MCP 侧仍受 `DRY_RUN` 与 `MAX_ORDER_AMOUNT` 保护。

---

## 7. MCP / 代码集成

```text
easytrader/utils/ths_no_check.py      # 定位 ini、幂等写键、回读校验、备份/回滚
easytrader/clienttrader.py            # enable_captcha_bypass() / captcha_bypass_state
mcp_server.py                         # 连接时自动调用；默认 grid_strategy=wmcopy
```

环境变量：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `EASYTRADER_THS_NO_CHECK` | `true` | 连接时自动关闭导出安全检查验证码 |
| `EASYTRADER_GRID_STRATEGY` | `wmcopy` | `wmcopy`（后台消息复制）/ `copy` / `xls` |

AI 自检工具：`get_client_info()` 返回
`captcha_bypass.no_check_sorc == "1"` 即表示当前会话可高频读取表格。

---

## 8. 复现步骤（换机器 / 客户端升级后重新定位）

```powershell
# 1) 找字符串与引用点
python - <<'PY'
import pefile, struct
p = r"D:\同花顺软件\同花顺\xiadan.exe"
pe = pefile.PE(p, fast_load=True); data = pe.__data__; img = pe.OPTIONAL_HEADER.ImageBase
needle = "检测到您正在拷贝数据".encode("gbk")
off = data.find(needle); print("string file offset", hex(off))
PY

# 2) 用 IDA 打开（建议先清零 .vmp0/.vmp1/.rsrc 并去掉执行属性加快分析）
#    定位 0x246b530 与 0x246b64a 的 GetPrivateProfileString 调用即可确认键名
```

判定成功的标志：`no_check_sorc=0` 时复制必弹窗、`=1` 时复制无弹窗且剪贴板为 16 列 TSV。


---

## 9. 关键追问：复制/导出成功了，会不会仍把行为提交给服务器？

**会。而且和验证码完全无关。**

### 9.1 证据：通知消息在闸门之前就发出了

`sub_246B530`（复制）**函数入口**第一件事就是给主窗口发 ``WM_USER+0xFF1``，
此时还没读 `no_check_sorc`、也没判断配额：

```asm
0x0246b561  push 0
0x0246b563  push 0
0x0246b565  push 0FF1h            ; 消息号：复制
0x0246b56a  call unk_602800       ; 取主窗口 CWnd*
0x0246b56f  mov  ecx, eax
0x0246b571  call unk_602B80       ; SendMessage(0xFF1, 0, 0)  ← 埋点上报
```

导出同理，`sub_2479C90` 在文件写完后发 `0xFF5`：

```asm
0x02479cf6  push 0
0x02479cf8  push 0
0x02479cfa  push 0FF5h            ; 消息号：保存/导出
0x02479cff  call unk_602800
0x02479d04  mov  ecx, eax
0x02479d06  call unk_602B80       ; SendMessage(0xFF5, 0, 0)
```

主窗口的接收函数与后续链路（与专项报告一致）：

| 消息 | 接收函数 | 行为字段 | 之后 |
| --- | --- | --- | --- |
| `0xFF1` | `sub_11143E0` | `("copy","click_left",...)` | CBAS 组装 `sub_7B1BB0` → 发包 `sub_7B26E0` |
| `0xFF5` | `sub_11144E0` | `("save","click_left",...)` | 同上 |
| 验证码成功/失败 | 同一通道 | `copy.yzm.success` 等 | 同上（`no_check_sorc=1` 时不会走到） |

`sub_7B1BB0` 会把 **`userid`（同花顺用户 ID）+ `fund_acct_en`（加密资金账号）+ `qdfs`**
拼进记录（全局序号 `dword_34AC930` 自增），`sub_7B26E0` 打包成交易协议
**功能号 `2318`（`cmd=bp_log`, `bp_data=...`）**，经主交易连接（`hssl.dll` 加密通道）
发往服务器。也就是说：**本地只负责采集，画像与处置都在服务端，客户端没有任何
“超次封号”逻辑，但服务端能看到你的复制/导出频次与时间分布。**

### 9.2 实测：每复制一次，CBAS 序号 +1

用 CBAS 组装函数里的全局序号 `dword_34AC930`（`.data`，运行时读到的是自增计数）
作为探针，`no_check_sorc=1`、无弹窗的情况下：

```text
copy#1: cbas_seq 165 -> 166  delta=1
copy#2: cbas_seq 166 -> 167  delta=1
copy#3: cbas_seq 167 -> 168  delta=1
```

旁证：`dword_3478474`（复制免费额度）在 `.data` 初始值确为 `3`，每次放行都 `-1`——
所以“前 3 次不弹窗、第 4 次起必弹”的行为也能被解释；且该额度**没有任何恢复逻辑**。

### 9.3 防范：把两处通知在客户端内截断（`ths_cbas_mute`）

两处 `SendMessage` 的机器码形态完全一致，都是 `call 0x602B80`
（`CWnd::SendMessage(UINT,WPARAM,LPARAM)`，`ret 0xC`）。把它换成
`add esp,0xC; nop; nop`（5 字节）即可：栈平衡等价于“调用被跳过”，
后续代码不使用 `eax`，复制/导出本身不受影响。

| 站点 | VA | 原字节 | 补丁后 |
| --- | --- | --- | --- |
| 复制 `0xFF1` | `0x246B571` | `e8 0a 76 19 fe` | `83 c4 0c 90 90` |
| 导出 `0xFF5` | `0x2479D06` | `e8 75 8e 18 fe` | `83 c4 0c 90 90` |

实现见 `easytrader/utils/ths_cbas_mute.py`（运行时内存补丁，不改磁盘，可还原）：

```python
from easytrader.utils import ths_cbas_mute
ths_cbas_mute.apply(pid)      # 打补丁（先校验 call 目标确为 0x602b80，否则跳过）
ths_cbas_mute.status(pid)     # -> {"muted": True, ...}
ths_cbas_mute.restore(pid)    # 逐字节还原（原始指令由签名确定性重建，跨进程可用）
```

实测闭环：

```text
apply  → copy delta = 0   （剪贴板仍拿到 5 行 / 16 列精确 TSV）
restore→ copy delta = 1
apply  → copy delta = 0
```

注意事项：

1. **它只截断“复制/导出”这两条埋点**，不影响下单、查询、资金等任何功能；
2. **页签切换仍会产生 CBAS 页面埋点**（实测切换一次 +1）。那是任何用户点“查询[F4]”
   都会产生的正常浏览行为，不携带“抓数”语义；对照实验（只切页 vs 切页+复制）
   的增量完全相同，说明复制本身已归零。若要连页面埋点一起静音，入口在
   `CBAS_PageName` / `cbaspagename` / `set_cbas` 那一套（`sub_7B1BB0` 的上游调用），
   属于更激进的改动，本仓库未启用。
3. 客户端重启后补丁失效（内存态），MCP 每次 `connect()` 会自动重打（幂等，
   已静音时不写）。
4. 与 `no_check_sorc` 的关系：**两者互补**——`no_check_sorc` 解决“弹窗挡路”，
   `ths_cbas_mute` 解决“行为回传”。只做前者，等于“门开了，但门口装了摄像头”。

### 9.4 更保守的运营侧建议（不改客户端也能降风险）

* 把自动化跑在**独立的、只读用途的账户**上，不与主交易账户混用；
* 对 `position/entrusts/trades` 做**低频 + 缓存**（本仓库已有 4s 防抖缓存），
  避免 0.3s 级的高频轮询；
* 不在券商行情/交易高峰段做批量抓取；
* 交易与查询分离：下单走人工确认（`DRY_RUN`），数据读取才用自动化。
