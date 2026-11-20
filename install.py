#!/usr/bin/env python3
"""Build and install a Zed dev extension using Zed's standard symlink layout."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--build-only", action="store_true")
    parser.add_argument("--prebuilt", action="store_true", help="Install bundled WASM without compiling")
    parser.add_argument("--zed-data-dir", type=Path, help="Override Zed's data directory")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    prebuilt = args.prebuilt or not (root / "Cargo.toml").exists()
    if not prebuilt:
        subprocess.run(["cargo", "build", "--locked", "--release", "--target", "wasm32-wasip2", "--manifest-path", str(root / "Cargo.toml")], check=True)
        artifact = root / "target/wasm32-wasip2/release/zed_csharpier.wasm"
        # Atomic replacement prevents Zed from trying to load a partially copied WASM.
        with tempfile.NamedTemporaryFile(dir=root, suffix=".wasm", delete=False) as temporary:
            staged = Path(temporary.name)
            temporary.write(artifact.read_bytes())
        staged.replace(root / "extension.wasm")
    elif not (root / "extension.wasm").is_file():
        raise SystemExit("No prebuilt extension.wasm found")
    if args.build_only:
        print(f"Built {root / 'extension.wasm'}")
        return
    if args.zed_data_dir:
        data = args.zed_data_dir.expanduser()
    elif sys.platform == "darwin":
        data = Path.home() / "Library/Application Support/Zed"
    elif os.name == "nt":
        data = Path(os.environ["LOCALAPPDATA"]) / "Zed"
    else:
        data = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "zed"
    installed = data / "extensions/installed"
    installed.mkdir(parents=True, exist_ok=True)
    target = installed / "csharpier"
    if target.is_symlink() and target.resolve() == root:
        print(f"Updated CSharpier dev extension: {target}")
        return
    if target.exists() or target.is_symlink():
        raise SystemExit(f"Refusing to replace {target}; uninstall that extension in Zed first")
    if prebuilt:
        with tempfile.TemporaryDirectory(dir=installed.parent, prefix=".csharpier-install-") as temporary:
            staged = Path(temporary)
            for name in ("extension.wasm", "extension.toml", "README.md", "LICENSE"):
                shutil.copy2(root / name, staged / name)
            staged.rename(target)
        print(f"Installed prebuilt CSharpier extension: {target}")
    else:
        target.symlink_to(root, target_is_directory=True)
        print(f"Installed CSharpier dev extension: {target} -> {root}")


if __name__ == "__main__":
    main()
