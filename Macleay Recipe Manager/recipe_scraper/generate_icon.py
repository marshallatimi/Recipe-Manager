"""
generate_icon.py - creates icon.ico in the project root.
Run manually or automatically by CI before PyInstaller:
  python generate_icon.py

Draws a side-view frying-pan silhouette (white) on the app's red background,
matching the reference image: oval pan body with visible inner rim and a long
handle extending to the upper-right.
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
        red   = (192, 57, 43, 255)   # background colour, used for inner-rim cutout

        # ── Pan body ──────────────────────────────────────────────────────────
        # Wide shallow oval representing the pan body (side/angle view)
        d.ellipse([2*s, 26*s, 38*s, 54*s], fill=white)

        # Inner cooking-surface cutout: background colour oval at the top of
        # the pan body shows the depth/rim of the pan
        d.ellipse([5*s, 20*s, 35*s, 33*s], fill=red)

        # ── Handle ────────────────────────────────────────────────────────────
        # Tapered polygon from pan (wide end) to grip (narrow end),
        # angling up to the upper-right like the reference image
        handle_pts = [
            (34*s, 29*s),   # top at pan end
            (38*s, 35*s),   # bottom at pan end
            (61*s, 11*s),   # bottom at grip end
            (57*s,  6*s),   # top at grip end
        ]
        d.polygon(handle_pts, fill=white)

        # Rounded cap at grip end
        d.ellipse([55*s, 5*s, 63*s, 12*s], fill=white)

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
