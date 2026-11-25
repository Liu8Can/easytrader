# -*- coding: utf-8 -*-
"""静音同花顺 CBAS 行为上报（复制/导出触发的 ``0xFF1`` / ``0xFF5`` 埋点）。

为什么需要它
------------
``no_check_sorc=1`` 只能关掉**本地验证码弹窗**，并不能阻止客户端把“我复制了表格”
这件事上报服务器。实证链路（详见 ``docs/ths_captcha_bypass.md``）：

* 复制：``sub_246B530`` **入口处**（还没进闸门）就 ``SendMessage(0xFF1)``
  → 主窗口 ``sub_11143E0`` → CBAS 组装 ``sub_7B1BB0`` → 打包交易协议 ``2318``（``cmd=bp_log``）
  经主交易连接发往服务器；记录里绑定 ``userid`` + ``fund_acct_en``（加密资金账号）+
  ``qdfs``，末尾还要 ``dword_34AC930`` 全局序号自增。
* 导出：``sub_2479C90`` 在文件写完后 ``SendMessage(0xFF5)``，走同一条上报链路。
* 验证码成功/失败（``copy.yzm.success`` 等）只在验证码分支里发；``no_check_sorc=1``
  时根本走不到，因此不是问题。

结论：**开关能免掉弹窗，但免不掉上报**。要真正“不提交”，必须在客户端发通知前截断。

做法
----
两处通知的机器码形态完全一致::

    push 0
    push 0
    push 0xFF1 / 0xFF5       ; 消息号
    call 0x602800            ; 取主窗口 CWnd*
    mov  ecx, eax
    call 0x602B80            ; ∵ CWnd::SendMessage(UINT, WPARAM, LPARAM)，ret 0xC
                             ; ← 就把这 5 字节替换掉即可

补丁：``e8 xx xx xx xx`` → ``83 c4 0c 90 90``（``add esp,0xC`` + 2×``nop``）。
栈平衡等价于“调用已被跳过”，后续代码不使用 ``eax``，因此复制/导出本身完全不受影响。

* 只改内存，不动磁盘文件，客户端重启即恢复原状；
* 打补丁前校验 ``call`` 目标确实是 ``0x602b80``（未来版本变了就自动跳过并告警）；
* 提供 ``restore()`` 逐字节还原。

实测（2026-11-25，xiadan.exe 5.19.45.110）
------------------------------------------
打补丁后连续 3 次复制：CBAS 序列号 ``dword_34AC930`` 增量 **0**，剪贴板仍拿到 6 行 16 列 TSV；
还原补丁后再复制一次：增量恢复为 **1**。
"""

from __future__ import annotations

import ctypes
import struct
from ctypes import wintypes
from typing import Any, Dict, Iterable, List, Optional

from easytrader.log import logger

IMAGE_BASE = 0x400000
MODULE_NAME = "xiadan.exe"

#: 复制通道：sub_246B530 入口的 `SendMessage(0xFF1)`
SITE_COPY = 0x246B571
#: 保存通道：sub_2479C90 导出完成后的 `SendMessage(0xFF5)`
SITE_SAVE = 0x2479D06
#: 两处共同的被调用函数：`CWnd::SendMessage(UINT,WPARAM,LPARAM)`
SEND_MESSAGE_FN = 0x602B80

SITES: Dict[str, int] = {"copy": SITE_COPY, "save": SITE_SAVE}

#: `call rel32`(5B) → `add esp,0Ch` + 2×`nop`
PATCH_BYTES = bytes.fromhex("83c40c9090")

_PAGE_EXECUTE_READWRITE = 0x40
_PAGE_EXECUTE_READ = 0x20

_originals: Dict[int, Dict[str, bytes]] = {}

_psapi = ctypes.WinDLL("psapi.dll")
_kernel32 = ctypes.WinDLL("kernel32.dll")


class CbasMuteError(RuntimeError):
    """进程访问失败 / 目标字节与预期不符。"""


class _MODULEINFO(ctypes.Structure):
    _fields_ = [
        ("lpBaseOfDll", ctypes.c_void_p),
        ("SizeOfImage", wintypes.DWORD),
        ("EntryPoint", ctypes.c_void_p),
    ]


# ---------------------------------------------------------------------------
# 进程 / 模块
# ---------------------------------------------------------------------------
def find_module_base(pid: int, module_name: str = MODULE_NAME) -> Optional[int]:
    """返回目标进程中 ``xiadan.exe`` 的加载基址（客户端启用 ASLR，不能写死 0x400000）。"""
    handle = _kernel32.OpenProcess(0x0410, False, pid)  # QUERY_INFORMATION|VM_READ
    if not handle:
        raise CbasMuteError(f"OpenProcess 失败: pid={pid}")
    try:
        modules = (ctypes.c_void_p * 1024)()
        needed = wintypes.DWORD()
        if not _psapi.EnumProcessModulesEx(
            handle, modules, ctypes.sizeof(modules), ctypes.byref(needed), 0x03
        ):
            raise CbasMuteError("EnumProcessModulesEx 失败")
        count = needed.value // ctypes.sizeof(ctypes.c_void_p)
        for index in range(count):
            info = _MODULEINFO()
            if not _psapi.GetModuleInformation(
                handle, modules[index], ctypes.byref(info), ctypes.sizeof(info)
            ):
                continue
            buffer = ctypes.create_unicode_buffer(260)
            _psapi.GetModuleBaseNameW(handle, modules[index], buffer, 260)
            if buffer.value.lower() == module_name.lower():
                return int(info.lpBaseOfDll)
        return None
    finally:
        _kernel32.CloseHandle(handle)


def _read(handle, address: int, size: int) -> Optional[bytes]:
    buffer = (ctypes.c_ubyte * size)()
    read = ctypes.c_size_t()
    ok = _kernel32.ReadProcessMemory(
        handle, ctypes.c_void_p(address), buffer, size, ctypes.byref(read)
    )
    return bytes(buffer) if ok and read.value == size else None


def _write(handle, address: int, payload: bytes) -> bool:
    old = wintypes.DWORD()
    if not _kernel32.VirtualProtectEx(
        handle, ctypes.c_void_p(address), len(payload), _PAGE_EXECUTE_READWRITE,
        ctypes.byref(old),
    ):
        return False
    try:
        buffer = (ctypes.c_ubyte * len(payload)).from_buffer_copy(payload)
        written = ctypes.c_size_t()
        ok = _kernel32.WriteProcessMemory(
            handle, ctypes.c_void_p(address), buffer, len(payload), ctypes.byref(written)
        )
        return bool(ok) and written.value == len(payload)
    finally:
        _kernel32.VirtualProtectEx(
            handle, ctypes.c_void_p(address), len(payload), old.value,
            ctypes.byref(wintypes.DWORD()),
        )


# ---------------------------------------------------------------------------
# 补丁
# ---------------------------------------------------------------------------
def call_target(site_va: int, code: bytes) -> Optional[int]:
    """把 ``e8 xx xx xx xx`` 解成静态地址；不是 call rel32 则返回 None。"""
    if len(code) < 5 or code[0] != 0xE8:
        return None
    rel = struct.unpack_from("<i", code, 1)[0]
    return (site_va + 5 + rel) & 0xFFFFFFFF


def original_bytes(site_va: int, target: int = SEND_MESSAGE_FN) -> bytes:
    """重建该站点原本的 ``call rel32`` 指令。

    因为目标函数在同一镜像内的固定地址（``0x602b80``），原始字节可以**确定性重建**，
    所以 ``restore()`` 不依赖打补丁时的进程状态——即使 MCP 进程重启过也能还原。
    """
    rel = (target - (site_va + 5)) & 0xFFFFFFFF
    if rel >= 0x80000000:
        rel -= 1 << 32
    return b"\xE8" + struct.pack("<i", rel)


def apply(
    pid: int, sites: Iterable[str] = ("copy", "save"), strict: bool = False
) -> Dict[str, Any]:
    """给目标进程打上“静音 CBAS 复制/导出埋点”补丁。

    :param pid: ``xiadan.exe`` 的进程 ID
    :param sites: ``("copy", "save")`` 中的子集
    :param strict: True 时任一站点签名不符即抛错；默认跳过并告警
    :return: ``{"base", "pid", "sites": {name: {"applied", "va", "original"}}, "changed"}``
    """
    base = find_module_base(pid)
    if not base:
        raise CbasMuteError(f"未在 pid={pid} 中找到 {MODULE_NAME}")

    access = 0x0410 | 0x0020 | 0x0008  # VM_READ|VM_WRITE|VM_OPERATION
    handle = _kernel32.OpenProcess(access, False, pid)
    if not handle:
        raise CbasMuteError(
            f"OpenProcess(VM_WRITE) 失败: pid={pid}。"
            "请确保 Python/MCP 与同花顺权限对等（同为管理员或同为普通权限）"
        )

    results: Dict[str, Any] = {"base": base, "pid": pid, "sites": {}, "changed": False}
    originals = _originals.setdefault(pid, {})
    try:
        for name in sites:
            site_va = SITES.get(name)
            if site_va is None:
                continue
            address = base + (site_va - IMAGE_BASE)
            code = _read(handle, address, 5)
            info: Dict[str, Any] = {"va": site_va, "applied": False}
            if code is None:
                info["error"] = "ReadProcessMemory 失败"
            elif code == PATCH_BYTES:
                info["applied"] = True
                info["note"] = "已是静音状态"
            elif call_target(site_va, code) != SEND_MESSAGE_FN:
                info["error"] = f"签名不符: {code.hex(' ')}"
                info["code"] = code.hex(" ")
            elif _write(handle, address, PATCH_BYTES):
                originals.setdefault(name, code)
                info["applied"] = True
                info["original"] = code.hex(" ")
                results["changed"] = True
            else:
                info["error"] = "WriteProcessMemory 失败"

            if info.get("error") and strict:
                raise CbasMuteError(f"{name}: {info['error']}")
            if info.get("error"):
                logger.warning("CBAS 埋点静音跳过 %s: %s", name, info["error"])
            results["sites"][name] = info
    finally:
        _kernel32.CloseHandle(handle)

    if results["changed"]:
        logger.info(
            "已静音同花顺 CBAS 复制/导出埋点: base=%#x sites=%s",
            base,
            [n for n, i in results["sites"].items() if i.get("applied")],
        )
    return results


def restore(pid: int) -> Dict[str, Any]:
    """把补丁逐字节还原。

    不依赖打补丁时的进程内状态：原始 ``call`` 指令由签名确定性重建，
    因此即使 MCP 进程重启过、补丁仍在客户端内存里，也能正确还原。
    """
    base = find_module_base(pid)
    if not base:
        raise CbasMuteError(f"未在 pid={pid} 中找到 {MODULE_NAME}")

    handle = _kernel32.OpenProcess(0x0410 | 0x0020 | 0x0008, False, pid)
    if not handle:
        raise CbasMuteError(f"OpenProcess 失败: pid={pid}")
    restored: List[str] = []
    try:
        for name, site_va in SITES.items():
            address = base + (site_va - IMAGE_BASE)
            code = _read(handle, address, 5)
            if code != PATCH_BYTES:
                continue
            if _write(handle, address, original_bytes(site_va)):
                restored.append(name)
    finally:
        _kernel32.CloseHandle(handle)

    _originals.pop(pid, None)
    if restored:
        logger.info("已还原同花顺 CBAS 埋点补丁: %s", restored)
    return {"pid": pid, "restored": restored, "changed": bool(restored)}


def status(pid: int) -> Dict[str, Any]:
    """只读检查两处站点当前是否处于静音状态。"""
    try:
        base = find_module_base(pid)
    except CbasMuteError:
        return {"pid": pid, "found": False}
    if not base:
        return {"pid": pid, "found": False}
    handle = _kernel32.OpenProcess(0x0410, False, pid)
    if not handle:
        return {"pid": pid, "found": True, "readable": False}
    out: Dict[str, Any] = {"pid": pid, "found": True, "base": base, "sites": {}}
    try:
        for name, site_va in SITES.items():
            code = _read(handle, base + (site_va - IMAGE_BASE), 5)
            out["sites"][name] = {
                "va": site_va,
                "bytes": code.hex(" ") if code else None,
                "muted": code == PATCH_BYTES,
            }
        out["muted"] = all(i.get("muted") for i in out["sites"].values())
    finally:
        _kernel32.CloseHandle(handle)
    return out
