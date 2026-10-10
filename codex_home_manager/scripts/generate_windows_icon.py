from __future__ import annotations

from pathlib import Path
import base64
from io import BytesIO
import struct

from PIL import Image


icon_sizes = [16, 20, 24, 30, 32, 36, 40, 48, 60, 64, 72, 80, 96, 128, 256]


def render_icon(size: int, master_path: Path | None = None) -> Image.Image:
    master_path = master_path or Path(__file__).resolve().parents[1] / "packaging/windows/assets/cockpit-master.png"
    with Image.open(master_path) as master:
        if master.width != master.height or master.width < 1024:
            raise ValueError("A square production master of at least 1024 pixels is required")
        # The production master contains its own safe margins and transparent corners.
        artwork = master.convert("RGBA")
        if artwork.getchannel("A").getextrema()[0] != 0:
            raise ValueError("The production master must have transparent outer corners")
        return artwork.resize((size, size), Image.Resampling.LANCZOS)


def save_icon(frames: list[Image.Image], path: Path) -> None:
    encoded_frames: list[bytes] = []
    for frame in frames:
        buffer = BytesIO()
        frame.save(buffer, format="PNG", optimize=True)
        encoded_frames.append(buffer.getvalue())

    header_size = 6 + 16 * len(frames)
    offset = header_size
    directory = bytearray(struct.pack("<HHH", 0, 1, len(frames)))
    for frame, encoded in zip(frames, encoded_frames, strict=True):
        width = 0 if frame.width == 256 else frame.width
        height = 0 if frame.height == 256 else frame.height
        directory.extend(struct.pack("<BBBBHHII", width, height, 0, 0, 1, 32, len(encoded), offset))
        offset += len(encoded)
    path.write_bytes(bytes(directory) + b"".join(encoded_frames))


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    icon_path = root / "packaging" / "windows" / "assets" / "codex-home-manager.ico"
    tauri_icon_path = root / "src-tauri" / "icons" / "icon.ico"
    icon_path.parent.mkdir(parents=True, exist_ok=True)
    tauri_icon_path.parent.mkdir(parents=True, exist_ok=True)
    frames = [render_icon(size) for size in icon_sizes]
    for target in (icon_path, tauri_icon_path):
        save_icon(frames, target)
        print(target)
    public_path = root / "public" / "cockpit-icon.png"
    public_path.parent.mkdir(parents=True, exist_ok=True)
    web_icon = render_icon(256)
    web_icon.save(public_path, optimize=True)
    encoded_icon = base64.b64encode(public_path.read_bytes()).decode("ascii")
    (root / "public" / "favicon.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256" role="img" aria-label="Codex Cockpit">'
        f'<image width="256" height="256" href="data:image/png;base64,{encoded_icon}"/></svg>\n',
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
