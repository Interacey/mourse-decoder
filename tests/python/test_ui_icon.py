"""Tests for the generated app/tray icon (Pillow, no display needed)."""

from __future__ import annotations

import pytest

pytest.importorskip("PIL")

from mourse_decoder.ui.icon import ACTIVE_COLOR, PAUSED_COLOR, create_icon  # noqa: E402


@pytest.mark.parametrize("size", [16, 32, 64, 256])
def test_icon_size_and_transparency(size):
    image = create_icon(size=size)
    assert image.size == (size, size)
    assert image.mode == "RGBA"
    assert image.getpixel((0, 0))[3] == 0  # corner is transparent, the mouse is narrower than the image


def test_icon_color_reflects_state():
    # a body pixel left of the vertical split, above the Morse marks
    assert create_icon(active=True, size=64).getpixel((19, 32))[:3] == ACTIVE_COLOR
    assert create_icon(active=False, size=64).getpixel((19, 32))[:3] == PAUSED_COLOR


def test_icon_contains_morse_dot_and_dash():
    image = create_icon(size=256)
    assert image.getpixel((90, 154))[:3] == (255, 255, 255)   # dot (35%, 60%)
    assert image.getpixel((143, 154))[:3] == (255, 255, 255)  # dash (45-67%, 60%)
