"""Build the web fonts in app/static/fonts from the SF fonts installed on a Mac.

Run on a Mac with SF Pro installed (developer.apple.com/fonts):
    uv run --with fonttools --with brotli python scripts/build-fonts.py

Makes Latin-only WOFF2 files for the weights the pages use: SF Pro Text below
20px, SF Pro Display from 20px up (Apple's split), and SF Mono for identifiers.
"""
import io
import sys
from pathlib import Path

from fontTools import subset
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

OUT = Path(__file__).resolve().parent.parent / "app" / "static" / "fonts"
LIB, SYS = Path("/Library/Fonts"), Path("/System/Library/Fonts")

# Basic Latin, Latin-1, common punctuation (’ “ ” · … – — ‹ ›), €, ™, arrows, minus, check marks.
UNICODES = ("U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,"
            "U+2000-206F,U+20AC,U+2122,U+2190-2199,U+2212,U+2713-2714")

FONTS = {
    "sf-pro-text-regular.woff2": LIB / "SF-Pro-Text-Regular.otf",
    "sf-pro-text-medium.woff2": LIB / "SF-Pro-Text-Medium.otf",
    "sf-pro-text-semibold.woff2": LIB / "SF-Pro-Text-Semibold.otf",
    "sf-pro-text-bold.woff2": LIB / "SF-Pro-Text-Bold.otf",
    "sf-pro-display-semibold.woff2": LIB / "SF-Pro-Display-Semibold.otf",
    "sf-pro-display-bold.woff2": LIB / "SF-Pro-Display-Bold.otf",
    "sf-mono-regular.woff2": SYS / "SFNSMono.ttf",   # variable; pinned to 400 below
}


def build(src: Path, dest: Path) -> int:
    font = TTFont(src)
    if "fvar" in font:  # pin every axis (weight 400) so the file is a plain regular
        font = instancer.instantiateVariableFont(font, {a.axisTag: 400 for a in font["fvar"].axes})
        buf = io.BytesIO(); font.save(buf); buf.seek(0); font = TTFont(buf)
    opts = subset.Options()
    opts.flavor = "woff2"
    opts.layout_features = ["*"]          # keep kerning, tabular figures (tnum) and the rest
    opts.name_IDs = ["*"]
    opts.notdef_outline = True
    sub = subset.Subsetter(opts)
    sub.populate(unicodes=subset.parse_unicodes(UNICODES))
    sub.subset(font)
    font.flavor = "woff2"
    font.save(dest)
    return dest.stat().st_size


def main():
    missing = [str(p) for p in FONTS.values() if not p.exists()]
    if missing:
        sys.exit("Missing fonts (install SF Pro from developer.apple.com/fonts):\n  " + "\n  ".join(missing))
    OUT.mkdir(parents=True, exist_ok=True)
    for name, src in FONTS.items():
        print(f"{name:32} {build(src, OUT / name) / 1024:6.1f} KB  from {src.name}")


if __name__ == "__main__":
    main()
