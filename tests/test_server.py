import importlib.util
import io
import json
import os
from pathlib import Path
import queue
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from client import Client, ROOT, SCRIPT

spec = importlib.util.spec_from_file_location("csharpier_lsp", SCRIPT)
lsp = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = lsp
spec.loader.exec_module(lsp)


def apply_edits(source, edits):
    for edit in reversed(edits):
        start = lsp.offset(source, edit["range"]["start"])
        end = lsp.offset(source, edit["range"]["end"])
        source = source[:start] + edit["newText"] + source[end:]
    return source


class UnitTests(unittest.TestCase):
    def test_utf16_and_crlf_edit_ranges(self):
        for before, after in [
            ("class X{string S=\"😀\";}", 'class X\n{\n    string S = "😀";\n}\n'),
            ("a\r\nb\r\n", "a\r\n  b\r\n"),
            ("a\r\nb", "a\nb"),
            ("a\nb", "a\r\nb"),
            ("", "\n"), ("x\n", ""), ("same", "same"),
            ("😀abcd", "😀abXcd"),
        ]:
            with self.subTest(before=before, after=after):
                self.assertEqual(apply_edits(before, lsp.text_edits(before, after)), after)
        self.assertEqual(lsp.text_edits("same", "same"), [])
        with self.assertRaises(ValueError):
            lsp.offset("😀", {"line": 0, "character": 1})

    def test_protocol_uses_utf8_content_length(self):
        body = json.dumps({"text": "😀å"}, ensure_ascii=False).encode()
        message = f"Content-Length: {len(body)}\r\n\r\n".encode() + body
        self.assertEqual(lsp.read_message(io.BytesIO(message)), {"text": "😀å"})
        self.assertIsNone(lsp.read_message(io.BytesIO()))
        for invalid in (b"\r\n", b"Content-Length: 999999999\r\n\r\n", b"Content-Length: 10\r\n\r\n{}"):
            with self.assertRaises(ValueError):
                lsp.read_message(io.BytesIO(invalid))

    def test_configuration_validation(self):
        self.assertEqual(lsp.Options.parse(None), lsp.Options())
        for options in ({"timeout_ms": 0}, {"cache_entries": -1}, {"command": "dotnet"}, {"command": [""]}, {"unknown": True}, {"preformat_on_change": 1}):
            with self.assertRaises(ValueError):
                lsp.Options.parse(options)

    def test_only_local_file_uris(self):
        self.assertEqual(lsp.path_from_uri((ROOT / "a b.cs").as_uri()), ROOT / "a b.cs")
        for uri in ("https://example.com/file.cs", "file://other-machine/file.cs"):
            with self.assertRaises(ValueError):
                lsp.path_from_uri(uri)

    def test_cache_is_bounded_and_config_changes_invalidate_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            formatter = lsp.Formatter(root, lsp.Options(cache_entries=2, cache_bytes=1000))
            calls = []
            class FakeWorker:
                def __init__(self, *args): self.last_used = time.monotonic()
                def format(self, path, text): calls.append(text); return text.strip() + "\n"
                def stop(self): pass
            with patch.object(lsp, "Worker", FakeWorker), patch.object(lsp, "default_command", return_value=("fake",)):
                try:
                    file = root / "Example.cs"
                    self.assertEqual(formatter.format(file, "a"), "a\n")
                    self.assertEqual(formatter.format(file, "a"), "a\n")
                    self.assertEqual(formatter.format(file, "a\n"), "a\n")
                    self.assertEqual(len(calls), 1)
                    (root / ".editorconfig").write_text("root=true\n")
                    formatter.format(file, "a")
                    self.assertEqual(len(calls), 2)
                    for text in ["b", "c", "d"]:
                        formatter.format(file, text)
                    self.assertLessEqual(len(formatter.cache), 2)
                    self.assertLessEqual(formatter.cache_size, 1000)
                finally:
                    formatter.close()

    def test_timeout_kills_worker_and_can_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = root / "fake.py"
            fake.write_text("import sys,time\nsys.stdin.buffer.read(1)\ntime.sleep(30)\n")
            worker = lsp.Worker(root, (sys.executable, str(fake)), 100)
            try:
                for _ in range(2):
                    with self.assertRaisesRegex(lsp.FormatError, "timed out"):
                        worker.format(root / "Example.cs", "class X {}")
                    self.assertIsNone(worker.process)
            finally:
                worker.stop()

    def test_content_with_pipe_delimiter_is_rejected_without_starting_worker(self):
        worker = lsp.Worker(ROOT, ("does-not-exist",), 100)
        with self.assertRaises(lsp.FormatError):
            worker.format(ROOT / "Example.cs", "\x03")
        self.assertIsNone(worker.process)

    def test_invalid_configuration_does_not_replace_running_configuration(self):
        server = lsp.Server(ROOT, io.BytesIO())
        try:
            server.handle({"method": "workspace/didChangeConfiguration", "params": {"settings": {"timeout_ms": -1}}})
            self.assertEqual(server.options, lsp.Options())
        finally:
            server.close()


    def test_stale_results_and_cancelled_requests_are_never_applied(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                output = io.BytesIO()
                server = lsp.Server(ROOT, output)
                entered, release = threading.Event(), threading.Event()
                uri = (ROOT / "backend/Example.cs").as_uri()
                server.options = lsp.Options(preformat_on_change=False)
                try:
                    server.handle({"method": "textDocument/didOpen", "params": {"textDocument": {"uri": uri, "version": 1, "text": "class X{}"}}})
                    def blocked(path, text):
                        entered.set()
                        release.wait(2)
                        return text + "\n"
                    with patch.object(server.formatter, "format", side_effect=blocked):
                        server.handle({"id": 1, "method": "textDocument/formatting", "params": {"textDocument": {"uri": uri}}})
                        self.assertTrue(entered.wait(1))
                        if cancel:
                            server.handle({"method": "$/cancelRequest", "params": {"id": 1}})
                        else:
                            server.handle({"method": "textDocument/didChange", "params": {"textDocument": {"uri": uri, "version": 2}, "contentChanges": [{"text": "class Changed{}"}]}})
                        release.set()
                        server.executor.submit(lambda: None).result(timeout=2)
                        output.seek(0)
                        message = lsp.read_message(output)
                        self.assertEqual(message["error"]["code"], -32800 if cancel else -32801)
                        self.assertNotIn("result", message)
                finally:
                    release.set()
                    server.close()

    def test_preformat_is_debounced_and_only_current_version_is_sent(self):
        server = lsp.Server(ROOT, io.BytesIO())
        uri = (ROOT / "backend/Example.cs").as_uri()
        called = threading.Event()
        values = []
        try:
            def record(path, text):
                values.append(text)
                called.set()
                return text
            with patch.object(server.formatter, "format", side_effect=record):
                server.handle({"method": "textDocument/didOpen", "params": {"textDocument": {"uri": uri, "version": 1, "text": "class First{}"}}})
                server.handle({"method": "textDocument/didChange", "params": {"textDocument": {"uri": uri, "version": 2}, "contentChanges": [{"text": "class Second{}"}]}})
                self.assertTrue(called.wait(2))
                server.executor.submit(lambda: None).result(timeout=2)
                self.assertEqual(values, ["class Second{}"])
        finally:
            server.close()

    def test_manifest_selection_and_idle_worker_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nested = root / "nested"
            nested.mkdir()
            (root / "dotnet-tools.json").write_text(json.dumps({"isRoot": True, "tools": {"csharpier": {"version": "1.4.0"}}}))
            self.assertEqual(lsp.tool_directory(nested / "X.cs", root), root)
            (nested / "dotnet-tools.json").write_text(json.dumps({"isRoot": True, "tools": {}}))
            with self.assertRaises(lsp.FormatError):
                lsp.tool_directory(nested / "X.cs", root)
            formatter = lsp.Formatter(root, lsp.Options(idle_timeout_seconds=1))
            worker = lsp.Worker(root, ("unused",), 100)
            worker.last_used = time.monotonic() - 10
            formatter.workers["example"] = ((), worker)
            with patch.object(worker, "stop") as stop:
                formatter.reap()
                stop.assert_called_once()
                self.assertEqual(formatter.workers, {})
            formatter.close()


class IntegrationTests(unittest.TestCase):
    def test_real_csharpier_formats_then_unchanged_save_returns_no_edits(self):
        path = ROOT / "backend" / "Example.cs"
        source = 'class Example{public string Text=>"😀 å";}'
        with Client(options={"preformat_on_change": False}) as client:
            client.open(path, source)
            result = client.format(path)
            self.assertNotIn("error", result, result)
            formatted = apply_edits(source, result["result"])
            self.assertIn('"😀 å"', formatted)
            self.assertIn("\n{\n", formatted)
            client.change(path, formatted)
            self.assertEqual(client.format(path)["result"], [])
            self.assertEqual(client.format(path)["result"], [])
        self.assertEqual(client.process.returncode, 0)

    def test_real_xml_project_files(self):
        with Client(options={"preformat_on_change": False}) as client:
            for suffix in ("csproj", "props", "targets", "proj", "slnx"):
                with self.subTest(suffix=suffix):
                    path = ROOT / f"Example.{suffix}"
                    source = '<Project><PropertyGroup><Nullable>enable</Nullable></PropertyGroup></Project>'
                    client.open(path, source)
                    result = client.format(path)
                    self.assertNotIn("error", result, result)
                    formatted = apply_edits(source, result["result"])
                    self.assertIn("\n", formatted)
                    self.assertIn("<Nullable>enable</Nullable>", formatted)

    def test_invalid_csharp_is_never_replaced_with_empty_text_and_worker_recovers(self):
        path = ROOT / "backend" / "Broken.cs"
        with Client(options={"preformat_on_change": False}) as client:
            client.open(path, "class Broken {")
            result = client.format(path)
            self.assertIn("error", result)
            self.assertNotIn("result", result)
            client.change(path, "class Fixed {}")
            self.assertNotIn("error", client.format(path))

    def test_unknown_document_and_external_document_fail_without_edits(self):
        with Client(options={"preformat_on_change": False}) as client:
            self.assertIn("error", client.format(ROOT / "NotOpened.cs"))
            outside = Path("/tmp/Outside.cs")
            client.open(outside, "class X{}")
            self.assertIn("error", client.format(outside))

    def test_incremental_utf16_changes_are_accepted(self):
        path = ROOT / "backend" / "Example.cs"
        with Client(options={"preformat_on_change": False}) as client:
            source = 'class X{string S="😀";}'
            client.open(path, source)
            client.notify("textDocument/didChange", {"textDocument": {"uri": path.as_uri(), "version": 2}, "contentChanges": [{"range": {"start": {"line": 0, "character": 6}, "end": {"line": 0, "character": 7}}, "text": "Renamed"}]})
            result = client.format(path)
            self.assertNotIn("error", result, result)
            changed = source.replace("class X", "class Renamed")
            self.assertIn("class Renamed", apply_edits(changed, result["result"]))

    def test_preformat_populates_cache_and_worker_is_reused(self):
        formatter = lsp.Formatter(ROOT, lsp.Options())
        try:
            path = ROOT / "backend" / "Example.cs"
            formatted = formatter.format(path, "class Example{}")
            child = next(iter(formatter.workers.values()))[1].process
            self.assertEqual(formatter.format(path, formatted), formatted)
            self.assertEqual(formatter.invocations, 1)
            formatter.format(path, "class Changed{}")
            self.assertIs(next(iter(formatter.workers.values()))[1].process, child)
        finally:
            formatter.close()
        self.assertIsNotNone(child.poll())


if __name__ == "__main__":
    unittest.main()
