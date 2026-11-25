# -*- coding: utf-8 -*-
"""关闭同花顺 5.0「数据导出安全检查」验证码（``xiadan.ini`` 的 ``[AI_TRADE] no_check_sorc``）。

问题现象
--------
同花顺 5.0 的 ``CVirtualGridCtrl`` 表格，只要执行**复制**（``Ctrl+C`` /
``WM_COMMAND 0xE122``）或**另存为**（``Ctrl+S``），就会弹窗：

* 「检测到您正在拷贝数据，为保护您的账号数据安全，请先输入验证码」
* 「检测到您正在保存数据，为保护您的账号数据安全，请先输入验证码」

反向结论（xiadan.exe，PE32 + VMProtect，未虚拟化的 ``.text``）
-----------------------------------------------------------------
复制与保存共用同一个闸门函数 ``0x246b530``，其判定逻辑等价于::

    char buf[0x103];
    GetPrivateProfileString("AI_TRADE", "no_check_sorc", "0",
                            buf, sizeof(buf), "<安装目录>\\xiadan.ini");
    if (buf[0] == '1') {            // ← 本模块写入的开关
        AllowAndCopy();             // 直接放行，弹窗不会出现
    } else if (g_free_quota > 0) {  // 免验证额度（全局 0x3478474）
        g_free_quota--;
        AllowAndCopy();
    } else {
        ShowCaptchaDialog();        // CDialog IDD=0x23A5，server 端校验
    }

对应关键指令（VA，ImageBase=0x400000）::

    0x246b649  push  ebx                     ; 配置管理器
    0x246b64a  call  0x37fcdab               ; 读 xiadan.ini（section/key/default）
    0x246b654  movsx eax, byte ptr [buf]      ; 取返回值首字符
    0x246b65f  cmp   eax, 0x31               ; '1'
    0x246b662  je    0x246b66d               ; → 放行分支
    0x246b664  cmp   dword ptr [0x3478474],0 ; 剩余免验证次数
    0x246b66b  jle   0x246b689               ; → 弹验证码
    0x246b696  call  0x2461bf0               ; 创建验证码弹窗

保存出口 ``0x2479d60`` 读取的是**同一组** ``(xiadan.ini / AI_TRADE / no_check_sorc)``，
因此把这一个键设为 ``1`` 即可同时放开「拷贝」与「保存」两条通道。

为什么优先改 ini 而不是打补丁
----------------------------
* 这是客户端**自带**的配置读取路径，等价于官方“关闭导出安全检查”，不是伪造授权；
* 不碰二进制，不受 VMProtect 完整性校验影响，客户端升级后只需重新写一次；
* 客户端每次操作都会重新读 ini，**无需重启进程**，也不影响已登录会话；
* 数据仍由客户端自己导出（GBK/TSV），是精确值，不存在识别误差。

用法::

    from easytrader.utils import ths_no_check
    ini = ths_no_check.find_xiadan_ini(r"D:\\同花顺软件\\同花顺\\xiadan.exe")
    ths_no_check.ensure_no_check_sor(ini)      # 写入并回读校验
    ths_no_check.read_no_check_sor(ini)        # -> "1"
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

SECTION_NAME = "AI_TRADE"
KEY_NAME = "no_check_sorc"
INI_NAME = "xiadan.ini"

_SECTION_BYTES = f"[{SECTION_NAME}]".encode("ascii")
_KEY_BYTES = KEY_NAME.encode("ascii")


class NoCheckSorError(RuntimeError):
    """xiadan.ini 不可定位 / 不可写 / 校验失败。"""


# ---------------------------------------------------------------------------
# 定位
# ---------------------------------------------------------------------------
def find_xiadan_ini(
    exe_path: Optional[str] = None, extra_dirs: Optional[List[str]] = None
) -> Optional[Path]:
    """按优先级定位 ``xiadan.ini``：下单端同目录 → 常见安装/用户目录。"""
    candidates: List[Path] = []

    if exe_path:
        exe = Path(exe_path)
        base = exe.parent if exe.is_file() or exe.suffix.lower() == ".exe" else exe
        candidates.append(base / INI_NAME)

    for directory in extra_dirs or []:
        candidates.append(Path(directory) / INI_NAME)

    home = Path.home()
    for directory in (
        home / "同花顺",
        home / "Documents" / "同花顺",
        Path(os.environ.get("APPDATA", "")) / "同花顺",
        Path(os.environ.get("LOCALAPPDATA", "")) / "同花顺",
        Path(r"D:\同花顺软件\同花顺"),
        Path(r"C:\同花顺软件\同花顺"),
        Path(r"C:\Program Files\同花顺"),
        Path(r"C:\Program Files (x86)\同花顺"),
    ):
        if str(directory).strip():
            candidates.append(directory / INI_NAME)

    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


# ---------------------------------------------------------------------------
# 读写 ini（按字节处理，绝不改动文件编码/换行/其余内容）
# ---------------------------------------------------------------------------
def _split_lines(data: bytes) -> List[bytes]:
    """保留行尾地切行，保证回写后与原文件逐字节一致（除目标键外）。"""
    return data.splitlines(keepends=True)


def _is_section_header(line: bytes) -> bool:
    return line.lstrip(b"\xef\xbb\xbf \t").startswith(b"[")


def _key_of(line: bytes) -> Optional[bytes]:
    stripped = line.strip()
    if not stripped or stripped.startswith((b";", b"#")) or b"=" not in stripped:
        return None
    return stripped.split(b"=", 1)[0].strip().lower()


def _section_span(lines: List[bytes]) -> Optional[tuple]:
    """返回 ``[AI_TRADE]`` 段落的 (header_index, end_index_exclusive)。"""
    header = None
    for index, line in enumerate(lines):
        if _is_section_header(line) and line.strip().lower() == _SECTION_BYTES.lower():
            header = index
            break
    if header is None:
        return None
    end = len(lines)
    for index in range(header + 1, len(lines)):
        if _is_section_header(lines[index]):
            end = index
            break
    return header, end


def _eol_of(lines: List[bytes], default: bytes = b"\r\n") -> bytes:
    for line in lines:
        if line.endswith(b"\r\n"):
            return b"\r\n"
        if line.endswith(b"\n"):
            return b"\n"
    return default


def read_no_check_sor(ini_path: Optional[str | Path]) -> Optional[str]:
    """读取 ``[AI_TRADE] no_check_sorc`` 的当前值；键不存在返回 ``None``。"""
    if ini_path is None:
        return None
    data = Path(ini_path).read_bytes()
    lines = _split_lines(data)
    span = _section_span(lines)
    if span is None:
        return None
    header, end = span
    for index in range(header + 1, end):
        if _key_of(lines[index]) == _KEY_BYTES:
            _, _, value = lines[index].partition(b"=")
            return value.strip().decode("gbk", "replace")
    return None


def ensure_no_check_sor(
    ini_path: Optional[str | Path], value: str = "1", backup: bool = True
) -> Dict[str, Any]:
    """幂等地把 ``no_check_sorc`` 写成 ``value``（默认 ``1`` = 关闭导出安全检查）。

    返回 ``{"path", "changed", "previous", "value", "backup"}``。
    """
    if ini_path is None:
        raise NoCheckSorError("未定位到 xiadan.ini，无法关闭同花顺导出安全检查")
    path = Path(ini_path)
    if not path.is_file():
        raise NoCheckSorError(f"xiadan.ini 不存在: {path}")

    data = path.read_bytes()
    lines = _split_lines(data)
    eol = _eol_of(lines)
    previous = read_no_check_sor(path)
    new_line = _KEY_BYTES + b"=" + str(value).encode("ascii") + eol

    span = _section_span(lines)
    changed = False
    if span is None:
        # 没有 [AI_TRADE] 段：追加到文件末尾
        if lines and not lines[-1].endswith((b"\n", b"\r")):
            lines[-1] = lines[-1] + eol
        lines.append(_SECTION_BYTES + eol)
        lines.append(new_line)
        changed = True
    else:
        header, end = span
        replaced = False
        for index in range(header + 1, end):
            if _key_of(lines[index]) == _KEY_BYTES:
                if previous != value:
                    lines[index] = new_line
                    changed = True
                replaced = True
                break
        if not replaced:
            # 插到段头之后，紧贴其他配置项
            lines.insert(header + 1, new_line)
            changed = True

    if not changed:
        return {
            "path": str(path),
            "changed": False,
            "previous": previous,
            "value": previous,
            "backup": None,
        }

    backup_path: Optional[str] = None
    if backup:
        candidate = path.with_suffix(path.suffix + f".bak.{time.strftime('%Y%m%d')}")
        try:
            if not candidate.exists():
                shutil.copyfile(path, candidate)
            backup_path = str(candidate)
        except OSError:
            backup_path = None

    payload = b"".join(lines)
    tmp = path.with_name(path.name + ".easytrader.tmp")
    tmp.write_bytes(payload)
    # 客户端可能正持有该文件：原文件被占用时回退为直接覆盖
    try:
        os.replace(tmp, path)
    except OSError:
        try:
            path.write_bytes(payload)
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)

    if read_no_check_sor(path) != value:
        raise NoCheckSorError(f"写入后回读校验失败: {path}")

    return {
        "path": str(path),
        "changed": True,
        "previous": previous,
        "value": value,
        "backup": backup_path,
    }


def disable_captcha(exe_path: Optional[str] = None) -> Dict[str, Any]:
    """高层入口：自动定位 ini 并写入 ``no_check_sorc=1``。"""
    ini = find_xiadan_ini(exe_path=exe_path)
    if ini is None:
        raise NoCheckSorError(
            "未找到 xiadan.ini（请通过 exe_path 指定 xiadan.exe 所在目录）"
        )
    return ensure_no_check_sor(ini)


def restore(ini_path: Optional[str | Path]) -> Dict[str, Any]:
    """回滚：把 ``no_check_sorc`` 恢复为 ``0``（重新开启导出安全检查）。"""
    return ensure_no_check_sor(ini_path, value="0", backup=False)
