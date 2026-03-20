"""
create_version_info.py – writes a PyInstaller-compatible Windows version
resource file to file_version_info.txt.

Usage:
  python create_version_info.py 1.2.3
"""
import sys

def write(version_str: str, dest: str = "file_version_info.txt"):
    parts = (version_str.lstrip("v") + ".0.0.0").split(".")[:4]
    parts = [int(p) for p in parts]
    v_tuple   = f"({parts[0]}, {parts[1]}, {parts[2]}, {parts[3]})"
    v_string  = f"{parts[0]}.{parts[1]}.{parts[2]}.{parts[3]}"
    short_ver = f"{parts[0]}.{parts[1]}.{parts[2]}"

    content = f"""\
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={v_tuple},
    prodvers={v_tuple},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        u'040904B0',
        [StringStruct(u'CompanyName',      u'Macleay Recipe Manager'),
         StringStruct(u'FileDescription',  u'Macleay Recipe Manager'),
         StringStruct(u'FileVersion',      u'{v_string}'),
         StringStruct(u'InternalName',     u'RecipeManager'),
         StringStruct(u'LegalCopyright',   u'\\u00a9 2025 Macleay Recipe Manager'),
         StringStruct(u'OriginalFilename', u'RecipeManager.exe'),
         StringStruct(u'ProductName',      u'Macleay Recipe Manager'),
         StringStruct(u'ProductVersion',   u'{short_ver}')])
    ]),
    VarFileInfo([VarStruct(u'Translation', [0x0409, 1200])])
  ]
)
"""
    with open(dest, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"Version info → {dest}  (version {short_ver})")


if __name__ == "__main__":
    version = sys.argv[1] if len(sys.argv) > 1 else "1.0.0"
    write(version)
