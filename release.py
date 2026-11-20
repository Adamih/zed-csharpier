#!/usr/bin/env python3
"""Build a portable, prebuilt Zed extension tarball and checksums."""
import gzip
import hashlib
import io
from pathlib import Path
import re
import subprocess
import sys
import tarfile


def main():
    root = Path(__file__).resolve().parent
    subprocess.run([sys.executable, str(root / "install.py"), "--build-only"], cwd=root, check=True)
    version = re.search(r'^version = "([0-9.]+)"$', (root / "extension.toml").read_text(), re.MULTILINE).group(1)
    name = f"csharpier-{version}"
    destination = root / "dist"
    destination.mkdir(exist_ok=True)
    archive = destination / f"{name}.tar.gz"
    # Explicit allowlist: no source from another project, credentials, caches or Git history.
    files = ("extension.toml", "extension.wasm", "install.py", "README.md", "LICENSE")
    with archive.open("wb") as output, gzip.GzipFile(filename="", fileobj=output, mode="wb", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as tar:
            for filename in files:
                data = (root / filename).read_bytes()
                info = tarfile.TarInfo(f"{name}/{filename}")
                info.size = len(data)
                info.mode = 0o644
                info.mtime = 0
                tar.addfile(info, io.BytesIO(data))
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    (destination / "SHA256SUMS").write_text(f"{checksum}  {archive.name}\n")
    print(f"Built {archive.name} ({archive.stat().st_size:,} bytes)")
    print(f"SHA256 {checksum}")


if __name__ == "__main__":
    main()
