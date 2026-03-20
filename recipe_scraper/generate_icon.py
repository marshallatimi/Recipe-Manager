"""
generate_icon.py - creates icon.ico in the project root.
Run manually or automatically by CI before PyInstaller:
  python generate_icon.py

Draws a red apple silhouette (white) on the app's red background.
"""
import os, sys


def generate(dest="icon.ico"):
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        print("Pillow not installed - skipping icon generation.")
        return False

    sizes = [16, 24, 32, 48, 64, 128, 256]
    frames = []

    for sz in sizes:
        img = Image.new("RGBA", (sz, sz), (0, 0, 0, 0))
        d   = ImageDraw.Draw(img)
        s   = sz / 64  # scale relative to 64-px design grid

        # ── Rounded-square background (app red) ──────────────────────────────
        bg_rad = max(2, int(sz * 0.20))
        d.rounded_rectangle(
            [0, 0, sz - 1, sz - 1],
            radius=bg_rad,
            fill=(192, 57, 43, 255),
        )

        white = (255, 255, 255, 255)
        red   = (192, 57, 43, 255)   # background colour — used for the cleft

        # ── Apple body ────────────────────────────────────────────────────────
        # Two overlapping lobes form the characteristic double-bump top
        lx, ly, lr = 22*s, 30*s, 13*s          # left lobe centre + radius
        d.ellipse([lx-lr, ly-lr, lx+lr, ly+lr], fill=white)

        rx2, ry2, rr = 42*s, 30*s, 13*s         # right lobe centre + radius
        d.ellipse([rx2-rr, ry2-rr, rx2+rr, ry2+rr], fill=white)

        # Fill in the bottom half so the two lobes connect into a solid body
        d.ellipse([16*s, 28*s, 48*s, 56*s], fill=white)

        # Cleft: background-colour ellipse cuts into the top centre
        d.ellipse([27*s, 17*s, 37*s, 29*s], fill=red)

        # ── Stem ──────────────────────────────────────────────────────────────
        d.rounded_rectangle(
            [29*s, 10*s, 35*s, 22*s],
            radius=max(1, int(2*s)),
            fill=(130, 80, 25, 255),
        )

        # ── Leaf ──────────────────────────────────────────────────────────────
        d.ellipse([31*s, 10*s, 52*s, 18*s], fill=(80, 175, 55, 255))

        frames.append(img)

    out = os.path.abspath(dest)
    frames[0].save(
        out,
        format="ICO",
        sizes=[(sz, sz) for sz in sizes],
        append_images=frames[1:],
    )
    print(f"Icon saved -> {out}")
    return True


if __name__ == "__main__":
    dest = sys.argv[1] if len(sys.argv) > 1 else "icon.ico"
    ok = generate(dest)
    sys.exit(0 if ok else 1)
