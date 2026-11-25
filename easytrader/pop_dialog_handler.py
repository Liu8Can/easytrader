# coding:utf-8
import re
import time
from typing import Optional

from easytrader import exceptions
from easytrader.log import logger
from easytrader.utils.perf import perf_clock
from easytrader.utils.win_gui import SetForegroundWindow, ShowWindow, win32defines


NON_TRADE_POPUP_KEYWORDS = (
    "广告",
    "公告",
    "新股",
    "中签",
    "风险提示",
    "弹出窗口",
    "成交回报",
    "消息提示",
    "升级",
    "活动",
    "通知",
)


def is_non_trade_popup_title(title: str, main_title: str = "网上股票交易系统") -> bool:
    """判断弹窗标题是否属于非交易确认类干扰弹窗（广告/公告/新股提醒/成交回报等，Issue #376）。"""
    t = (title or "").strip()
    if main_title in t or "网上股票交易系统" in t:
        return False
    # 排除交易确认与验证码弹窗，避免误关正在进行的交易
    if any(k in t for k in ("委托确认", "撤单确认", "验证码", "另存为")):
        return False
    if t == "" or any(k in t for k in NON_TRADE_POPUP_KEYWORDS):
        return True
    return True


class PopDialogHandler:
    def __init__(self, app, background: bool = False):
        self._app = app
        #: 后台模式：不调用 SetForegroundWindow / type_keys（两者都会抢前台焦点）
        self._background = bool(background)

    @staticmethod
    def _set_foreground(window):
        try:
            if window.has_style(win32defines.WS_MINIMIZE):  # if minimized
                ShowWindow(window.wrapper_object(), 9)  # restore window state
            else:
                SetForegroundWindow(window.wrapper_object())  # bring to front
        except Exception as ex:
            logger.debug("set foreground failed: %s", ex)

    @perf_clock
    def handle(self, title):
        if any(s in title for s in {"提示信息", "委托确认", "网上交易用户协议", "撤单确认"}):
            self._submit_by_shortcut()
            return None

        if "提示" in title:
            content = self._extract_content()
            self._submit_by_click()
            return {"message": content}

        content = self._extract_content()
        self._close()
        return {"message": "unknown message: {}".format(content)}

    def _extract_content(self):
        top = self._app.top_window()
        # 优先尝试同花顺常见的弹窗内容控件 ID (1004)
        try:
            ctrl = top.child_window(control_id=1004, class_name="Static")
            if ctrl.exists(timeout=0.2):
                text = ctrl.window_text().strip()
                if text:
                    return text
        except Exception:
            pass

        # 回退：遍历所有 Static 子控件，避免多 Static 时触发 ElementAmbiguousError
        try:
            texts = []
            for ctrl in top.descendants(class_name="Static"):
                txt = ctrl.window_text().strip()
                if txt and txt not in {"提示", "提示信息", "委托确认", "撤单确认"}:
                    texts.append(txt)
            if texts:
                return " ".join(texts)
        except Exception:
            pass

        try:
            return top.Static.window_text()
        except Exception:
            return ""

    @staticmethod
    def _extract_entrust_id(content):
        match = re.search(r"[\da-zA-Z]+", content or "")
        return match.group() if match else ""

    def _submit_by_click(self):
        top = self._app.top_window()
        for btn_text in ("确定", "是(Y)", "是", "确定(Y)"):
            try:
                btn = top.child_window(title=btn_text, class_name="Button")
                if btn.exists(timeout=0.2):
                    btn.click()
                    return
            except Exception:
                continue

        # 兼容旧版 best_match 回退（替换已废弃的 Window_ / ChildWindow）
        try:
            top["确定"].click()
        except Exception:
            self._app.window(best_match="Dialog", top_level_only=True).child_window(
                best_match="确定"
            ).click()

    def _submit_by_shortcut(self):
        # 优先直接点击确认按钮（不依赖前台焦点与快捷键，后台/前台均稳定）
        try:
            self._submit_by_click()
            return
        except Exception as ex:
            logger.debug("直接点击确认按钮失败，回退 Alt+Y 快捷键: %s", ex)
        if self._background:
            return
        self._set_foreground(self._app.top_window())
        self._app.top_window().type_keys("%Y", set_foreground=True)

    def _close(self):
        self._app.top_window().close()


class TradePopDialogHandler(PopDialogHandler):
    @perf_clock
    def handle(self, title) -> Optional[dict]:
        if title == "委托确认":
            self._submit_by_shortcut()
            return None

        if title == "提示信息":
            content = self._extract_content()
            if "超出涨跌停" in content:
                self._submit_by_shortcut()
                return None

            if "委托价格的小数价格应为" in content:
                self._submit_by_shortcut()
                return None

            if "逆回购" in content:
                self._submit_by_shortcut()
                return None

            if "正回购" in content:
                self._submit_by_shortcut()
                return None

            return None

        if title == "提示":
            content = self._extract_content()
            if "成功" in content:
                entrust_no = self._extract_entrust_id(content)
                self._submit_by_click()
                return {"entrust_no": entrust_no, "message": content}

            self._submit_by_click()
            time.sleep(0.05)
            raise exceptions.TradeError(content)
        self._close()
        return None
