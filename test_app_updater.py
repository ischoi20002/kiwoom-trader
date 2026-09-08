# -*- coding: utf-8 -*-
"""app_updater 버전 비교·요약 단위 테스트."""
from __future__ import annotations

from core.app_updater import (
    format_check_summary,
    parse_version_tuple,
    version_is_newer,
    version_from_release_tag,
    UpdateCheckResult,
    _parse_app_version_from_text,
    _pick_release_asset,
)


def test_parse_version_tuple() -> None:
    assert parse_version_tuple("0.18.99") == (0, 18, 99)
    assert parse_version_tuple("1.0") == (1, 0)
    assert parse_version_tuple("") == ()


def test_version_is_newer() -> None:
    assert version_is_newer("0.19.0", "0.18.99") is True
    assert version_is_newer("0.18.99", "0.19.0") is False
    assert version_is_newer("0.18.99", "0.18.99") is False
    assert version_is_newer("0.18.10", "0.18.9") is True


def test_parse_app_version_from_text() -> None:
    text = 'APP_VERSION: str = "0.19.0"\n'
    assert _parse_app_version_from_text(text) == "0.19.0"


def test_version_from_release_tag() -> None:
    assert version_from_release_tag("v0.19.8") == "0.19.8"
    assert version_from_release_tag("0.19.8") == "0.19.8"


def test_pick_release_asset() -> None:
    assets = [
        {"name": "notes.txt", "id": 1},
        {"name": "k-trader-0.19.9.zip", "id": 2},
        {"name": "other.zip", "id": 3},
    ]
    a = _pick_release_asset(assets, "0.19.9")
    assert a is not None
    assert a["id"] == 2


def test_format_check_summary_latest() -> None:
    r = UpdateCheckResult(
        ok=True,
        local_version="0.19.0",
        remote_version="0.19.0",
        update_available=False,
        remote_ref="github:owner/repo",
        release_tag="v0.19.0",
        message="최신입니다 (v0.19.0)",
    )
    s = format_check_summary(r)
    assert "0.19.0" in s
    assert "최신" in s
    assert "Release" in s or "github" in s


if __name__ == "__main__":
    test_parse_version_tuple()
    test_version_is_newer()
    test_parse_app_version_from_text()
    test_version_from_release_tag()
    test_pick_release_asset()
    test_format_check_summary_latest()
    print("ok")
