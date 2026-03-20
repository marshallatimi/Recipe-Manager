# 🍴 Macleay Recipe Manager

A polished desktop recipe manager for Windows. Scrape recipes from any cooking website, build shopping lists, plan meals, and export to PDF — all in a native desktop app with no browser needed.

[![Latest Release](https://img.shields.io/github/v/release/marshallatimi/Recipe-Manager?label=download&logo=github)](https://github.com/marshallatimi/Recipe-Manager/releases/latest)
[![Build](https://github.com/marshallatimi/Recipe-Manager/actions/workflows/build.yml/badge.svg)](https://github.com/marshallatimi/Recipe-Manager/actions/workflows/build.yml)
![Platform](https://img.shields.io/badge/platform-Windows%2010%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

---

## ⬇️ Download

Go to the **[Releases](../../releases/latest)** page and grab one of:

| File | Description |
|------|-------------|
| **MacleayRecipeManager-Setup.exe** | ✅ Recommended — installer with Start Menu & desktop shortcut |
| **RecipeManager.exe** | Portable — single file, run from anywhere, no install needed |

> **Requires Windows 10 or later.** Microsoft Edge (WebView2) is built into Windows 10+ — no extra installs needed.

---

## ✨ Features

### Recipe Management
- **Scrape any recipe URL** — paste a link from AllRecipes, NYT Cooking, BBC Food, and 300+ other sites
- **Create recipes from scratch** — full manual editor for your own creations
- **My Recipes library** — save, search, sort, and filter your entire collection
- **Categories & tags** — organize by cuisine, meal type, or anything you like
- **Photo support** — attach a photo to any recipe

### Scaling & Shopping
- **Smart servings scaler** — adjusts every ingredient automatically; handles fractions, mixed units, metric & imperial
- **Make Default** — bake a scaled size into the recipe permanently
- **Shopping List builder** — combine ingredients across multiple recipes with smart unit merging (e.g. 1 cup + ½ cup = 1½ cups)
- **Print / Export PDF** — clean printable shopping lists and recipe cards

### Meal Planning
- **Meals** — group recipes into a named meal; set per-recipe serving overrides
- **Group Meals** — multi-meal event planner (dinner parties, weekly meal prep)
- **Export Group Meal PDF** — one folder with a shopping list PDF and one PDF per meal

### App
- **Cookbooks** — multiple cookbook files, switchable any time; import/export `.cookbook` files
- **AccuChef CSV import** — migrate your existing AccuChef recipe collection
- **Dark mode** — full theme support, persisted across sessions
- **Custom title bar** — frameless window with minimize / maximize / close

---

## 🚀 Quick Start (installer)

1. Download **MacleayRecipeManager-Setup.exe** from [Releases](../../releases/latest)
2. Run the installer and follow the wizard (~30 seconds)
3. Launch from your desktop or Start menu

Your data is saved in `Documents\Macleay Recipe Manager\` — it survives updates and uninstalls.

---

## 🗂️ Portable use

1. Download **RecipeManager.exe**
2. Put it anywhere (USB drive, Desktop, etc.) and double-click
3. Recipes are saved to `Documents\Macleay Recipe Manager\` on your PC

---

## 🛠️ For Developers

### Run from source (Python 3.10+)

```bash
git clone https://github.com/marshallatimi/Recipe-Manager.git
cd Recipe-Manager
pip install -r requirements.txt
python launcher.py
```

### Build the exe yourself

```bash
pip install -r requirements.txt pillow pyinstaller
python generate_icon.py          # generates icon.ico
python create_version_info.py 1.0.0
pyinstaller recipe_manager.spec
# → dist/RecipeManager.exe
```

### Build the installer

```bash
# (Requires Inno Setup 6 — https://jrsoftware.org/isinfo.php)
iscc /DAppVersion=1.0.0 installer.iss
# → Output/MacleayRecipeManager-Setup.exe
```

### Release a new version

```bash
git tag v1.2.0
git push origin v1.2.0
```

GitHub Actions automatically:
1. Builds `RecipeManager.exe` with PyInstaller
2. Packages it into `MacleayRecipeManager-Setup.exe` with Inno Setup
3. Creates a GitHub Release with both files attached

---

## 🏗️ Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.11, Flask, SQLite |
| Recipe scraping | [recipe-scrapers](https://github.com/hhursev/recipe-scrapers) |
| Desktop window | [pywebview](https://pywebview.flowrl.com/) + EdgeChromium (WebView2) |
| Packaging | PyInstaller (single-file exe) |
| Installer | Inno Setup 6 |
| CI/CD | GitHub Actions |

---

## 📁 Data & Privacy

All your data lives locally on your machine — no cloud, no accounts, no telemetry.

| Path | Contents |
|------|----------|
| `Documents\Macleay Recipe Manager\cookbooks\` | Your cookbook `.cookbook` files |
| `Documents\Macleay Recipe Manager\static\uploads\` | Recipe photos |
| `Documents\Macleay Recipe Manager\settings.json` | App settings (theme, font size, etc.) |
