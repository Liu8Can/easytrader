# -*- coding: utf-8 -*-
"""``easytrader.utils.ths_no_check`` 单元测试（纯文件级，不需要同花顺客户端）。"""

import pytest

from easytrader.utils import ths_no_check

INI_TEMPLATE = (
    b"[26_WT_SERVER]\r\n"
    b"LAST_LOCALSP=0\r\n"
    b"\r\n"
    b"[AI_TRADE]\r\n"
    b"AI_CONNECT_TIME_OUT=10000\r\n"
    b"QS_LOGINTYPES=dnZT0fUFdldO0nIDdsD=\r\n"
    b"\r\n"
    b"[OTHER]\r\n"
    b"FOO=bar\r\n"
)


def _write(tmp_path, payload=INI_TEMPLATE):
    path = tmp_path / "xiadan.ini"
    path.write_bytes(payload)
    return path


def test_read_returns_none_when_key_absent(tmp_path):
    ini = _write(tmp_path)
    assert ths_no_check.read_no_check_sor(ini) is None


def test_inserts_key_into_existing_section(tmp_path):
    ini = _write(tmp_path)
    result = ths_no_check.ensure_no_check_sor(ini)

    assert result["changed"] is True
    assert result["previous"] is None
    assert ths_no_check.read_no_check_sor(ini) == "1"
    data = ini.read_bytes()
    # 键落在 [AI_TRADE] 段内，且不影响其他段
    section = data.split(b"[AI_TRADE]")[1].split(b"[OTHER]")[0]
    assert b"no_check_sorc=1" in section
    assert b"QS_LOGINTYPES=dnZT0fUFdldO0nIDdsD=" in data
    assert b"FOO=bar" in data


def test_is_idempotent_and_keeps_bytes_stable(tmp_path):
    ini = _write(tmp_path)
    ths_no_check.ensure_no_check_sor(ini)
    first = ini.read_bytes()

    second = ths_no_check.ensure_no_check_sor(ini)
    assert second["changed"] is False
    assert ini.read_bytes() == first


def test_updates_existing_value_in_place(tmp_path):
    payload = b"[AI_TRADE]\r\nno_check_sorc=0\r\nOTHER=1\r\n"
    ini = _write(tmp_path, payload)
    result = ths_no_check.ensure_no_check_sor(ini, value="1")

    assert result["previous"] == "0"
    assert result["changed"] is True
    assert ths_no_check.read_no_check_sor(ini) == "1"
    assert ini.read_bytes().count(b"no_check_sorc") == 1


def test_appends_section_when_missing(tmp_path):
    ini = _write(tmp_path, b"[ONLY]\r\nA=1\r\n")
    ths_no_check.ensure_no_check_sor(ini)

    assert ths_no_check.read_no_check_sor(ini) == "1"
    assert b"[AI_TRADE]" in ini.read_bytes()


def test_preserves_lf_line_endings(tmp_path):
    ini = _write(tmp_path, b"[AI_TRADE]\nA=1\n")
    ths_no_check.ensure_no_check_sor(ini)
    data = ini.read_bytes()
    assert b"\r\n" not in data
    assert b"no_check_sorc=1\n" in data


def test_backup_created_once(tmp_path):
    ini = _write(tmp_path)
    result = ths_no_check.ensure_no_check_sor(ini)
    assert result["backup"]
    backup = tmp_path / result["backup"].split("\\")[-1]
    assert backup.exists()
    assert backup.read_bytes() == INI_TEMPLATE


def test_restore_sets_value_back_to_zero(tmp_path):
    ini = _write(tmp_path)
    ths_no_check.ensure_no_check_sor(ini)
    ths_no_check.restore(ini)
    assert ths_no_check.read_no_check_sor(ini) == "0"


def test_missing_ini_raises():
    with pytest.raises(ths_no_check.NoCheckSorError):
        ths_no_check.ensure_no_check_sor(None)


def test_find_xiadan_ini_from_exe_path(tmp_path):
    ini = _write(tmp_path)
    exe = tmp_path / "xiadan.exe"
    exe.write_bytes(b"MZ")
    assert ths_no_check.find_xiadan_ini(exe) == ini
