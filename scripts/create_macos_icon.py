from pathlib import Path
import shutil
import subprocess

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "build" / "macos"
ICONSET_DIR = OUTPUT_DIR / "TraceMindAI.iconset"
ICNS_PATH = OUTPUT_DIR / "TraceMindAI.icns"


def load_font(size: int) -> ImageFont.FreeTypeFont:
    candidates = (
        Path("/System/Library/Fonts/SFNSRounded.ttf"),
        Path("/System/Library/Fonts/SFNS.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
    )
    for path in candidates:
        if path.is_file():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default(size=size)


def create_master_icon() -> Image.Image:
    size = 1024
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (72, 72, size - 72, size - 72),
        radius=210,
        fill=(28, 30, 34, 255),
    )
    draw.rounded_rectangle(
        (118, 118, size - 118, size - 118),
        radius=172,
        outline=(255, 255, 255, 35),
        width=4,
    )
    draw.rounded_rectangle(
        (690, 150, 858, 178),
        radius=14,
        fill=(28, 119, 255, 255),
    )

    font = load_font(390)
    label = "AI"
    bounds = draw.textbbox((0, 0), label, font=font)
    text_width = bounds[2] - bounds[0]
    text_height = bounds[3] - bounds[1]
    x = (size - text_width) / 2
    y = (size - text_height) / 2 - bounds[1] - 12
    draw.text((x, y), label, font=font, fill=(255, 255, 255, 255))
    return image


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if ICONSET_DIR.exists():
        shutil.rmtree(ICONSET_DIR)
    ICONSET_DIR.mkdir()

    master = create_master_icon()
    variants = (
        (16, "icon_16x16.png"),
        (32, "icon_16x16@2x.png"),
        (32, "icon_32x32.png"),
        (64, "icon_32x32@2x.png"),
        (128, "icon_128x128.png"),
        (256, "icon_128x128@2x.png"),
        (256, "icon_256x256.png"),
        (512, "icon_256x256@2x.png"),
        (512, "icon_512x512.png"),
        (1024, "icon_512x512@2x.png"),
    )
    for pixels, filename in variants:
        resized = master.resize((pixels, pixels), Image.Resampling.LANCZOS)
        resized.save(ICONSET_DIR / filename)

    subprocess.run(
        ["iconutil", "-c", "icns", str(ICONSET_DIR), "-o", str(ICNS_PATH)],
        check=True,
    )
    shutil.rmtree(ICONSET_DIR)
    print(ICNS_PATH)


if __name__ == "__main__":
    main()
