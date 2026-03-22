"""
Macleay Recipe Manager – desktop launcher
------------------------------------------
Starts the Flask server on a free port and opens a native desktop window
via pywebview.  Works as a plain Python script and as a PyInstaller .exe.
"""

import sys
import os
import socket
import subprocess
import threading
import time
import shutil
import webview


def _find_browser() -> str | None:
    """Return path to the best available Chromium-based browser (Edge or Chrome)."""
    lappdata = os.environ.get("LOCALAPPDATA", "")
    pfiles   = os.environ.get("PROGRAMFILES", "")
    pfiles86 = os.environ.get("PROGRAMFILES(X86)", os.environ.get("PROGRAMW6432", ""))

    candidates = [
        # Edge — stable, beta, dev, canary
        os.path.join(pfiles86, r"Microsoft\Edge\Application\msedge.exe"),
        os.path.join(pfiles,   r"Microsoft\Edge\Application\msedge.exe"),
        os.path.join(lappdata, r"Microsoft\Edge\Application\msedge.exe"),
        # Chrome — stable, beta, canary
        os.path.join(pfiles,   r"Google\Chrome\Application\chrome.exe"),
        os.path.join(pfiles86, r"Google\Chrome\Application\chrome.exe"),
        os.path.join(lappdata, r"Google\Chrome\Application\chrome.exe"),
        # Chrome SxS (Canary)
        os.path.join(lappdata, r"Google\Chrome SxS\Application\chrome.exe"),
        # Brave
        os.path.join(pfiles,   r"BraveSoftware\Brave-Browser\Application\brave.exe"),
        os.path.join(pfiles86, r"BraveSoftware\Brave-Browser\Application\brave.exe"),
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    for name in ("msedge", "msedge.exe", "google-chrome", "chrome", "chrome.exe", "brave", "brave.exe"):
        found = shutil.which(name)
        if found:
            return found
    return None

# Keep old name as alias so existing call sites in _html_to_pdf still work
def _find_edge() -> str:
    return _find_browser() or "msedge"

# ── Path setup ────────────────────────────────────────────────────────────────
if getattr(sys, "frozen", False):
    BASE_DIR = sys._MEIPASS                          # type: ignore[attr-defined]
    # When installed to Program Files, write user data to Documents instead
    DATA_DIR = os.path.join(os.path.expanduser("~"), "Documents", "Macleay Recipe Manager")
    os.makedirs(DATA_DIR, exist_ok=True)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    DATA_DIR = BASE_DIR

os.chdir(BASE_DIR)   # So Flask resolves relative paths inside the bundle

# Tell app.py where to store the database & uploads
os.environ["RECIPE_DATA_DIR"] = DATA_DIR

# ── Import the Flask app (after chdir so imports resolve) ────────────────────
import app as flask_app   # noqa: E402


# ── Helpers ───────────────────────────────────────────────────────────────────

def find_free_port(preferred: int = 5000) -> int:
    for port in [preferred] + list(range(5001, 5100)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("No free port found in range 5000-5099")


def wait_for_server(port: int, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def run_server(port: int) -> None:
    flask_app.startup()
    from werkzeug.serving import make_server
    server = make_server("127.0.0.1", port, flask_app.app)
    server.serve_forever()


# ── Main ──────────────────────────────────────────────────────────────────────

class FileApi:
    """Exposed to JavaScript as window.pywebview.api"""
    def open_file_dialog(self):
        result = webview.windows[0].create_file_dialog(
            webview.OPEN_DIALOG,
            file_types=('Cookbook files (*.cookbook)', 'All files (*.*)')
        )
        return result[0] if result else None

    def save_file_dialog(self, suggested_name="My Cookbook"):
        safe = self._safe_filename(suggested_name)
        result = webview.windows[0].create_file_dialog(
            webview.SAVE_DIALOG,
            save_filename=safe + ".cookbook",
            file_types=('Cookbook files (*.cookbook)',)
        )
        return result if result else None

    def import_file_dialog(self):
        """Open a dialog to pick a .cookbook or AccuChef .csv file for import."""
        result = webview.windows[0].create_file_dialog(
            webview.OPEN_DIALOG,
            file_types=(
                'Macleay Recipe Manager Cookbook (*.cookbook)',
                'AccuChef CSV Export (*.csv)',
                'All files (*.*)',
            )
        )
        return result[0] if result else None

    @staticmethod
    def _safe_filename(name):
        """Strip characters that Windows forbids in file names."""
        import re
        safe = re.sub(r'[\\/:*?"<>|]', '-', str(name))
        safe = safe.strip('. ')  # can't end with dot/space
        return safe or "export"

    def save_pdf(self, html_content, suggested_name):
        """Show a Save dialog then render HTML → PDF via WeasyPrint (no browser needed)."""
        safe_name = self._safe_filename(suggested_name)
        result = webview.windows[0].create_file_dialog(
            webview.SAVE_DIALOG,
            save_filename=safe_name + ".pdf",
            file_types=('PDF Files (*.pdf)',)
        )
        if not result:
            return {"ok": False, "msg": "Cancelled"}
        pdf_path = result[0] if isinstance(result, (list, tuple)) else result
        if not pdf_path:
            return {"ok": False, "msg": "Cancelled"}
        if not pdf_path.lower().endswith(".pdf"):
            pdf_path += ".pdf"
        try:
            err = _html_to_pdf(html_content, pdf_path)
            if err:
                return {"ok": False, "msg": err}
            return {"ok": True, "path": pdf_path}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def close_window(self):
        webview.windows[0].destroy()

    def save_pdf_folder(self, pdfs, folder_name):
        """Ask the user for a parent folder, create a subfolder named after the
        group meal, then render each {filename, html} entry as a separate PDF."""
        safe_folder = self._safe_filename(folder_name) or "Group Export"

        result = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG)
        if not result:
            return {"ok": False, "msg": "Cancelled"}
        parent_dir = result[0] if isinstance(result, (list, tuple)) else result
        if not parent_dir:
            return {"ok": False, "msg": "Cancelled"}

        out_dir = os.path.join(parent_dir, safe_folder)
        os.makedirs(out_dir, exist_ok=True)

        saved = []
        for entry in pdfs:
            safe_name = self._safe_filename(entry.get("filename", "export"))
            pdf_path = os.path.join(out_dir, safe_name + ".pdf")
            try:
                err = _html_to_pdf(entry["html"], pdf_path)
                if err:
                    return {"ok": False, "msg": f"Failed on '{safe_name}': {err}"}
                if os.path.exists(pdf_path):
                    saved.append(safe_name)
            except Exception as e:
                return {"ok": False, "msg": f"Failed on '{safe_name}': {e}"}

        return {"ok": True, "folder": out_dir, "count": len(saved)}

    def print_preview(self, html_content):
        """Render HTML → temp PDF via WeasyPrint then open in the user's default viewer."""
        import tempfile
        tmp_pdf = None
        try:
            fd, tmp_pdf = tempfile.mkstemp(suffix='_print.pdf')
            os.close(fd)
            err = _html_to_pdf(html_content, tmp_pdf)
            if err:
                return {"ok": False, "msg": err}
            os.startfile(tmp_pdf)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "msg": str(e)}
        # Note: do NOT delete tmp_pdf — the viewer still needs it

    def save_csv_dialog(self, suggested_name="cookbook"):
        """Open a save dialog for exporting a CSV file."""
        safe = self._safe_filename(suggested_name)
        result = webview.windows[0].create_file_dialog(
            webview.SAVE_DIALOG,
            save_filename=safe + ".csv",
            file_types=('CSV Files (*.csv)',)
        )
        return result if result else None

    def exit_app(self):
        webview.windows[0].destroy()


def _generate_app_icon() -> str | None:
    """Generate a frying-pan ICO file in the temp dir. Returns path or None."""
    try:
        from PIL import Image, ImageDraw
        import tempfile

        sizes = [16, 24, 32, 48, 64, 256]
        frames = []
        for sz in sizes:
            img = Image.new("RGBA", (sz, sz), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            s = sz / 64  # scale relative to 64-px design grid

            # Rounded-square background (app red)
            bg_rad = max(2, int(sz * 0.20))
            d.rounded_rectangle([0, 0, sz - 1, sz - 1], radius=bg_rad,
                                 fill=(192, 57, 43, 255))

            white = (255, 255, 255, 255)
            red   = (192, 57, 43, 255)

            # Pan body: wide shallow oval (side/angle view)
            d.ellipse([2*s, 26*s, 38*s, 54*s], fill=white)

            # Inner cooking-surface cutout
            d.ellipse([5*s, 20*s, 35*s, 33*s], fill=red)

            # Handle: tapered polygon angling to the upper-right
            handle_pts = [
                (34*s, 29*s),
                (38*s, 35*s),
                (61*s, 11*s),
                (57*s,  6*s),
            ]
            d.polygon(handle_pts, fill=white)
            d.ellipse([55*s, 5*s, 63*s, 12*s], fill=white)  # rounded grip cap

            frames.append(img)

        tmp = tempfile.mktemp(suffix=".ico")
        frames[0].save(tmp, format="ICO",
                       sizes=[(sz, sz) for sz in sizes],
                       append_images=frames[1:])
        return tmp
    except Exception:
        return None


def _pisa_html_to_pdf(html_content: str, pdf_path: str) -> str | None:
    """Render HTML string → pdf_path using xhtml2pdf/pisa (pure Python, no system deps).
    Returns None on success, or an error string on failure.
    """
    try:
        from xhtml2pdf import pisa
        with open(pdf_path, 'wb') as f:
            result = pisa.CreatePDF(html_content.encode('utf-8'), dest=f)
        if result.err:
            # Clean up any partial file
            try:
                if os.path.exists(pdf_path): os.unlink(pdf_path)
            except OSError:
                pass
            return f"PDF generation error (pisa): {result.err}"
        if os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 0:
            return None  # success
        return "PDF was empty after generation."
    except ImportError:
        return "pisa_not_available"
    except Exception as e:
        try:
            if os.path.exists(pdf_path): os.unlink(pdf_path)
        except OSError:
            pass
        return f"PDF generation error: {e}"


def _html_to_pdf(html_content: str, pdf_path: str) -> str | None:
    """Render HTML → PDF. Tries xhtml2pdf/pisa first (pure Python, always works),
    then falls back to Edge headless if pisa is unavailable.
    Returns None on success, or an error string on failure.
    """
    err = _pisa_html_to_pdf(html_content, pdf_path)
    if err is not None:
        # pisa failed — fall back to a Chromium-based browser (Edge, Chrome, Brave…)
        browser = _find_browser()
        if not browser:
            return ("PDF generation failed. Please install Google Chrome or Microsoft Edge "
                    "to enable PDF export.")
        import tempfile
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(
                mode='w', suffix='.html', delete=False, encoding='utf-8'
            ) as f:
                f.write(html_content)
                tmp = f.name
            file_url = "file:///" + tmp.replace("\\", "/")
            return _edge_html_to_pdf(browser, file_url, pdf_path)
        finally:
            if tmp and os.path.exists(tmp):
                try: os.unlink(tmp)
                except OSError: pass
    return err  # None = success, or error string


def _edge_html_to_pdf(edge_exe: str, file_url: str, pdf_path: str,
                      timeout: int = 30) -> str | None:
    """Render file_url → pdf_path using Edge headless (fallback only).
    Tries --headless=new first, then legacy --headless.
    Returns None on success, or an error string on failure.
    """
    base = [
        "--disable-gpu", "--no-sandbox",
        "--print-to-pdf-no-header-footer",
        f"--print-to-pdf={pdf_path}",
    ]
    for headless_flag in ("--headless=new", "--headless"):
        try:
            subprocess.run(
                [edge_exe, headless_flag] + base + [file_url],
                timeout=timeout, check=False,
                capture_output=True,
                creationflags=0x08000000,  # CREATE_NO_WINDOW
            )
            if os.path.exists(pdf_path):
                return None  # success
        except subprocess.TimeoutExpired:
            return "Timed out while generating the PDF (>30 s)."
        except FileNotFoundError:
            return "Microsoft Edge was not found."
        except Exception as e:
            return str(e)
    return "PDF was not created. Please ensure Microsoft Edge is installed and up to date."


def _set_taskbar_icon(hwnd: int, ico_path: str) -> None:
    """Set the window icon in the title bar and taskbar via Win32 API."""
    try:
        import ctypes
        WM_SETICON   = 0x0080
        IMAGE_ICON   = 1
        LR_LOADFROMFILE = 0x0010
        LR_DEFAULTSIZE  = 0x0040
        hicon = ctypes.windll.user32.LoadImageW(
            None, ico_path, IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE
        )
        if hicon:
            ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, 0, hicon)  # ICON_SMALL
            ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, 1, hicon)  # ICON_BIG
    except Exception:
        pass


def main() -> None:
    port = find_free_port()

    # Generate the apple taskbar icon ahead of window creation
    icon_path = _generate_app_icon()

    # Start Flask in a daemon thread
    t = threading.Thread(target=run_server, args=(port,), daemon=True)
    t.start()

    # Wait until Flask is ready before opening the window
    if not wait_for_server(port):
        print("ERROR: Flask server did not start in time.", file=sys.stderr)
        sys.exit(1)

    window = webview.create_window(
        "Macleay Recipe Manager",
        f"http://127.0.0.1:{port}",
        width=1280,
        height=860,
        min_size=(900, 600),
        js_api=FileApi(),
    )

    def on_shown():
        """Maximize the window, bring it to the front, and apply the pan icon."""
        try:
            webview.windows[0].maximize()
        except Exception:
            pass
        # Bring the window to the foreground (important after installer launch)
        try:
            import ctypes
            hwnd = webview.windows[0].native.Handle.ToInt32()
            ctypes.windll.user32.SetForegroundWindow(hwnd)
            ctypes.windll.user32.BringWindowToTop(hwnd)
        except Exception:
            pass
        if icon_path:
            try:
                hwnd = webview.windows[0].native.Handle.ToInt32()
                _set_taskbar_icon(hwnd, icon_path)
            except Exception:
                pass

    # Wire up the shown event so we can set the taskbar icon after startup
    window.events.shown += on_shown

    # gui="edgechromium" gives the best look on Windows; falls back automatically
    try:
        webview.start(gui="edgechromium")
    except Exception:
        webview.start()


if __name__ == "__main__":
    main()
