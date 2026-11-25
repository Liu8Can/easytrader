# coding:utf-8
from pywinauto import win32defines

try:
    from pywinauto.win32functions import (
        SetForegroundWindow as _raw_set_foreground_window,
        ShowWindow as _raw_show_window,
    )
    _USING_WIN32GUI = False
except ImportError:
    from win32gui import (
        SetForegroundWindow as _raw_set_foreground_window,
        ShowWindow as _raw_show_window,
    )
    _USING_WIN32GUI = True


def _extract_hwnd(window_or_hwnd):
    """Extract integer HWND from a pywinauto wrapper/spec or return int directly."""
    if isinstance(window_or_hwnd, int):
        return window_or_hwnd
    if hasattr(window_or_hwnd, "handle"):
        return int(window_or_hwnd.handle)
    if hasattr(window_or_hwnd, "wrapper_object"):
        return int(window_or_hwnd.wrapper_object().handle)
    return window_or_hwnd


def SetForegroundWindow(window_or_hwnd):
    """Cross-version safe SetForegroundWindow accepting HWND or pywinauto wrapper."""
    target = _extract_hwnd(window_or_hwnd) if _USING_WIN32GUI else window_or_hwnd
    return _raw_set_foreground_window(target)


def ShowWindow(window_or_hwnd, cmd_show):
    """Cross-version safe ShowWindow accepting HWND or pywinauto wrapper."""
    target = _extract_hwnd(window_or_hwnd) if _USING_WIN32GUI else window_or_hwnd
    return _raw_show_window(target, cmd_show)
