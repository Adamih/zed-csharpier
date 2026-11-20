from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class InstallTests(unittest.TestCase):
    def test_release_installs_without_cargo_and_refuses_to_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "bundle"
            bundle.mkdir()
            shutil.copy2(Path(__file__).resolve().parents[1] / "install.py", bundle / "install.py")
            for name, content in (("extension.wasm", b"\x00asm\x01\x00\x00\x00"), ("extension.toml", b'id = "csharpier"\n'), ("README.md", b"Example"), ("LICENSE", b"MIT")):
                (bundle / name).write_bytes(content)
            profile = root / "zed"
            command = [sys.executable, str(bundle / "install.py"), "--zed-data-dir", str(profile)]
            result = subprocess.run(command, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            installed = profile / "extensions/installed/csharpier"
            self.assertFalse(installed.is_symlink())
            self.assertEqual((installed / "extension.wasm").read_bytes(), (bundle / "extension.wasm").read_bytes())
            repeated = subprocess.run(command, text=True, capture_output=True)
            self.assertNotEqual(repeated.returncode, 0)
            self.assertIn("Refusing to replace", repeated.stderr)
