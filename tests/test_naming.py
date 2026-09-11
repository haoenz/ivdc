"""文件名清洗与随机标识的规则覆盖。"""

from __future__ import annotations

import random

import pytest

from ivdc.naming import TAG_ALPHABET, new_tag, sanitize_filename


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("普通标题", "普通标题"),
        ("标题 / 带斜杠", "标题 _ 带斜杠"),
        ('a<b>c:d"e/f\\g|h?i*j', "a_b_c_d_e_f_g_h_i_j"),
        ("标题\t带\n换行", "标题 带 换行"),
        ("连续    空白", "连续 空白"),
        ("  前后空白  ", "前后空白"),
        ("结尾的点...", "结尾的点"),
        ("结尾空格   ", "结尾空格"),
        ("", "_"),
        ("...", "_"),
    ],
)
def test_sanitize_illegal_and_whitespace(raw: str, expected: str) -> None:
    assert sanitize_filename(raw) == expected


@pytest.mark.parametrize("raw", ["CON", "con", "Aux", "nul", "com1", "LPT9", "CON.txt"])
def test_reserved_device_names_get_prefixed(raw: str) -> None:
    assert sanitize_filename(raw).startswith("_")


@pytest.mark.parametrize("raw", ["CONSOLE", "COM10", "LPT0", "a.CON", "NULL"])
def test_non_reserved_names_untouched(raw: str) -> None:
    assert sanitize_filename(raw) == raw


def test_control_characters_replaced() -> None:
    assert sanitize_filename("a\x00b\x1fc") == "a_b_c"


# ---- 随机标识 -------------------------------------------------------------


def test_tag_length_and_alphabet() -> None:
    tag = new_tag()
    assert len(tag) == 6
    assert set(tag) <= set(TAG_ALPHABET)


def test_tag_honours_requested_length() -> None:
    assert len(new_tag(10)) == 10
    assert new_tag(0) == ""


def test_tag_is_deterministic_with_seeded_rng() -> None:
    assert new_tag(8, random.Random(7)) == new_tag(8, random.Random(7))


def test_sequential_tags_do_not_collide() -> None:
    rng = random.Random(1)
    tags = {new_tag(6, rng) for _ in range(50)}
    assert len(tags) == 50
