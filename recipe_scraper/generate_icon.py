"""
generate_icon.py – creates icon.ico in the project root.
Run manually during development, or automatically by CI before PyInstaller.
  python generate_icon.py
"""
import os, sys, math

def generate(dest="icon.ico"):
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        print("Pillow not installed – skipping icon generation.")
        return False

    sizes = [16, 24, 32, 48, 64, 128, 256]
    frames = []
    for sz in sizes:
        img = Image.new("RGBA", (sz, sz), (0, 0, 0, 0))
        d   = ImageDraw.Draw(img)
        s   = sz / 32  # scale relative to 32-px design grid

        # ── Pan body ──────────────────────────────────────────────────────────
        px, py, pr = 10*s, 17*s, 10*s
        # Outer rim (dark red)
        d.ellipse([px-pr, py-pr, px+pr, py+pr], fill=(152, 30, 20, 255))
        # Inner surface (brighter red)
        ir = pr * 0.80
        d.ellipse([px-ir, py-ir, px+ir, py+ir], fill=(210, 60, 45, 255))
        # Subtle inner shadow at the bottom
        d.ellipse([px-ir*0.72, py-ir*0.72+ir*0.3, px+ir*0.72, py+ir*0.6],
                  fill=(180, 45, 32, 120))
        # Shine / highlight (top-left arc)
        d.ellipse([px-pr*0.55, py-pr*0.70, px+pr*0.05, py-pr*0.08],
                  fill=(255, 200, 190, 90))

        # ── Handle ────────────────────────────────────────────────────────────
        hx1, hy1 = px + pr - 1*s, py - 2.2*s
        hx2, hy2 = hx1 + 12*s,    py + 2.2*s
        radius   = max(1, int(2 * s))
        d.rounded_rectangle([hx1, hy1, hx2, hy2],
                             radius=radius, fill=(130, 28, 18, 255))
        # Handle highlight strip
        d.rounded_rectangle([hx1 + 1*s, hy1 + 1*s, hx2 - 1*s, hy1 + 2.5*s],
                             radius=radius, fill=(170, 50, 36, 180))

        frames.append(img)

    out = os.path.abspath(dest)
    frames[0].save(
        out,
        format="ICO",
        sizes=[(sz, sz) for sz in sizes],
        append_images=frames[1:],
    )
    print(f"Icon saved → {out}")
    return True


if __name__ == "__main__":
    dest = sys.argv[1] if len(sys.argv) > 1 else "icon.ico"
    ok = generate(dest)
    sys.exit(0 if ok else 1)
