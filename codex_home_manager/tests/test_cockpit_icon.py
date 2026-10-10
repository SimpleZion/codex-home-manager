from pathlib import Path
import base64
import importlib.util
import json
import xml.etree.ElementTree as xml_tree

from PIL import Image


root = Path(__file__).resolve().parents[1]


def connected_components(mask, width, height):
    remaining = set(mask)
    sizes = []
    while remaining:
        stack = [remaining.pop()]
        size = 0
        while stack:
            x, y = stack.pop()
            size += 1
            for neighbor in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if 0 <= neighbor[0] < width and 0 <= neighbor[1] < height and neighbor in remaining:
                    remaining.remove(neighbor)
                    stack.append(neighbor)
        sizes.append(size)
    return sorted(sizes, reverse=True)


def test_cockpit_icon_surfaces_use_one_master():
    config = json.loads((root / "src-tauri/tauri.conf.json").read_text(encoding="utf-8"))
    assert config["bundle"]["windows"]["nsis"]["installerIcon"] == "icons/icon.ico"
    assert config["bundle"]["windows"]["nsis"]["uninstallerIcon"] == "icons/icon.ico"
    spec = importlib.util.spec_from_file_location("cockpit_icon", root / "scripts/generate_windows_icon.py")
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    installer = root / "packaging/windows/assets/codex-home-manager.ico"
    assert installer.read_bytes() == (root / "src-tauri/icons/icon.ico").read_bytes()
    with Image.open(installer) as icon:
        assert icon.ico.sizes() == {(size, size) for size in generator.icon_sizes}
        for size in generator.icon_sizes:
            frame = icon.ico.getimage((size, size)).convert("RGBA")
            assert frame.tobytes() == generator.render_icon(size).tobytes()
            assert frame.getchannel("A").getextrema()[0] < 255
    with Image.open(root / "public/cockpit-icon.png") as web:
        assert web.convert("RGBA").tobytes() == generator.render_icon(256).tobytes()
    favicon = xml_tree.fromstring((root / "public/favicon.svg").read_text(encoding="utf-8"))
    embedded = favicon.find("{http://www.w3.org/2000/svg}image").attrib["href"]
    assert base64.b64decode(embedded.removeprefix("data:image/png;base64,")) == (root / "public/cockpit-icon.png").read_bytes()


def test_small_icon_has_one_cabin_frame_and_one_navigation_mark():
    spec = importlib.util.spec_from_file_location("cockpit_icon", root / "scripts/generate_windows_icon.py")
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    for size in (16, 24, 32):
        frame = generator.render_icon(size).convert("RGBA")
        white = set()
        green = set()
        for y in range(size):
            for x in range(size):
                red, channel_green, blue, alpha = frame.getpixel((x, y))
                if alpha < 80:
                    continue
                # The tile's outer rim is a material highlight, not part of the cabin mark.
                in_mark_area = size * 0.1 <= x < size * 0.9 and size * 0.1 <= y < size * 0.9
                if in_mark_area and red > channel_green * 0.93 and min(red, channel_green, blue) > 150 and max(red, channel_green, blue) - min(red, channel_green, blue) < 65:
                    white.add((x, y))
                if channel_green > 75 and channel_green > red * 1.25 and channel_green > blue * 1.15:
                    green.add((x, y))
        assert connected_components(white, size, size)[:2] == [len(white)]
        assert connected_components(green, size, size)[:2] == [len(green)]
        assert len(white) >= round(size * size * 0.08)
        assert len(green) >= round(size * size * 0.06)


def test_master_is_resized_without_cropping_or_replacing_alpha():
    spec = importlib.util.spec_from_file_location("cockpit_icon", root / "scripts/generate_windows_icon.py")
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    with Image.open(root / "packaging/windows/assets/cockpit-master.png") as master:
        expected = master.convert("RGBA").resize((32, 32), Image.Resampling.LANCZOS)
    assert generator.render_icon(32).tobytes() == expected.tobytes()
