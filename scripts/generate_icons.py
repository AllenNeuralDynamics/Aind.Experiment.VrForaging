# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pillow",
#     "resvg-py",
# ]
# ///
"""Generate icon.ico, icon_dev.svg and icon_dev.ico from a base icon.svg.

Usage:
    uv run scripts/generate_icons.py [path/to/icon.svg]

Outputs are written next to the input svg.
"""

import argparse
import io
from pathlib import Path

import resvg_py
from PIL import Image

ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]

DEV_TAG = """
  <g id="dev-tag" aria-label="DEV" transform="translate(-253 -240.5) scale(1.5)">
    <path d="M364 420h120c10 0 18 8 18 18v38c0 10-8 18-18 18H322z" fill="#F23855"/>
    <text x="414" y="469" text-anchor="middle" fill="#FFFFFF" font-family="Arial, Helvetica, sans-serif" font-size="35" font-weight="700" letter-spacing="1">DEV</text>
  </g>
"""


def make_dev_svg(svg: str) -> str:
    idx = svg.rstrip().rfind("</svg>")
    if idx == -1:
        raise ValueError("Input is not a valid svg: missing closing </svg> tag")
    return svg[:idx] + DEV_TAG + svg[idx:]


def render_png(svg: str, size: int) -> Image.Image:
    png = resvg_py.svg_to_bytes(svg_string=svg, width=size, height=size)
    return Image.open(io.BytesIO(bytes(png))).convert("RGBA")


def write_ico(svg: str, path: Path) -> None:
    # Render each size separately so small icons are rasterized crisply rather than downscaled.
    frames = [render_png(svg, s) for s in ICO_SIZES]
    largest = frames[-1]
    largest.save(path, format="ICO", sizes=[(s, s) for s in ICO_SIZES], append_images=frames[:-1])
    print(f"Wrote {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "svg",
        type=Path,
        nargs="?",
        default=Path(__file__).resolve().parents[1] / "assets" / "icon.svg",
        help="Base svg icon (default: assets/icon.svg)",
    )
    args = parser.parse_args()

    src: Path = args.svg
    out_dir = src.parent
    svg = src.read_text(encoding="utf-8")
    dev_svg = make_dev_svg(svg)

    dev_svg_path = out_dir / f"{src.stem}_dev.svg"
    dev_svg_path.write_text(dev_svg, encoding="utf-8")
    print(f"Wrote {dev_svg_path}")

    write_ico(svg, out_dir / f"{src.stem}.ico")
    write_ico(dev_svg, out_dir / f"{src.stem}_dev.ico")


if __name__ == "__main__":
    main()
