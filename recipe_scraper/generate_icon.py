"""
generate_icon.py - creates icon.ico in the project root.
Run manually or automatically by CI before PyInstaller:
  python generate_icon.py

Draws a cast-iron frying pan silhouette (white) on the app's red background,
styled like a top-down view with a long diagonal handle - matching the
reference pan image.
"""
import os, sys, math


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

        # ── Pan body ──────────────────────────────────────────────────────────
        # Positioned upper-left, large circle
        cx, cy = 26 * s, 24 * s
        pr     = 18 * s

        # Outer circle (white)
        d.ellipse([cx - pr, cy - pr, cx + pr, cy + pr], fill=(255, 255, 255, 255))

        # Inner surface (very light off-white for depth, like a recessed pan surface)
        ir = pr * 0.82
        d.ellipse([cx - ir, cy - ir, cx + ir, cy + ir], fill=(235, 235, 235, 255))

        # Rim highlight arc (white thin ring just inside outer edge)
        rr = pr * 0.91
        d.ellipse(
            [cx - rr, cy - rr, cx + rr, cy + rr],
            outline=(255, 255, 255, 255),
            width=max(1, int(1.5 * s)),
        )

        # ── Handle (white, angled ~45° toward lower-right) ───────────────────
        angle  = math.radians(42)          # direction of handle
        hw     = 4.8 * s                   # half-width at pan end
        hw_tip = 2.0 * s                   # half-width at grip end (taper)

        # Handle starts just outside pan rim
        hsx = cx + pr * math.cos(angle) * 0.78
        hsy = cy + pr * math.sin(angle) * 0.78

        # Handle ends near bottom-right corner
        hex_ = 59 * s
        hey  = 58 * s

        # Perpendicular direction
        px = -math.sin(angle)
        py =  math.cos(angle)

        poly = [
            (hsx + px * hw,     hsy + py * hw),
            (hsx - px * hw,     hsy - py * hw),
            (hex_ - px * hw_tip, hey - py * hw_tip),
            (hex_ + px * hw_tip, hey + py * hw_tip),
        ]
        d.polygon(poly, fill=(255, 255, 255, 255))

        # Rounded grip at handle end
        gr = 3.8 * s
        d.ellipse(
            [hex_ - gr, hey - gr, hex_ + gr, hey + gr],
            fill=(255, 255, 255, 255),
        )
        # Hole in grip (characteristic of cast-iron pans)
        hole = gr * 0.42
        d.ellipse(
            [hex_ - hole, hey - hole, hex_ + hole, hey + hole],
            fill=(192, 57, 43, 255),
        )

        # ── Small side handle (stub on the opposite side) ─────────────────────
        # Cast-iron pans often have a small helper handle opposite the main one
        side_angle = math.radians(42 + 180)  # opposite side
        shx = cx + pr * math.cos(side_angle) * 0.78
        shy = cy + pr * math.sin(side_angle) * 0.78
        shex = cx + (pr + 5 * s) * math.cos(side_angle)
        shey = cy + (pr + 5 * s) * math.sin(side_angle)
        sh_hw = 3.0 * s
        sh_poly = [
            (shx  + px * sh_hw,  shy  + py * sh_hw),
            (shx  - px * sh_hw,  shy  - py * sh_hw),
            (shex - px * sh_hw * 0.7, shey - py * sh_hw * 0.7),
            (shex + px * sh_hw * 0.7, shey + py * sh_hw * 0.7),
        ]
        d.polygon(sh_poly, fill=(255, 255, 255, 255))

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
