# -*- coding: utf-8 -*-
import abc
import io
import os
import tempfile
import time
from io import StringIO
from typing import TYPE_CHECKING, Dict, List, Optional

import pandas as pd
import pywinauto
import pywinauto.clipboard
import pywinauto.keyboard

from easytrader import exceptions
from easytrader.log import logger
from easytrader.utils.captcha import captcha_recognize
from easytrader.utils.win_gui import SetForegroundWindow, ShowWindow, win32defines

if TYPE_CHECKING:
    # pylint: disable=unused-import
    from easytrader import clienttrader


class IGridStrategy(abc.ABC):
    @abc.abstractmethod
    def get(self, control_id: int) -> List[Dict]:
        """
        获取 grid 数据并格式化返回

        :param control_id: grid 的 control id
        :return: grid 数据
        """
        pass

    @abc.abstractmethod
    def set_trader(self, trader: "clienttrader.IClientTrader"):
        pass


class BaseStrategy(IGridStrategy):
    def __init__(self):
        self._trader = None

    def set_trader(self, trader: "clienttrader.IClientTrader"):
        self._trader = trader

    @abc.abstractmethod
    def get(self, control_id: int) -> List[Dict]:
        """
        :param control_id: grid 的 control id
        :return: grid 数据
        """
        pass

    def _get_grid(self, control_id: int):
        grid = self._trader.main.child_window(
            control_id=control_id, class_name="CVirtualGridCtrl", visible_only=True
        )
        return grid

    def _set_foreground(self, grid=None):
        if getattr(self._trader, "_background_mode", False):
            # 后台静默模式：绝不抢占前台焦点，仅在最小化时静默还原
            try:
                target = grid or self._trader.main
                if target.has_style(win32defines.WS_MINIMIZE):
                    ShowWindow(target.wrapper_object(), 4)  # SW_SHOWNOACTIVATE
            except Exception as ex:
                logger.debug("background restore ignored: %s", ex)
            return
        try:
            if grid is None:
                grid = self._trader.main
            if grid.has_style(win32defines.WS_MINIMIZE):  # if minimized
                ShowWindow(grid.wrapper_object(), 9)  # restore window state
            else:
                SetForegroundWindow(grid.wrapper_object())  # bring to front
        except Exception as ex:
            logger.debug("set foreground ignored: %s", ex)

    def _handle_captcha_dialog(self, max_retry: int = 5) -> bool:
        """
        检测同花顺复制/导出表格时弹出的风控验证码弹窗（如“检测到你正在拷贝数据 请输入验证码”）。
        遇到验证码时绝不死循环卡死或静默返回空列表，直接明确抛出需要人工输入验证码的提示。
        """
        try:
            top = self._trader.app.top_window()
            # 如果 top_window 就是主窗口本身，说明没有弹窗
            if top.handle == self._trader.main.handle:
                return True
            static_texts = [
                c.window_text().strip()
                for c in top.descendants(class_name="Static")
                if c.window_text().strip()
            ]
            combined = " ".join(static_texts)
            if "验证码" in combined or "拷贝数据" in combined:
                logger.warning("检测到同花顺风控验证码弹窗: %s", combined)
                raise exceptions.TradeError(
                    f"同花顺弹出风控验证码（{combined}），需要人工在客户端输入验证码后重试"
                )
        except exceptions.TradeError:
            raise
        except Exception as ex:
            logger.debug("check captcha dialog ignored: %s", ex)
        return True

    def _parse_tsv_content(self, content: str) -> List[Dict]:
        if not content or not content.strip():
            return []
        df = pd.read_csv(
            StringIO(content),
            sep="\t",
            dtype=self._trader.config.GRID_DTYPE,
            na_filter=False,
        )
        # 清理列名首尾空白与末尾空列（同花顺导出行尾常带多余制表符）
        df.columns = [str(c).strip() for c in df.columns]
        drop_cols = [c for c in df.columns if c == "" or c.startswith("Unnamed:")]
        if drop_cols:
            df = df.drop(columns=drop_cols)
        return df.to_dict("records")


class Copy(BaseStrategy):
    """
    通过复制 grid 内容到剪切板再读取来获取 grid 内容
    """

    _need_captcha_reg = True

    def get(self, control_id: int) -> List[Dict]:
        grid = self._get_grid(control_id)
        self._set_foreground(grid)
        grid.type_keys("^A^C", set_foreground=False, pause=0.2)
        content = self._get_clipboard_data()
        return self._format_grid_data(content)

    def _format_grid_data(self, data: str) -> List[Dict]:
        if not data or not data.strip():
            return []
        try:
            return self._parse_tsv_content(data)
        except Exception as ex:
            logger.warning("解析剪贴板表格数据失败: %s", ex)
            Copy._need_captcha_reg = True
            return []

    def _get_clipboard_data(self) -> str:
        if Copy._need_captcha_reg:
            self._handle_captcha_dialog()

        count = 5
        while count > 0:
            try:
                return pywinauto.clipboard.GetData()
            # pylint: disable=broad-except
            except Exception as e:
                # 回退使用 pyperclip 读取剪贴板
                try:
                    import pyperclip

                    text = pyperclip.paste()
                    if text:
                        return text
                except Exception:
                    pass
                count -= 1
                logger.warning("读取剪贴板失败 (%s), 剩余重试次数: %d", e, count)
                self._trader.wait(0.2)
        return ""


class WMCopy(Copy):
    """
    通过发送 WM_COMMAND(0xE122) 在后台复制 grid 内容到剪切板再读取（不抢前台焦点）
    """

    def get(self, control_id: int) -> List[Dict]:
        grid = self._get_grid(control_id)
        # 清空旧剪贴板内容，避免误读上一次复制的数据
        try:
            import pyperclip

            pyperclip.copy("")
        except Exception:
            pass
        grid.post_message(win32defines.WM_COMMAND, 0xE122, 0)
        # 客户端写剪贴板可能占用 1~2 秒（16 列宽表），等久一点再读，避免 OpenClipboard 拒绝访问
        self._trader.wait(0.35)
        content = self._get_clipboard_data()
        if not content or not content.strip():
            # 若后台 WM_COMMAND 未命中（少数券商皮肤），再短暂重试一次
            grid.post_message(win32defines.WM_COMMAND, 0xE122, 0)
            self._trader.wait(0.45)
            content = self._get_clipboard_data()
        return self._format_grid_data(content)


class Xls(BaseStrategy):
    """
    通过将 Grid 另存为 xls 文件再读取的方式获取 grid 内容
    """

    def __init__(self, tmp_folder: Optional[str] = None):
        """
        :param tmp_folder: 用于保存临时文件的文件夹
        """
        super().__init__()
        self.tmp_folder = tmp_folder

    def get(self, control_id: int) -> List[Dict]:
        grid = self._get_grid(control_id)

        # ctrl+s 保存 grid 内容为 xls 文件
        self._set_foreground(grid)
        grid.type_keys("^s", set_foreground=False)

        # 等待弹窗（可能是验证码，也可能是“另存为”对话框）
        count = 15
        while count > 0:
            if self._trader.is_exist_pop_dialog():
                break
            self._trader.wait(0.2)
            count -= 1

        # 如果弹出验证码，先处理验证码
        self._handle_captcha_dialog()

        # 等待“另存为”对话框就绪
        if not self._trader.is_exist_pop_dialog():
            self._trader.wait(0.5)

        fd, temp_path = tempfile.mkstemp(
            suffix=".xls", prefix="easytrader_grid_", dir=self.tmp_folder
        )
        os.close(fd)
        # 删除占位空文件，避免弹出“是否覆盖”确认框
        try:
            os.remove(temp_path)
        except OSError:
            pass

        try:
            save_win = self._trader.app.top_window()
            self._set_foreground(save_win)

            # 填入保存路径（兼容 Edit1 与新版 Windows 文件名输入框）
            try:
                save_win.Edit1.set_edit_text(temp_path)
            except Exception:
                save_win.child_window(class_name="Edit", found_index=0).set_edit_text(
                    temp_path
                )

            self._trader.wait(0.15)
            save_win.type_keys("%{s}%{y}", set_foreground=False)

            # 轮询等待文件真正写入完成（最长等待 5 秒）
            deadline = time.time() + 5.0
            while time.time() < deadline:
                if os.path.exists(temp_path) and os.path.getsize(temp_path) > 0:
                    break
                if self._trader.is_exist_pop_dialog():
                    # 若出现覆盖确认框或提示框，尝试确认/关闭
                    try:
                        top = self._trader.app.top_window()
                        title = top.window_text()
                        if "确认另存为" in title:
                            top.type_keys("%{y}", set_foreground=False)
                        elif "另存为" not in title:
                            top.Button2.click()
                    except Exception:
                        pass
                self._trader.wait(0.2)

            if not os.path.exists(temp_path):
                raise exceptions.TradeError(
                    f"同花顺表格导出超时，未生成临时文件: {temp_path}"
                )

            return self._format_grid_data(temp_path)
        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

    def _format_grid_data(self, data: str) -> List[Dict]:
        with open(data, encoding="gbk", errors="replace") as f:
            content = f.read()
        return self._parse_tsv_content(content)
