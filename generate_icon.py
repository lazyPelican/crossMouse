"""Generate mouse_share.ico for the Mouse Share application."""
from PIL import Image, ImageDraw

def create_icon(size=256):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size
    # Mouse body
    body_x0, body_y0 = int(s * 0.15), int(s * 0.1)
    body_x1, body_y1 = int(s * 0.85), int(s * 0.9)
    d.rounded_rectangle(
        [body_x0, body_y0, body_x1, body_y1],
        radius=int(s * 0.3),
        fill=(60, 60, 60, 255),
        outline=(180, 180, 180, 255),
        width=max(2, s // 40),
    )
    cx = s // 2
    # Vertical divider
    d.line(
        [(cx, body_y0 + int(s * 0.06)), (cx, int(s * 0.48))],
        fill=(180, 180, 180, 255),
        width=max(2, s // 50),
    )
    # Horizontal divider
    d.line(
        [(body_x0 + int(s * 0.06), int(s * 0.48)), (body_x1 - int(s * 0.06), int(s * 0.48))],
        fill=(180, 180, 180, 255),
        width=max(2, s // 50),
    )
    # Scroll wheel
    wr = max(4, s // 16)
    wheel_y = int(s * 0.32)
    d.ellipse(
        [cx - wr, wheel_y - wr * 2, cx + wr, wheel_y + wr * 2],
        fill=(80, 160, 240, 255),
        outline=(180, 180, 180, 255),
        width=max(1, s // 80),
    )
    return img

if __name__ == "__main__":
    img256 = create_icon(256)
    img48 = create_icon(48)
    img32 = create_icon(32)
    img16 = create_icon(16)
    img256.save(
        "mouse_share.ico",
        format="ICO",
        sizes=[(16, 16), (32, 32), (48, 48), (256, 256)],
        append_images=[img16, img32, img48],
    )
    print("Created mouse_share.ico")
