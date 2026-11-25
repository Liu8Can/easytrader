# -*- coding: utf-8 -*-
"""把内嵌 API 桥安装到同花顺脚本目录，并生成本机口令。

用法::

    python install_bridge.py                  # 自动探测 xiadan.exe 安装目录
    python install_bridge.py --exe "D:\\同花顺软件\\同花顺\\xiadan.exe"
    python install_bridge.py --uninstall      # 移除桥脚本

安装完成后：重启同花顺下单端（或让它重新加载“自定义组件/策略”面板），
桥会在客户端进程内启动并监听 127.0.0.1:19801。
"""

from __future__ import annotations

import argparse
import os
import secrets
import shutil
import sys
import tempfile
from pathlib import Path

BRIDGE_SRC = Path(__file__).resolve().parent / "bridge" / "ths_bridge.py"
SCRIPT_SUBDIRS = ("自定义组件", "经典策略")
TARGET_NAME = "easytrader_bridge.py"
TOKEN_NAME = "easytrader_bridge.token"
DEFAULT_PORT = 19801

DEFAULT_EXE_CANDIDATES = (
    r"D:\同花顺软件\同花顺\xiadan.exe",
    r"C:\同花顺软件\同花顺\xiadan.exe",
    r"C:\Program Files\同花顺\xiadan.exe",
    r"C:\Program Files (x86)\同花顺\xiadan.exe",
)


def find_exe(explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit)
        if path.is_file():
            return path
        raise SystemExit(f"找不到 xiadan.exe: {path}")
    for candidate in DEFAULT_EXE_CANDIDATES:
        if Path(candidate).is_file():
            return Path(candidate)
    raise SystemExit(
        "未能自动定位 xiadan.exe，请用 --exe 指定（例如 D:\\同花顺软件\\同花顺\\xiadan.exe）"
    )


def write_token(path: Path, force: bool = False) -> str:
    if path.exists() and not force:
        return path.read_text(encoding="utf-8").strip()
    token = secrets.token_hex(16)
    path.write_text(token, encoding="utf-8")
    return token


def install(exe: Path, subdir: str, force_token: bool) -> None:
    base = exe.parent
    dest_dir = base / "script" / subdir
    if not dest_dir.is_dir():
        raise SystemExit(f"脚本目录不存在: {dest_dir}")
    if not BRIDGE_SRC.is_file():
        raise SystemExit(f"桥脚本缺失: {BRIDGE_SRC}")

    dest = dest_dir / TARGET_NAME
    shutil.copyfile(BRIDGE_SRC, dest)
    token_path = Path(tempfile.gettempdir()) / TOKEN_NAME
    token = write_token(token_path, force=force_token)

    print("已安装桥脚本:", dest)
    print("本机口令文件:", token_path)
    print("口令(供校验):", token[:8] + "…")
    print()
    print("接下来二选一激活：")
    print("  1) 重启同花顺下单端（需重新登录）——加载脚本时桥会自动启动；")
    print("  2) 打开客户端的“自定义组件/策略”面板，让它重新扫描脚本目录。")
    print()
    print("激活后可用以下命令自检：")
    print("  python -m easytrader.ths_api_bridge")


def uninstall(exe: Path) -> None:
    base = exe.parent
    removed = []
    for subdir in SCRIPT_SUBDIRS:
        target = base / "script" / subdir / TARGET_NAME
        if target.exists():
            target.unlink()
            removed.append(str(target))
    token_path = Path(tempfile.gettempdir()) / TOKEN_NAME
    if token_path.exists():
        token_path.unlink()
        removed.append(str(token_path))
    for path in removed:
        print("已移除:", path)
    if not removed:
        print("没有找到需要移除的文件")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="安装同花顺内嵌 API 桥")
    parser.add_argument("--exe", help="xiadan.exe 路径")
    parser.add_argument("--subdir", default="自定义组件", choices=SCRIPT_SUBDIRS)
    parser.add_argument("--uninstall", action="store_true", help="移除桥脚本")
    parser.add_argument("--force-token", action="store_true", help="重新生成口令")
    args = parser.parse_args(argv)

    exe = find_exe(args.exe)
    print("目标客户端:", exe)
    if args.uninstall:
        uninstall(exe)
    else:
        install(exe, args.subdir, args.force_token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
