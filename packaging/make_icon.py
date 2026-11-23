"""iSync ikonunu (packaging/iSync.ico) üretir: mavi yuvarlatılmış kare + iki senkron oku."""

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).with_name("iSync.ico")


def draw(size: int) -> Image.Image:
    s = 4  # süper örnekleme
    W = size * s
    im = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    # degrade arka plan
    top, bot = (61, 139, 255), (31, 95, 214)
    grad = Image.new("RGBA", (W, W))
    gd = ImageDraw.Draw(grad)
    for y in range(W):
        t = y / W
        gd.line([(0, y), (W, y)], fill=tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)) + (255,))
    mask = Image.new("L", (W, W), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, W - 1, W - 1], radius=int(W * 0.22), fill=255)
    im.paste(grad, (0, 0), mask)
    # oklar
    lw = max(2, int(W * 0.085))
    m = W * 0.24
    box = [m, m, W - m, W - m]
    white = (255, 255, 255, 255)
    d.arc(box, start=200, end=330, fill=white, width=lw)
    d.arc(box, start=20, end=150, fill=white, width=lw)
    import math

    cx = cy = W / 2
    r = (W - 2 * m) / 2 - lw / 2

    def head(angle_deg: float, direction: int):
        a = math.radians(angle_deg)
        x, y = cx + r * math.cos(a), cy + r * math.sin(a)
        # teğet yönü
        tx, ty = -math.sin(a) * direction, math.cos(a) * direction
        L = W * 0.16
        nx, ny = -ty, tx
        p1 = (x + tx * L * 0.9, y + ty * L * 0.9)
        p2 = (x + nx * L * 0.6 - tx * L * 0.2, y + ny * L * 0.6 - ty * L * 0.2)
        p3 = (x - nx * L * 0.6 - tx * L * 0.2, y - ny * L * 0.6 - ty * L * 0.2)
        d.polygon([p1, p2, p3], fill=white)

    head(330, 1)
    head(150, 1)
    return im.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    sizes = [16, 24, 32, 48, 64, 128, 256]
    big = draw(256)
    big.save(OUT, sizes=[(s, s) for s in sizes])
    big.save(OUT.with_suffix(".png"))
    print("yazıldı:", OUT)
