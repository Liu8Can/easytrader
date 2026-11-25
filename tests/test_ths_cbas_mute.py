# -*- coding: utf-8 -*-
"""``easytrader.utils.ths_cbas_mute`` 的纯逻辑单元测试（不需要客户端进程）。"""

import struct

import pytest

from easytrader.utils import ths_cbas_mute as mute


def test_sites_cover_copy_and_save():
    assert set(mute.SITES) == {"copy", "save"}
    assert mute.SITES["copy"] == 0x246B571
    assert mute.SITES["save"] == 0x2479D06


def test_patch_is_five_bytes_add_esp_0c():
    assert len(mute.PATCH_BYTES) == 5
    assert mute.PATCH_BYTES[:3] == b"\x83\xc4\x0c"  # add esp, 0Ch
    assert mute.PATCH_BYTES[3:] == b"\x90\x90"  # nop; nop


def test_call_target_decodes_relative_call():
    # 0x246b571: e8 0a 76 19 fe  -> 0x246b576 + 0xfe19760a
    code = bytes.fromhex("e80a7619fe")
    assert mute.call_target(0x246B571, code) == 0x602B80


def test_call_target_rejects_non_call():
    assert mute.call_target(0x246B571, b"\x90\x90\x90\x90\x90") is None
    assert mute.call_target(0x246B571, b"\x83\xc4\x0c\x90\x90") is None
    assert mute.call_target(0x246B571, b"\xe8") is None


def test_call_target_matches_send_message_signature():
    """两处站点的真实字节都应解到 CWnd::SendMessage。"""
    for site_va in mute.SITES.values():
        rel = (mute.SEND_MESSAGE_FN - (site_va + 5)) & 0xFFFFFFFF
        code = b"\xe8" + struct.pack("<i", rel if rel < 0x80000000 else rel - (1 << 32))
        assert mute.call_target(site_va, code) == mute.SEND_MESSAGE_FN


def test_status_returns_not_found_for_unusable_pid():
    result = mute.status(0)
    assert result["found"] is False


def test_apply_raises_for_missing_process():
    with pytest.raises(mute.CbasMuteError):
        mute.apply(0)
