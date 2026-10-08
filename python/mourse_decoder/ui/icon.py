"""The app icon: a computer mouse with Morse marks (dot, dash) on its body.

Drawn instead of shipped as a PNG so it is sharp at any size (tray 16 px, splash
256 px) and can show the state by color. It is drawn 4x larger and scaled down
for smooth edges.
"""

from __future__ import annotations

from PIL import Image, ImageDraw

ACTIVE_COLOR = (43, 123, 214)   # blue = input active
PAUSED_COLOR = (130, 130, 130)  # gray = paused
_WHITE = (255, 255, 255)
_SUPERSAMPLE = 4


def create_icon(active: bool = True, size: int = 64) -> Image.Image:
    """Mouse with button split, scroll wheel and two rows of Morse marks."""
    big = size * _SUPERSAMPLE
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    u = big / 100  # grid in percent of the edge length
    color = ACTIVE_COLOR if active else PAUSED_COLOR

    # body
    draw.rounded_rectangle((22 * u, 5 * u, 78 * u, 95 * u), radius=28 * u, fill=color)
    # button split: vertical line on top, horizontal line below
    line = max(int(2.6 * u), 1)
    draw.line((50 * u, 5.5 * u, 50 * u, 38 * u), fill=_WHITE, width=line)
    draw.line((22.5 * u, 38 * u, 77.5 * u, 38 * u), fill=_WHITE, width=line)
    # scroll wheel
    draw.rounded_rectangle((46.5 * u, 15 * u, 53.5 * u, 30 * u), radius=3.5 * u, fill=_WHITE)

    # Morse marks: two rows, each a dot and a dash
    def dot(cx: float, cy: float) -> None:
        r = 4.6 * u
        draw.ellipse((cx * u - r, cy * u - r, cx * u + r, cy * u + r), fill=_WHITE)

    def dash(x1: float, x2: float, cy: float) -> None:
        h = 4.6 * u
        draw.rounded_rectangle((x1 * u, cy * u - h, x2 * u, cy * u + h), radius=h, fill=_WHITE)

    dot(35, 60)
    dash(45, 67, 60)
    dash(33, 55, 76)
    dot(66, 76)

    return image.resize((size, size), Image.LANCZOS)
