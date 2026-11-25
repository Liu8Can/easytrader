# -*- coding: utf-8 -*-
import abc
import functools
import logging
import os
import re
import sys
import time
from typing import Type, Union

import hashlib, binascii

import easyutils
from pywinauto import findwindows, timings

from easytrader import exceptions, grid_strategies, pop_dialog_handler, refresh_strategies
from easytrader.config import client
from easytrader.grid_strategies import IGridStrategy
from easytrader.log import logger
from easytrader.refresh_strategies import IRefreshStrategy
from easytrader.utils.misc import file2dict
from easytrader.utils.perf import perf_clock
from easytrader.utils.win_gui import SetForegroundWindow, ShowWindow, win32defines

if not sys.platform.startswith("darwin"):
    import pywinauto
    import pywinauto.clipboard

class IClientTrader(abc.ABC):
    @property
    @abc.abstractmethod
    def app(self):
        """Return current app instance"""
        pass

    @property
    @abc.abstractmethod
    def main(self):
        """Return current main window instance"""
        pass

    @property
    @abc.abstractmethod
    def config(self):
        """Return current config instance"""
        pass

    @abc.abstractmethod
    def wait(self, seconds: float):
        """Wait for operation return"""
        pass

    @abc.abstractmethod
    def refresh(self):
        """Refresh data"""
        pass

    @abc.abstractmethod
    def is_exist_pop_dialog(self):
        pass


class ClientTrader(IClientTrader):
    _editor_need_type_keys = False
    # The strategy to use for getting grid data
    grid_strategy: Union[IGridStrategy, Type[IGridStrategy]] = grid_strategies.Copy
    _grid_strategy_instance: IGridStrategy = None
    refresh_strategy: IRefreshStrategy = refresh_strategies.Switch()

    def enable_type_keys_for_editor(self):
        """
        有些客户端无法通过 set_edit_text 方法输入内容，可以通过使用 type_keys 方法绕过
        """
        self._editor_need_type_keys = True

    @property
    def grid_strategy_instance(self):
        if self._grid_strategy_instance is None:
            self._grid_strategy_instance = (
                self.grid_strategy
                if isinstance(self.grid_strategy, IGridStrategy)
                else self.grid_strategy()
            )
            self._grid_strategy_instance.set_trader(self)
        return self._grid_strategy_instance

    def __init__(self):
        self._config = client.create(self.broker_type)
        self._app = None
        self._main = None
        self._toolbar = None
        self._background_mode = False
        self._exe_path = None
        self._no_check_sorc = None

    def enable_background_mode(self, enabled: bool = True):
        """开启无感后台模式：通过 Win32 消息交互，避免每次操作把同花顺强行弹到最前端抢焦点。"""
        self._background_mode = bool(enabled)

    def enable_captcha_bypass(self, value: str = "1"):
        """关闭同花顺「复制/保存表格数据」风控验证码，让 grid 复制/导出恢复可用。

        实现方式是写客户端自己的配置 ``<安装目录>\\xiadan.ini`` 的
        ``[AI_TRADE] no_check_sorc=1``：xiadan.exe 的复制/保存闸门函数
        (VA 0x246b530) 每次操作都会重新读取该键，读到首字符 ``'1'`` 即直接放行。

        优点：不改二进制、不注入、不受 VMProtect 校验影响、无需重启客户端，
        且表格数据仍由客户端自己导出，取值精确。详见
        ``easytrader/utils/ths_no_check.py`` 与 ``docs/ths_captcha_bypass.md``。

        :param value: ``"1"`` 关闭安全检查（默认）；``"0"`` 恢复检查。
        :return: 写入结果 dict（path / changed / previous / value / backup）
        """
        from easytrader.utils import ths_no_check

        exe_path = self._exe_path or self._config.DEFAULT_EXE_PATH or None
        try:
            result = ths_no_check.ensure_no_check_sor(
                ths_no_check.find_xiadan_ini(exe_path=exe_path), value=value
            )
        except ths_no_check.NoCheckSorError as ex:
            logger.warning("关闭同花顺导出安全检查失败: %s", ex)
            raise exceptions.TradeError(str(ex)) from ex

        self._no_check_sorc = result
        if result["changed"]:
            logger.info(
                "已写入同花顺导出安全检查开关: %s (%s -> %s, 无需重启客户端)",
                result["path"],
                result["previous"],
                result["value"],
            )
        else:
            logger.info(
                "同花顺导出安全检查已处于 no_check_sorc=%s，跳过: %s",
                result["value"],
                result["path"],
            )
        return result

    @property
    def captcha_bypass_state(self):
        """最近一次 ``enable_captcha_bypass`` 的结果（未调用则为 None）。"""
        return self._no_check_sorc

    @property
    def app(self):
        return self._app

    @property
    def main(self):
        return self._main

    @property
    def config(self):
        return self._config

    def connect(self, exe_path=None, title=None, process=None, **kwargs):
        """
        直接连接已手动登录好的同花顺客户端（无需在配置中写死账号密码）。

        优先级：
        1. 指定 process (PID)
        2. 指定 exe_path（若文件存在）
        3. 指定 title 正则
        4. 自动匹配已登录的“网上股票交易系统5.0”主窗口，或回退到 DEFAULT_EXE_PATH
        """
        self._get_left_menus_handle.cache_clear()
        connect_path = exe_path or self._config.DEFAULT_EXE_PATH or None

        if process is not None:
            self._app = pywinauto.Application(backend="win32").connect(
                process=int(process), timeout=10
            )
        elif connect_path and os.path.exists(connect_path):
            self._app = pywinauto.Application(backend="win32").connect(
                path=connect_path, timeout=10
            )
        elif title:
            self._app = pywinauto.Application(backend="win32").connect(
                title_re=title, timeout=10
            )
        else:
            # 自动探测已登录的同花顺下单窗口（兼容不同安装目录，如 D:\同花顺\ 或 D:\同花顺软件\同花顺\）
            connected = False
            for title_pattern in (
                f".*{re.escape(self._config.TITLE)}.*",
                r".*网上股票交易系统.*",
            ):
                try:
                    self._app = pywinauto.Application(backend="win32").connect(
                        title_re=title_pattern, timeout=5
                    )
                    connected = True
                    break
                except Exception:
                    continue
            if not connected:
                raise ValueError(
                    "未找到已登录的同花顺下单窗口（网上股票交易系统5.0）。"
                    "请先手动启动并登录 xiadan.exe，或显式传入 exe_path 参数。"
                )

        self._close_prompt_windows()
        self._main = self._resolve_main_window()
        self._init_toolbar()
        self._exe_path = connect_path or None

    def _resolve_main_window(self):
        """优先定位“网上股票交易系统”主窗口，避免 top_window() 误拿到气泡/弹窗。"""
        for title_pattern in (
            f".*{re.escape(self._config.TITLE)}.*",
            r".*网上股票交易系统.*",
        ):
            try:
                win = self._app.window(title_re=title_pattern)
                if win.exists(timeout=1):
                    return win
            except Exception:
                continue
        try:
            return self._app.top_window()
        except RuntimeError as ex:
            if "No windows for that process could be found" in str(ex):
                is_admin = False
                try:
                    import ctypes

                    is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
                except Exception:
                    pass
                raise RuntimeError(
                    f"{ex}。可能原因：(1) xiadan.exe 未登录或最小化在托盘；"
                    f"(2) Windows UIPI 权限隔离——当前 Python 管理员权限={is_admin}，"
                    f"若同花顺以管理员身份运行，请以同等权限运行 Python/MCP 客户端，或取消同花顺的管理员权限。"
                ) from ex
            raise

    @property
    def broker_type(self):
        return "ths"

    @property
    def balance(self):
        self._switch_left_menus(["查询[F4]", "资金股票"])

        return self._get_balance_from_statics()

    def _init_toolbar(self):
        try:
            self._toolbar = self._main.child_window(class_name="ToolbarWindow32")
        except Exception as ex:
            logger.debug("init toolbar ignored: %s", ex)
            self._toolbar = None

    def _ensure_main_window_ready(self):
        """确保主窗口未处于最小化状态；后台模式下不抢前台焦点（修复 Issue #506 / #457）。"""
        if self._main is None:
            return
        try:
            if self._main.has_style(win32defines.WS_MINIMIZE):
                # 后台模式使用 SW_SHOWNOACTIVATE(4) 静默还原不抢焦点；前台模式使用 SW_RESTORE(9)
                cmd = 4 if self._background_mode else 9
                ShowWindow(self._main.wrapper_object(), cmd)
                self.wait(0.2)
            if not self._background_mode:
                SetForegroundWindow(self._main.wrapper_object())
        except Exception as ex:
            logger.debug("restore/foreground main window ignored: %s", ex)

    def _find_visible_child(self, control_id: int, class_name: str, timeout: float = 1.5):
        """
        绕过 pywinauto .exists() 内部强行将 visible_only 设为 False 的坑，
        通过 descendants() + is_visible() 精准定位当前激活面板中的唯一可见控件。
        """
        deadline = time.time() + timeout
        while True:
            try:
                matches = [
                    ctrl
                    for ctrl in self._main.descendants(class_name=class_name)
                    if ctrl.control_id() == control_id and ctrl.is_visible()
                ]
                if matches:
                    return matches[0]
            except Exception as ex:
                logger.debug(
                    "find visible child (%s, %s) retry: %s", control_id, class_name, ex
                )
            if time.time() >= deadline:
                return None
            self.wait(0.1)

    def _get_balance_from_statics(self, max_retry: int = 3):
        """读取资金股票页面的静态文本字段，处理全 0 或可用金额暂为 0 但总资产非 0 的刷新延迟（修复 Issue #529）。"""
        result = {}
        for attempt in range(max_retry):
            result = {}
            for key, control_id in self._config.BALANCE_CONTROL_ID_GROUP.items():
                try:
                    ctrl = self._find_visible_child(control_id, "Static", timeout=1.0)
                    if ctrl is None:
                        continue
                    raw_text = ctrl.window_text().replace(",", "").strip()
                    if raw_text in ("", "--", "-"):
                        result[key] = 0.0
                    else:
                        result[key] = float(raw_text)
                except Exception as ex:
                    logger.warning(
                        "读取资金字段 %s (control_id=%s) 失败: %s", key, control_id, ex
                    )

            total_assets = result.get("总资产", 0.0)
            available = result.get("可用金额", 0.0)
            balance_val = result.get("资金余额", 0.0)
            all_zero = all(v == 0.0 for v in result.values())
            partial_zero_bug = (available == 0.0) and (total_assets > 0.0 or balance_val > 0.0)

            if (not all_zero and not partial_zero_bug) or attempt == max_retry - 1:
                return result
            logger.debug(
                "检测到资金字段可能尚未刷新完成 (result=%s)，等待 0.3s 重试 (%d/%d)...",
                result,
                attempt + 1,
                max_retry,
            )
            self.wait(0.3)
        return result

    @property
    def position(self):
        self._switch_left_menus(["查询[F4]", "资金股票"])

        return self._get_grid_data(self._config.COMMON_GRID_CONTROL_ID)

    @property
    def today_entrusts(self):
        self._switch_left_menus(["查询[F4]", "当日委托"])

        return self._get_grid_data(self._config.COMMON_GRID_CONTROL_ID)

    @property
    def today_trades(self):
        self._switch_left_menus(["查询[F4]", "当日成交"])

        return self._get_grid_data(self._config.COMMON_GRID_CONTROL_ID)

    @property
    def cancel_entrusts(self):
        self.refresh()
        self._switch_left_menus(["撤单[F3]"])

        return self._get_grid_data(self._config.COMMON_GRID_CONTROL_ID)

    @perf_clock
    def cancel_entrust(self, entrust_no):
        self.refresh()
        target_no = str(entrust_no).strip()
        for i, entrust in enumerate(self.cancel_entrusts):
            field_val = str(
                entrust.get(self._config.CANCEL_ENTRUST_ENTRUST_FIELD, "")
            ).strip()
            if field_val == target_no:
                self._cancel_entrust_by_double_click(i)
                return self._handle_pop_dialogs()
        return {"message": "委托单状态错误不能撤单, 该委托单可能已经成交或者已撤"}

    def cancel_all_entrusts(self):
        self.refresh()
        self._switch_left_menus(["撤单[F3]"])

        # 点击全部撤销控件
        self._app.top_window().child_window(
            control_id=self._config.TRADE_CANCEL_ALL_ENTRUST_CONTROL_ID, class_name="Button", title_re="""全撤.*"""
        ).click()
        self.wait(0.2)

        # 等待出现 确认兑换框
        if self.is_exist_pop_dialog():
            # 点击是 按钮
            w = self._app.top_window()
            if w is not None:
                btn = w["是(Y)"]
                if btn is not None:
                    btn.click()
                    self.wait(0.2)

        # 如果出现了确认窗口
        self.close_pop_dialog()

    @perf_clock
    def repo(self, security, price, amount, **kwargs):
        self._switch_left_menus(["债券回购", "融资回购（正回购）"])

        return self.trade(security, price, amount)

    @perf_clock
    def reverse_repo(self, security, price, amount, **kwargs):
        self._switch_left_menus(["债券回购", "融劵回购（逆回购）"])

        return self.trade(security, price, amount)

    @perf_clock
    def buy(self, security, price, amount, dry_run=False, **kwargs):
        self._switch_left_menus(["买入[F1]"])

        return self.trade(security, price, amount, dry_run=dry_run)

    @perf_clock
    def sell(self, security, price, amount, dry_run=False, **kwargs):
        self._switch_left_menus(["卖出[F2]"])

        return self.trade(security, price, amount, dry_run=dry_run)

    @perf_clock
    def market_buy(self, security, amount, ttype=None, limit_price=None, **kwargs):
        """
        市价买入
        :param security: 六位证券代码
        :param amount: 交易数量
        :param ttype: 市价委托类型，默认客户端默认选择，
                     深市可选 ['对手方最优价格', '本方最优价格', '即时成交剩余撤销', '最优五档即时成交剩余 '全额成交或撤销']
                     沪市可选 ['最优五档成交剩余撤销', '最优五档成交剩余转限价']
        :param limit_price: 科创板 限价

        :return: {'entrust_no': '委托单号'}
        """
        self._switch_left_menus(["市价委托", "买入"])

        return self.market_trade(security, amount, ttype, limit_price=limit_price)

    @perf_clock
    def market_sell(self, security, amount, ttype=None, limit_price=None, **kwargs):
        """
        市价卖出
        :param security: 六位证券代码
        :param amount: 交易数量
        :param ttype: 市价委托类型，默认客户端默认选择，
                     深市可选 ['对手方最优价格', '本方最优价格', '即时成交剩余撤销', '最优五档即时成交剩余 '全额成交或撤销']
                     沪市可选 ['最优五档成交剩余撤销', '最优五档成交剩余转限价']
        :param limit_price: 科创板 限价
        :return: {'entrust_no': '委托单号'}
        """
        self._switch_left_menus(["市价委托", "卖出"])

        return self.market_trade(security, amount, ttype, limit_price=limit_price)

    def market_trade(self, security, amount, ttype=None, limit_price=None, **kwargs):
        """
        市价交易
        :param security: 六位证券代码
        :param amount: 交易数量
        :param ttype: 市价委托类型，默认客户端默认选择，
                     深市可选 ['对手方最优价格', '本方最优价格', '即时成交剩余撤销', '最优五档即时成交剩余 '全额成交或撤销']
                     沪市可选 ['最优五档成交剩余撤销', '最优五档成交剩余转限价']

        :return: {'entrust_no': '委托单号'}
        """
        code = security[-6:]
        self._type_edit_control_keys(self._config.TRADE_SECURITY_CONTROL_ID, code)
        if ttype is not None:
            retry = 0
            retry_max = 10
            while retry < retry_max:
                try:
                    self._set_market_trade_type(ttype)
                    break
                except:
                    retry += 1
                    self.wait(0.1)
        self._set_market_trade_params(security, amount, limit_price=limit_price)
        self._submit_trade()

        return self._handle_pop_dialogs(
            handler_class=pop_dialog_handler.TradePopDialogHandler
        )

    def _set_market_trade_type(self, ttype):
        """根据选择的市价交易类型选择对应的下拉选项"""
        selects = self._main.child_window(
            control_id=self._config.TRADE_MARKET_TYPE_CONTROL_ID, class_name="ComboBox"
        )
        for i, text in enumerate(selects.texts()):
            # skip 0 index, because 0 index is current select index
            if i == 0:
                if re.search(ttype, text):  # 当前已经选中
                    return
                else:
                    continue
            if re.search(ttype, text):
                selects.select(i - 1)
                return
        raise TypeError("不支持对应的市价类型: {}".format(ttype))

    def _set_stock_exchange_type(self, ttype):
        """根据选择的市场类型选择对应的下拉选项（新版同花顺已取消该下拉框，不存在时安全跳过，修复 Issue #514 / #524）。"""
        selects = self._main.child_window(
            control_id=self._config.TRADE_STOCK_EXCHANGE_CONTROL_ID, class_name="ComboBox"
        )
        if not selects.exists(timeout=0.5):
            logger.info(
                "当前同花顺客户端不存在交易所选择下拉框 (control_id=%s)，已自动跳过",
                self._config.TRADE_STOCK_EXCHANGE_CONTROL_ID,
            )
            return

        for i, text in enumerate(selects.texts()):
            # skip 0 index, because 0 index is current select index
            if i == 0:
                if ttype.strip() == text.strip():  # 当前已经选中
                    return
                else:
                    continue
            if ttype.strip() == text.strip():
                selects.select(i - 1)
                return
        raise TypeError("不支持对应的市场类型: {}".format(ttype))

    def auto_ipo(self):
        self._switch_left_menus(self._config.AUTO_IPO_MENU_PATH)

        stock_list = self._get_grid_data(self._config.COMMON_GRID_CONTROL_ID)

        if len(stock_list) == 0:
            return {"message": "今日无新股"}
        invalid_list_idx = [
            i for i, v in enumerate(stock_list) if v[self.config.AUTO_IPO_NUMBER] <= 0
        ]

        if len(stock_list) == len(invalid_list_idx):
            return {"message": "没有发现可以申购的新股"}

        self._click(self._config.AUTO_IPO_SELECT_ALL_BUTTON_CONTROL_ID)
        self.wait(0.1)

        for row in invalid_list_idx:
            self._click_grid_by_row(row)
        self.wait(0.1)

        self._click(self._config.AUTO_IPO_BUTTON_CONTROL_ID)
        self.wait(0.1)

        return self._handle_pop_dialogs()

    def _click_grid_by_row(self, row):
        x = self._config.COMMON_GRID_LEFT_MARGIN
        y = (
            self._config.COMMON_GRID_FIRST_ROW_HEIGHT
            + self._config.COMMON_GRID_ROW_HEIGHT * row
        )
        self._app.top_window().child_window(
            control_id=self._config.COMMON_GRID_CONTROL_ID,
            class_name="CVirtualGridCtrl",
        ).click(coords=(x, y))

    @perf_clock
    def is_exist_pop_dialog(self):
        self.wait(0.5)  # wait dialog display
        try:
            return (
                self._main.wrapper_object() != self._app.top_window().wrapper_object()
            )
        except (
            findwindows.ElementNotFoundError,
            timings.TimeoutError,
            RuntimeError,
        ) as ex:
            logger.exception("check pop dialog timeout")
            return False

    @perf_clock
    def close_pop_dialog(self):
        try:
            if self._main.wrapper_object() != self._app.top_window().wrapper_object():
                w = self._app.top_window()
                if w is not None:
                    w.close()
                    self.wait(0.2)
        except (
                findwindows.ElementNotFoundError,
                timings.TimeoutError,
                RuntimeError,
        ) as ex:
            pass

    def _run_exe_path(self, exe_path):
        return os.path.join(os.path.dirname(exe_path), "xiadan.exe")

    def wait(self, seconds):
        time.sleep(seconds)

    def exit(self):
        self._app.kill()

    def _close_prompt_windows(self):
        self.wait(0.3)
        self._dismiss_non_trade_popups()
        self.wait(0.2)

    def _dismiss_non_trade_popups(self):
        """检测并安全关闭同花顺广告、公告、新股提醒、成交回报等非交易确认弹窗（修复 Issue #376）。"""
        if self._app is None:
            return
        try:
            windows = self._app.windows(class_name="#32770", visible_only=True)
        except Exception:
            return
        for window in windows:
            try:
                title = window.window_text()
                if pop_dialog_handler.is_non_trade_popup_title(title, self._config.TITLE):
                    logger.info("安全关闭非交易干扰弹窗: %r", title)
                    window.close()
                    self.wait(0.15)
            except Exception as ex:
                logger.debug("关闭干扰弹窗忽略异常: %s", ex)

    def close_pormpt_window_no_wait(self):
        self._dismiss_non_trade_popups()

    def _verify_entrust_in_today_entrusts(self, code: str, price_str: str, amount: int):
        """下单后若未检测到确认弹窗，反查当日委托核实是否实际生成委托单（修复 Issue #457）。"""
        try:
            entrusts = self.today_entrusts or []
            target_price = float(price_str)
            target_amount = int(amount)
            # 倒序查找最新一条匹配的委托
            for row in reversed(entrusts):
                row_code = str(row.get("证券代码", "")).strip()[-6:]
                if row_code != code:
                    continue
                try:
                    row_price = float(str(row.get("委托价格", "0")).replace(",", "").strip())
                    row_amount = int(float(str(row.get("委托数量", "0")).replace(",", "").strip()))
                except (TypeError, ValueError):
                    continue
                if abs(row_price - target_price) < 1e-4 and row_amount == target_amount:
                    entrust_no = str(
                        row.get("合同编号") or row.get("委托编号") or ""
                    ).strip()
                    if entrust_no:
                        return {
                            "entrust_no": entrust_no,
                            "verified_by": "today_entrusts",
                            "message": "通过当日委托反查确认委托单已生成",
                        }
        except Exception as ex:
            logger.warning("反查当日委托失败: %s", ex)
        return None

    def trade(self, security, price, amount, dry_run=False):
        code = str(security)[-6:]
        price_str = str(easyutils.round_price_by_code(float(price), code))
        amount_int = int(amount)

        if dry_run:
            self._ensure_main_window_ready()
            self._dismiss_non_trade_popups()
            # 通过 descendants()+is_visible() 精准检查当前激活面板，避开 pywinauto .exists() 强设 visible_only=False 的坑
            submit_ctrl = self._find_visible_child(
                self._config.TRADE_SUBMIT_CONTROL_ID, "Button", timeout=1.5
            )
            code_ctrl = self._find_visible_child(
                self._config.TRADE_SECURITY_CONTROL_ID, "Edit", timeout=1.0
            )
            price_ctrl = self._find_visible_child(
                self._config.TRADE_PRICE_CONTROL_ID, "Edit", timeout=1.0
            )
            amount_ctrl = self._find_visible_child(
                self._config.TRADE_AMOUNT_CONTROL_ID, "Edit", timeout=1.0
            )
            submit_exists = submit_ctrl is not None
            code_exists = code_ctrl is not None
            price_exists = price_ctrl is not None
            amount_exists = amount_ctrl is not None
            logger.info(
                "[DRY_RUN] 跳过实际下单点击: code=%s price=%s amount=%s controls_ready=(submit=%s,code=%s,price=%s,amount=%s)",
                code,
                price_str,
                amount_int,
                submit_exists,
                code_exists,
                price_exists,
                amount_exists,
            )
            return {
                "dry_run": True,
                "security": code,
                "price": float(price_str),
                "price_text": price_str,
                "amount": amount_int,
                "submit_button_ready": submit_exists,
                "submit_button_text": submit_ctrl.window_text() if submit_ctrl else "",
                "edit_controls_ready": {
                    "code": code_exists,
                    "price": price_exists,
                    "amount": amount_exists,
                },
                "message": "DRY_RUN 模式已开启：仅完成参数与当前可见面板控件校验，未点击确认下单",
            }

        self._set_trade_params(code, price_str, amount_int)
        self._submit_trade()

        result = self._handle_pop_dialogs(
            handler_class=pop_dialog_handler.TradePopDialogHandler
        )
        if result and result.get("entrust_no"):
            return result

        # 若未通过弹窗拿到委托单号（例如客户端关闭了确认弹窗），反查当日委托核实（Issue #457）
        verified = self._verify_entrust_in_today_entrusts(code, price_str, amount_int)
        if verified:
            return verified

        raise exceptions.TradeError(
            f"下单校验失败：点击下单后未收到委托确认弹窗，且在当日委托中也未查到匹配记录 "
            f"(code={code}, price={price_str}, amount={amount_int})。请人工核对客户端状态！"
        )

    def _click(self, control_id):
        self._app.top_window().child_window(
            control_id=control_id, class_name="Button"
        ).click()

    @perf_clock
    def _submit_trade(self):
        time.sleep(0.2)
        btn = self._find_visible_child(
            self._config.TRADE_SUBMIT_CONTROL_ID, "Button", timeout=2.0
        )
        if btn is None:
            raise exceptions.TradeError("未找到当前可见的下单确认按钮")
        btn.click()

    @perf_clock
    def __get_top_window_pop_dialog(self):
        return self._app.top_window().child_window(
            control_id=self._config.POP_DIALOD_TITLE_CONTROL_ID
        )

    @perf_clock
    def _get_pop_dialog_title(self):
        return (
            self._app.top_window()
            .child_window(control_id=self._config.POP_DIALOD_TITLE_CONTROL_ID)
            .window_text()
        )

    def _set_and_verify_edit(
        self, control_id: int, expected_text: str, field_name: str, is_float: bool = False
    ):
        """写入输入框并回读校验文本完全一致；若受输入法或行情回填干扰则自动采用剪贴板粘贴重试（修复 Issue #457 / #522）。"""
        editor = self._find_visible_child(control_id, "Edit", timeout=2.0)
        if editor is None:
            raise exceptions.TradeError(
                f"未找到可见的 {field_name} 输入框 (control_id={control_id})"
            )
        expected_str = str(expected_text).strip()

        def _matches(actual: str) -> bool:
            a = (actual or "").strip()
            if a == expected_str:
                return True
            if is_float:
                try:
                    # 确保小数点完整且数值一致（兼容 ETF 3 位小数，如 2.259）
                    return abs(float(a) - float(expected_str)) < 1e-6 and (
                        "." in a if "." in expected_str else True
                    )
                except ValueError:
                    return False
            return False

        # 第 1 次尝试：标准方式写入
        self.type_edit_control_keys(editor, expected_str)
        self.wait(0.08)
        actual = editor.window_text().strip()
        if _matches(actual):
            return

        # 第 2 次尝试：使用剪贴板粘贴（^a^v），彻底免疫中文输入法与小数点吞字问题
        logger.warning(
            "%s 输入框回读不一致 (actual=%r, expected=%r)，切换剪贴板粘贴重试...",
            field_name,
            actual,
            expected_str,
        )
        self._paste_into_edit(editor, expected_str)
        self.wait(0.1)
        actual = editor.window_text().strip()
        if not _matches(actual):
            raise exceptions.TradeError(
                f"{field_name} 输入框校验失败：期望 {expected_str!r}，实际控件文本为 {actual!r}，已中止下单！"
            )

    def _paste_into_edit(self, editor, text: str):
        """通过剪贴板全选粘贴写入输入框，避免中文输入法拦截小数点或数字。"""
        try:
            import pyperclip

            pyperclip.copy(str(text))
            editor.set_focus()
            editor.type_keys("^a^v", set_foreground=False)
        except Exception as ex:
            logger.debug("剪贴板粘贴回退到 set_edit_text: %s", ex)
            editor.set_edit_text(str(text))

    def _set_trade_params(self, security, price, amount):
        self._ensure_main_window_ready()
        self._dismiss_non_trade_popups()
        code = str(security)[-6:]
        price_str = str(easyutils.round_price_by_code(float(price), code))
        amount_str = str(int(amount))

        # 1. 写入并校验证券代码
        self._set_and_verify_edit(
            self._config.TRADE_SECURITY_CONTROL_ID, code, "证券代码", is_float=False
        )

        # 等待同花顺回填股票名称与最新行情价，避免后续覆写手动输入的价格（修复 Issue #522）
        self.wait(0.3)

        # 2. 写入并校验委托价格（兼容 ETF 3 位小数，如 2.259）
        self._set_and_verify_edit(
            self._config.TRADE_PRICE_CONTROL_ID, price_str, "委托价格", is_float=True
        )

        # 3. 写入并校验委托数量
        self._set_and_verify_edit(
            self._config.TRADE_AMOUNT_CONTROL_ID, amount_str, "委托数量", is_float=False
        )

        # 4. 点击下单前最终全量回读三项输入框，防止行情延迟回填篡改价格（修复 Issue #457 / #522）
        code_ed = self._find_visible_child(self._config.TRADE_SECURITY_CONTROL_ID, "Edit")
        price_ed = self._find_visible_child(self._config.TRADE_PRICE_CONTROL_ID, "Edit")
        amount_ed = self._find_visible_child(self._config.TRADE_AMOUNT_CONTROL_ID, "Edit")
        final_code = code_ed.window_text().strip() if code_ed else ""
        final_price = price_ed.window_text().strip() if price_ed else ""
        final_amount = amount_ed.window_text().strip() if amount_ed else ""
        if final_code != code or final_amount != amount_str:
            raise exceptions.TradeError(
                f"下单前最终核对失败：code={final_code!r}(期望{code!r}), "
                f"amount={final_amount!r}(期望{amount_str!r})，已中止下单！"
            )
        try:
            if abs(float(final_price) - float(price_str)) >= 1e-6:
                raise ValueError("price mismatch")
        except ValueError:
            # 若价格在输入数量瞬间被行情覆写，最后再纠正一次并复核
            self._set_and_verify_edit(
                self._config.TRADE_PRICE_CONTROL_ID, price_str, "委托价格", is_float=True
            )

    def _set_market_trade_params(self, security, amount, limit_price=None):
        self._type_edit_control_keys(
            self._config.TRADE_AMOUNT_CONTROL_ID, str(int(amount))
        )
        self.wait(0.1)
        price_control = None
        if str(security).startswith("68"):  # 科创板存在限价
            try:
                price_control = self._main.child_window(
                    control_id=self._config.TRADE_PRICE_CONTROL_ID, class_name="Edit"
                )
            except:
                pass
        if price_control is not None:
            price_control.set_edit_text(limit_price)

    def _get_grid_data(self, control_id):
        return self.grid_strategy_instance.get(control_id)

    def _type_keys(self, control_id, text):
        self._main.child_window(control_id=control_id, class_name="Edit").set_edit_text(
            text
        )

    def _type_edit_control_keys(self, control_id, text):
        editor = self._main.child_window(control_id=control_id, class_name="Edit")
        self.type_edit_control_keys(editor, str(text))

    def type_edit_control_keys(self, editor, text):
        text_str = str(text)
        if not self._editor_need_type_keys:
            editor.set_edit_text(text_str)
        else:
            editor.set_focus()
            editor.type_keys("^a{BACKSPACE}", set_foreground=False)
            self.wait(0.05)
            editor.type_keys(text_str, set_foreground=False)

    def _collapse_left_menus(self):
        items = self._get_left_menus_handle().roots()
        for item in items:
            item.collapse()

    @perf_clock
    def _switch_left_menus(self, path, sleep=0.2):
        self._ensure_main_window_ready()
        self.close_pormpt_window_no_wait()
        self.close_pop_dialog()
        try:
            self._get_left_menus_handle().get_item(path).select()
        except Exception as ex:
            # 容错：部分新版同花顺菜单名称略有差异，优先尝试常见快捷键回退
            shortcut_map = {
                ("买入[F1]",): "{F1}",
                ("卖出[F2]",): "{F2}",
                ("撤单[F3]",): "{F3}",
                ("查询[F4]", "资金股票"): "{F4}",
            }
            shortcut = shortcut_map.get(tuple(path))
            if shortcut:
                logger.warning(
                    "左侧菜单树定位 %s 失败 (%s)，回退使用快捷键 %s", path, ex, shortcut
                )
                self._main.type_keys(shortcut)
            else:
                raise
        if self._background_mode:
            # 后台模式通过 Win32 消息发送 F5 刷新（VK_F5 = 0x74），避免抢前台焦点
            try:
                self._main.post_message(win32defines.WM_KEYDOWN, 0x74, 0)
                self._main.post_message(win32defines.WM_KEYUP, 0x74, 0)
            except Exception as ex:
                logger.debug("background F5 post_message ignored: %s", ex)
        else:
            try:
                self._main.type_keys("{F5}")
            except Exception:
                self._app.top_window().type_keys("{F5}")
        self.wait(sleep)

    def _switch_left_menus_by_shortcut(self, shortcut, sleep=0.5):
        self.close_pop_dialog()
        self._app.top_window().type_keys(shortcut)
        self.wait(sleep)

    @functools.lru_cache()
    def _get_left_menus_handle(self):
        count = 2
        while True:
            try:
                handle = self._main.child_window(
                    control_id=129, class_name="SysTreeView32"
                )
                if count <= 0:
                    return handle
                # sometime can't find handle ready, must retry
                handle.wait("ready", 2)
                return handle
            # pylint: disable=broad-except
            except Exception as ex:
                logger.exception("error occurred when trying to get left menus")
            count = count - 1

    def _cancel_entrust_by_double_click(self, row):
        x = self._config.CANCEL_ENTRUST_GRID_LEFT_MARGIN
        y = (
            self._config.CANCEL_ENTRUST_GRID_FIRST_ROW_HEIGHT
            + self._config.CANCEL_ENTRUST_GRID_ROW_HEIGHT * row
        )
        self._app.top_window().child_window(
            control_id=self._config.COMMON_GRID_CONTROL_ID,
            class_name="CVirtualGridCtrl",
        ).double_click(coords=(x, y))

    def refresh(self):
        self.refresh_strategy.set_trader(self)
        self.refresh_strategy.refresh()

    @perf_clock
    def _handle_pop_dialogs(self, handler_class=pop_dialog_handler.PopDialogHandler):
        handler = handler_class(self._app)
        handled_any = False

        while self.is_exist_pop_dialog():
            handled_any = True
            try:
                title = self._get_pop_dialog_title()
            except pywinauto.findwindows.ElementNotFoundError:
                return {"message": "success"}

            result = handler.handle(title)
            if result:
                return result

        if not handled_any and handler_class is pop_dialog_handler.TradePopDialogHandler:
            logger.warning(
                "未检测到委托确认或成交结果弹窗（Issue #457），请核对当日委托并检查客户端设置"
            )
            return {
                "message": "no_popup_detected",
                "warning": "未检测到委托确认或结果弹窗，无法确认是否已下单，请通过当日委托核实并检查同花顺下单确认弹窗设置",
            }
        return {"message": "success"}


class BaseLoginClientTrader(ClientTrader):
    @abc.abstractmethod
    def login(self, user, password, exe_path, comm_password=None, **kwargs):
        """Login Client Trader"""
        pass

    def prepare(
        self,
        config_path=None,
        user=None,
        password=None,
        exe_path=None,
        comm_password=None,
        **kwargs
    ):
        """
        登陆客户端
        :param config_path: 登陆配置文件，跟参数登陆方式二选一
        :param user: 账号
        :param password: 明文密码
        :param exe_path: 客户端路径类似 r'C:\\htzqzyb2\\xiadan.exe', 默认 r'C:\\htzqzyb2\\xiadan.exe'
        :param comm_password: 通讯密码
        :return:
        """
        if config_path is not None:
            account = file2dict(config_path)
            user = account["user"]
            password = account["password"]
            comm_password = account.get("comm_password")
            exe_path = account.get("exe_path")
        self.login(
            user,
            password,
            exe_path or self._config.DEFAULT_EXE_PATH,
            comm_password,
            **kwargs
        )
        self._init_toolbar()
