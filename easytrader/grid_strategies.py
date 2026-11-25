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
            control_id=control_id, class_name="CVirtualGridCtrl"
        )
        return grid

    def _set_foreground(self, grid=None):
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
        检测并尝试自动处理同花顺复制/导出表格时弹出的验证码弹窗。
        返回 True 表示无验证码弹窗或验证码已通过；若识别失败则关闭弹窗并抛出 TradeError。
        """
        top = self._trader.app.top_window()
        try:
            has_captcha = top.child_window(
                class_name="Static", title_re=".*验证码.*"
            ).exists(timeout=0.6)
        except Exception:
            has_captcha = False

        if not has_captcha:
            return True

        logger.warning("检测到同花顺验证码弹窗，尝试自动识别...")
        fd, file_path = tempfile.mkstemp(suffix=".png", prefix="easytrader_captcha_")
        os.close(fd)
        found = False
        last_err = None
        try:
            count = max_retry
            while count > 0:
                top = self._trader.app.top_window()
                captcha_ctrl = top.child_window(control_id=0x965, class_name="Static")
                if not captcha_ctrl.exists(timeout=0.5):
                    found = True
                    break

                captcha_ctrl.capture_as_image().save(file_path)
                try:
                    captcha_num = captcha_recognize(file_path).strip()
                except Exception as ex:
                    last_err = ex
                    logger.error("验证码 OCR 识别异常（可能未安装 tesseract）: %s", ex)
                    break

                captcha_num = "".join(captcha_num.split())
                logger.info("captcha result-->%s", captcha_num)
                if len(captcha_num) == 4:
                    editor = top.child_window(control_id=0x964, class_name="Edit")
                    editor.set_focus()
                    self._trader.wait(0.1)
                    self._trader.type_edit_control_keys(editor, captcha_num)
                    self._trader.wait(0.1)
                    top.type_keys("{ENTER}", pause=0.1)
                    if not editor.exists(timeout=1):
                        logger.info("验证码验证成功-->%s", captcha_num)
                        found = True
                        break
                count -= 1
                self._trader.wait(0.1)
                try:
                    captcha_ctrl.click()
                except Exception:
                    pass

            if not found:
                try:
                    self._trader.app.top_window().Button2.click()
                except Exception:
                    try:
                        self._trader.app.top_window().close()
                    except Exception:
                        pass
                detail = f" ({last_err})" if last_err else ""
                raise exceptions.TradeError(
                    f"同花顺弹出验证码，需要人工输入验证码后重试{detail}（建议减少高频刷新或在客户端手动完成验证）"
                )
            return True
        finally:
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except OSError:
                    pass

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
    通过发送 WM_COMMAND(0xE122) 复制 grid 内容到剪切板再读取
    """

    def get(self, control_id: int) -> List[Dict]:
        grid = self._get_grid(control_id)
        grid.post_message(win32defines.WM_COMMAND, 0xE122, 0)
        self._trader.wait(0.2)
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
