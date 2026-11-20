"""A real stdio LSP client for regression tests and round-trip benchmarks."""
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parent / "fixtures/project"
SCRIPT = Path(__file__).resolve().parents[1] / "server" / "csharpier_lsp.py"


class Client:
    def __init__(self, root=ROOT, options=None):
        self.process = subprocess.Popen([sys.executable, "-u", str(SCRIPT), "--root", str(root)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.responses = queue.Queue()
        self.stderr = []
        self.sequence = 0
        self.versions = {}
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._errors, daemon=True).start()
        self.request("initialize", {"rootUri": root.as_uri(), "capabilities": {}, "initializationOptions": options or {}})
        self.notify("initialized", {})

    def _read(self):
        try:
            while True:
                headers = {}
                while line := self.process.stdout.readline():
                    if line == b"\r\n":
                        break
                    key, _, value = line.partition(b":")
                    headers[key.lower()] = value.strip()
                if not headers:
                    return
                body = self.process.stdout.read(int(headers[b"content-length"]))
                self.responses.put(json.loads(body))
        finally:
            self.process.stdout.close()

    def _errors(self):
        for line in self.process.stderr:
            self.stderr.append(line.decode(errors="replace"))
        self.process.stderr.close()

    def send(self, value):
        body = json.dumps(value, ensure_ascii=False).encode()
        self.process.stdin.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        self.process.stdin.flush()

    def notify(self, method, params):
        self.send({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method, params, timeout=10):
        self.sequence += 1
        request_id = self.sequence
        self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        response = self.responses.get(timeout=timeout)
        assert response["id"] == request_id, response
        return response

    def open(self, path, text):
        self.versions[path] = 1
        self.notify("textDocument/didOpen", {"textDocument": {"uri": path.as_uri(), "version": 1, "languageId": "csharp", "text": text}})

    def change(self, path, text):
        self.versions[path] += 1
        self.notify("textDocument/didChange", {"textDocument": {"uri": path.as_uri(), "version": self.versions[path]}, "contentChanges": [{"text": text}]})

    def format(self, path):
        return self.request("textDocument/formatting", {"textDocument": {"uri": path.as_uri()}, "options": {"tabSize": 4, "insertSpaces": True}})

    def close(self):
        if self.process.poll() is None:
            try:
                self.request("shutdown", {})
                self.notify("exit", {})
                self.process.stdin.close()
                self.process.wait(timeout=10)
            except (BrokenPipeError, queue.Empty, subprocess.TimeoutExpired):
                self.process.kill()
                self.process.wait()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
