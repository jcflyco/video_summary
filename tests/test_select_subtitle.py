"""Unit tests for subtitle language selection (any-language fallback)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from fetch_video import (  # noqa: E402
    _rank_any_codes,
    pick_any_auto,
    pick_any_manual,
    select_subtitle,
)


def test_fallback_to_non_original_manual():
    info = {
        "language": "zh-Hant",
        "title": "用AI管理時間",
        "subtitles": {"en-US": [{"ext": "vtt"}]},
        "automatic_captions": {},
    }
    sel = select_subtitle(info, "youtube")
    assert sel is not None
    lang, kind, reason, is_auto = sel
    assert lang == "en-US"
    assert kind == "manual"
    assert is_auto is False
    assert "非原语言" in reason


def test_prefer_original_lang_auto_over_manual_other():
    """When zh auto exists, prefer it over en manual even if both present."""
    info = {
        "language": "zh-Hant",
        "title": "用AI管理時間",
        "subtitles": {"en-US": [{"ext": "vtt"}]},
        "automatic_captions": {
            "zh-Hans-en-US": [{"ext": "vtt"}],
            "zh-Hant-en-US": [{"ext": "vtt"}],
            "fr-en-US": [{"ext": "vtt"}],
        },
    }
    sel = select_subtitle(info, "youtube")
    assert sel is not None
    lang, kind, _reason, is_auto = sel
    assert lang == "zh-Hant-en-US"
    assert kind == "auto"
    assert is_auto is True


def test_original_manual_wins():
    info = {
        "language": "zh",
        "title": "测试",
        "subtitles": {"zh-Hant": [{"ext": "vtt"}], "en": [{"ext": "vtt"}]},
        "automatic_captions": {"zh-Hans": [{"ext": "vtt"}]},
    }
    sel = select_subtitle(info, "youtube")
    assert sel is not None
    lang, kind, _reason, is_auto = sel
    assert lang == "zh-Hant"
    assert kind == "manual"
    assert is_auto is False


def test_rank_prefers_common_source_langs():
    assert _rank_any_codes(["zu-en-US", "en-US", "zh-Hant-en-US"]) == "en-US"
    assert pick_any_manual({"fr": [], "en-US": []}) == "en-US"
    assert pick_any_auto({"ja-en-US": [], "en": []}, "youtube") == "en"


def test_empty_tracks_returns_none():
    info = {
        "language": "zh",
        "title": "测试",
        "subtitles": {},
        "automatic_captions": {},
    }
    assert select_subtitle(info, "youtube") is None
