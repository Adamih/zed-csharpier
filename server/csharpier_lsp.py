#!/usr/bin/env python3
"""A formatting-only LSP bridge to the project's persistent CSharpier pipe worker.

No network sockets, runtime downloads, third-party Python packages or edits to disk.
stdout is exclusively LSP; CSharpier stderr is captured and errors preserve buffers.
"""
from __future__ import annotations

import argparse
import collections
import concurrent.futures
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import unquote, urlparse

MAX_MESSAGE = 32 * 1024 * 1024
CONFIG_FILES = (
    ".csharpierrc", ".csharpierrc.json", ".csharpierrc.yaml", ".editorconfig",
    ".csharpierignore", ".gitignore", "dotnet-tools.json", ".config/dotnet-tools.json",
    "global.json", "mise.toml", ".mise.toml",
)


class FormatError(Exception):
    pass


@dataclasses.dataclass(frozen=True)
class Options:
    command: tuple[str, ...] = ()
    timeout_ms: int = 5000
    idle_timeout_seconds: int = 300
    cache_entries: int = 64
    cache_bytes: int = 8 * 1024 * 1024
    preformat_on_change: bool = True
    debounce_ms: int = 75

    @classmethod
    def parse(cls, value):
        if value is None:
            return cls()
        if not isinstance(value, dict):
            raise ValueError("initialization_options must be an object")
        unknown = set(value) - {field.name for field in dataclasses.fields(cls)}
        if unknown:
            raise ValueError(f"Unknown CSharpier options: {', '.join(sorted(unknown))}")
        result = dict(value)
        if "command" in result:
            if not isinstance(result["command"], list) or not all(isinstance(arg, str) and arg for arg in result["command"]):
                raise ValueError("command must be a list of nonempty strings")
            result["command"] = tuple(result["command"])
        for key in ("timeout_ms", "idle_timeout_seconds", "cache_entries", "cache_bytes", "debounce_ms"):
            if key in result and (type(result[key]) is not int or result[key] < 0):
                raise ValueError(f"{key} must be a nonnegative integer")
        if result.get("timeout_ms", cls.timeout_ms) < 100:
            raise ValueError("timeout_ms must be at least 100")
        if "preformat_on_change" in result and type(result["preformat_on_change"]) is not bool:
            raise ValueError("preformat_on_change must be a boolean")
        return cls(**result)


def path_from_uri(uri):
    parsed = urlparse(uri)
    if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):
        raise ValueError("Only local file:// documents are supported")
    path = unquote(parsed.path)
    if os.name == "nt" and len(path) > 2 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    return Path(path).resolve()


def ancestors(directory):
    return (directory, *directory.parents)


def fingerprint(path):
    """Metadata only: cheap, and includes nested/inherited config and tool manifests."""
    result = []
    for directory in ancestors(path.parent):
        for name in CONFIG_FILES:
            candidate = directory / name
            try:
                stat = candidate.stat()
                result.append((str(candidate), stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size))
            except FileNotFoundError:
                pass
    return tuple(result)


def tool_directory(path, root):
    for directory in ancestors(path.parent):
        for name in ("dotnet-tools.json", ".config/dotnet-tools.json"):
            manifest = directory / name
            if manifest.is_file():
                try:
                    data = json.loads(manifest.read_text(encoding="utf-8"))
                except (ValueError, OSError) as error:
                    raise FormatError(f"Cannot read {manifest}: {error}") from error
                if "csharpier" in data.get("tools", {}):
                    return directory
                if data.get("isRoot"):
                    raise FormatError(f"CSharpier is not in {manifest}; run dotnet tool install csharpier")
        if directory == root:
            break
    return root


def default_command():
    mise = shutil.which("mise")
    if mise:
        return (mise, "exec", "--", "dotnet", "csharpier", "pipe-files")
    dotnet = shutil.which("dotnet")
    if not dotnet:
        raise FormatError("Cannot find mise or dotnet on PATH")
    return (dotnet, "csharpier", "pipe-files")


class Worker:
    def __init__(self, directory, command, timeout_ms):
        self.directory = directory
        self.command = command
        self.timeout = timeout_ms / 1000
        self.process = None
        self.responses = queue.Queue()
        self.errors = collections.deque(maxlen=16)
        self.last_used = time.monotonic()
        self.lock = threading.RLock()

    def start(self):
        if self.process is not None and self.process.poll() is None:
            return
        self.stop()
        self.responses = queue.Queue()
        self.errors.clear()
        try:
            child = subprocess.Popen(
                self.command, cwd=self.directory, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=os.name != "nt",
            )
        except OSError as error:
            raise FormatError(f"Cannot start CSharpier: {error}") from error
        self.process = child
        responses = self.responses

        def read_stdout():
            pending = bytearray()
            try:
                while chunk := child.stdout.read1(65536):
                    pending.extend(chunk)
                    while 3 in pending:
                        marker = pending.index(3)
                        responses.put(bytes(pending[:marker]))
                        del pending[:marker + 1]
                    if len(pending) > MAX_MESSAGE:
                        raise FormatError("CSharpier response exceeded 32 MiB")
            except Exception as error:
                responses.put(error)
            finally:
                responses.put(FormatError("CSharpier stopped; check dotnet tool restore"))
                child.stdout.close()

        def read_stderr():
            try:
                for line in child.stderr:
                    self.errors.append(line.decode("utf-8", errors="replace").strip())
            finally:
                child.stderr.close()

        threading.Thread(target=read_stdout, daemon=True).start()
        threading.Thread(target=read_stderr, daemon=True).start()

    def format(self, path, text):
        if "\x03" in str(path) or "\x03" in text:
            raise FormatError("CSharpier's pipe protocol cannot encode U+0003")
        with self.lock:
            self.start()
            self.last_used = time.monotonic()
            try:
                self.process.stdin.write(str(path).encode() + b"\x03" + text.encode() + b"\x03")
                self.process.stdin.flush()
                response = self.responses.get(timeout=self.timeout)
                if isinstance(response, Exception):
                    raise response
                if not response and text.strip():
                    raise FormatError("CSharpier returned no text: the file may be ignored or contain syntax errors")
                return response.decode("utf-8-sig")
            except (queue.Empty, OSError, UnicodeError, FormatError) as error:
                details = "\n".join(self.errors)[-2000:]
                self.stop()
                if isinstance(error, queue.Empty):
                    raise FormatError(f"CSharpier timed out after {self.timeout:g}s; worker will restart") from error
                raise FormatError(f"{error}" + (f"\n{details}" if details else "")) from error

    def stop(self):
        with self.lock:
            child, self.process = self.process, None
            if child is None:
                return
            if child.poll() is None:
                try:
                    if os.name == "nt":
                        child.kill()
                    else:
                        os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            try:
                child.wait(timeout=1)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
            if child.stdin:
                child.stdin.close()


class Formatter:
    def __init__(self, root, options):
        self.root = root
        self.options = options
        self.workers = {}
        self.cache = collections.OrderedDict()
        self.cache_size = 0
        self.lock = threading.RLock()
        self.invocations = 0

    def _put(self, key, text):
        if not self.options.cache_entries or not self.options.cache_bytes:
            return
        cost = len(text.encode("utf-8")) + 128
        if cost > self.options.cache_bytes:
            return
        previous = self.cache.pop(key, None)
        if previous:
            self.cache_size -= previous[1]
        self.cache[key] = (text, cost)
        self.cache_size += cost
        while len(self.cache) > self.options.cache_entries or self.cache_size > self.options.cache_bytes:
            _, (_, removed) = self.cache.popitem(last=False)
            self.cache_size -= removed

    def format(self, path, text):
        if not path.is_relative_to(self.root):
            raise FormatError("Document is outside this language server's worktree")
        if len(text.encode("utf-8")) > MAX_MESSAGE:
            raise FormatError("Document exceeds 32 MiB")
        stamp = fingerprint(path)
        key = (str(path), hashlib.sha256(text.encode()).digest(), stamp)
        with self.lock:
            if key in self.cache:
                result, _ = self.cache[key]
                self.cache.move_to_end(key)
                return result
            directory = tool_directory(path, self.root)
            command = self.options.command or default_command()
            worker_key = (directory, command)
            current = self.workers.get(worker_key)
            # Any relevant config/tool change restarts the worker and invalidates results.
            if current is not None and current[0] != stamp:
                current[1].stop()
                del self.workers[worker_key]
                current = None
            if current is None:
                worker = Worker(directory, command, self.options.timeout_ms)
                self.workers[worker_key] = (stamp, worker)
            else:
                worker = current[1]
            result = worker.format(path, text)
            self.invocations += 1
            self._put(key, result)
            self._put((str(path), hashlib.sha256(result.encode()).digest(), stamp), result)
            return result

    def reap(self):
        if not self.options.idle_timeout_seconds:
            return
        with self.lock:
            for key, (_, worker) in list(self.workers.items()):
                if time.monotonic() - worker.last_used > self.options.idle_timeout_seconds:
                    worker.stop()
                    del self.workers[key]

    def close(self):
        with self.lock:
            for _, worker in self.workers.values():
                worker.stop()
            self.workers.clear()
            self.cache.clear()
            self.cache_size = 0


def offset(text, position):
    line, character = position["line"], position["character"]
    if line < 0 or character < 0:
        raise ValueError("Negative document position")
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]] + [parts[-1]]
    if line == len(lines) and character == 0:
        return len(text)
    if line >= len(lines):
        raise ValueError("Line outside document")
    prefix = sum(map(len, lines[:line]))
    units = 0
    for index, char in enumerate(lines[line]):
        if units == character:
            return prefix + index
        units += 2 if ord(char) > 0xFFFF else 1
        if units > character:
            raise ValueError("Position splits a UTF-16 surrogate pair")
    if units == character:
        return prefix + len(lines[line])
    raise ValueError("Character outside line")


def position(text, index):
    prefix = text[:index]
    line = prefix.count("\n")
    tail = prefix.rsplit("\n", 1)[-1]
    return {"line": line, "character": len(tail.encode("utf-16-le")) // 2}


def text_edits(original, formatted):
    if original == formatted:
        return []
    start = 0
    limit = min(len(original), len(formatted))
    while start < limit and original[start] == formatted[start]:
        start += 1
    old_end, new_end = len(original), len(formatted)
    while old_end > start and new_end > start and original[old_end - 1] == formatted[new_end - 1]:
        old_end -= 1
        new_end -= 1
    # Do not split a CRLF pair in an LSP edit range.
    if start and original[start - 1:start + 1] == "\r\n":
        start -= 1
    if original[old_end - 1:old_end + 1] == "\r\n":
        old_end += 1
        new_end += 1
    return [{"range": {"start": position(original, start), "end": position(original, old_end)}, "newText": formatted[start:new_end]}]


def read_message(stream):
    length = None
    header_size = 0
    while True:
        line = stream.readline(8193)
        if not line:
            return None
        header_size += len(line)
        if header_size > 8192:
            raise ValueError("LSP header too large")
        if line in (b"\r\n", b"\n"):
            break
        name, _, value = line.partition(b":")
        if name.lower() == b"content-length":
            length = int(value.strip())
    if length is None or not 0 <= length <= MAX_MESSAGE:
        raise ValueError("Missing or invalid Content-Length")
    body = stream.read(length)
    if len(body) != length:
        raise ValueError("Truncated LSP message")
    return json.loads(body)


class Server:
    def __init__(self, root, output=None):
        self.root = root.resolve()
        self.output = output or sys.stdout.buffer
        self.output_lock = threading.Lock()
        self.documents = {}
        self.documents_lock = threading.RLock()
        self.options = Options()
        self.formatter = Formatter(self.root, self.options)
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.pending = {}
        self.timers = {}
        self.shutdown = False
        self.closed = threading.Event()
        self.maintenance = threading.Thread(target=self._maintain, daemon=True)
        self.maintenance.start()

    def _maintain(self):
        while not self.closed.wait(5):
            self.formatter.reap()

    def send(self, message):
        body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode()
        with self.output_lock:
            self.output.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
            self.output.flush()

    def respond(self, request_id, result=None, error=None):
        message = {"jsonrpc": "2.0", "id": request_id}
        message["error" if error is not None else "result"] = error if error is not None else result
        self.send(message)

    def _preformat(self, uri, version, text):
        try:
            with self.documents_lock:
                if self.documents.get(uri, (None,))[0] != version or self.closed.is_set():
                    return
            self.formatter.format(path_from_uri(uri), text)
        except (ValueError, FormatError, OSError):
            # Save-time requests report errors; speculative work must not show errors while typing.
            pass

    def schedule(self, uri):
        with self.documents_lock:
            timer = self.timers.pop(uri, None)
            if timer:
                timer.cancel()
            if not self.options.preformat_on_change:
                return
            version, text = self.documents[uri]
            def enqueue():
                if not self.closed.is_set():
                    try:
                        self.executor.submit(self._preformat, uri, version, text)
                    except RuntimeError:
                        pass
            timer = threading.Timer(self.options.debounce_ms / 1000, enqueue)
            timer.daemon = True
            self.timers[uri] = timer
            timer.start()

    def _format_request(self, request_id, uri, snapshot, cancelled):
        try:
            if cancelled.is_set():
                self.respond(request_id, error={"code": -32800, "message": "Request cancelled"})
                return
            version, text = snapshot
            result = self.formatter.format(path_from_uri(uri), text)
            with self.documents_lock:
                if cancelled.is_set():
                    self.respond(request_id, error={"code": -32800, "message": "Request cancelled"})
                elif self.documents.get(uri, (None,))[0] != version:
                    self.respond(request_id, error={"code": -32801, "message": "Document changed during formatting"})
                else:
                    self.respond(request_id, text_edits(text, result))
        except (FormatError, OSError, ValueError) as error:
            self.respond(request_id, error={"code": -32603, "message": str(error)})
        finally:
            self.pending.pop(request_id, None)

    def handle(self, message):
        request_id = message.get("id")
        method = message.get("method", "")
        params = message.get("params") or {}
        try:
            if method == "initialize":
                self.options = Options.parse(params.get("initializationOptions"))
                self.formatter.close()
                self.formatter = Formatter(self.root, self.options)
                self.respond(request_id, {"capabilities": {"positionEncoding": "utf-16", "textDocumentSync": {"openClose": True, "change": 1}, "documentFormattingProvider": True}, "serverInfo": {"name": "csharpier", "version": "0.1.0"}})
            elif method == "shutdown":
                self.shutdown = True
                self.respond(request_id)
            elif method == "exit":
                return False
            elif self.shutdown:
                if request_id is not None:
                    self.respond(request_id, error={"code": -32600, "message": "Server is shutting down"})
            elif method == "textDocument/didOpen":
                document = params["textDocument"]
                with self.documents_lock:
                    self.documents[document["uri"]] = (document["version"], document["text"])
                self.schedule(document["uri"])
            elif method == "textDocument/didChange":
                uri = params["textDocument"]["uri"]
                with self.documents_lock:
                    previous, text = self.documents[uri]
                    version = params["textDocument"]["version"]
                    if version <= previous:
                        return True
                    for change in params["contentChanges"]:
                        if "range" in change:
                            start, end = offset(text, change["range"]["start"]), offset(text, change["range"]["end"])
                            if end < start:
                                raise ValueError("Reversed edit range")
                            text = text[:start] + change["text"] + text[end:]
                        else:
                            text = change["text"]
                    self.documents[uri] = (version, text)
                self.schedule(uri)
            elif method == "textDocument/didClose":
                uri = params["textDocument"]["uri"]
                with self.documents_lock:
                    self.documents.pop(uri, None)
                    timer = self.timers.pop(uri, None)
                    if timer:
                        timer.cancel()
            elif method == "textDocument/formatting":
                uri = params["textDocument"]["uri"]
                with self.documents_lock:
                    snapshot = self.documents.get(uri)
                if snapshot is None:
                    raise ValueError("Document has not been opened")
                cancelled = threading.Event()
                self.pending[request_id] = cancelled
                self.executor.submit(self._format_request, request_id, uri, snapshot, cancelled)
            elif method == "$/cancelRequest":
                pending = self.pending.get(params.get("id"))
                if pending:
                    pending.set()
            elif method == "workspace/didChangeConfiguration":
                settings = params.get("settings") or {}
                if not settings:
                    return True
                options = Options.parse(settings.get("csharpier", settings))
                if options != self.options:
                    self.formatter.close()
                    self.options = options
                    self.formatter = Formatter(self.root, options)
            elif request_id is not None:
                self.respond(request_id, error={"code": -32601, "message": f"Unsupported method: {method}"})
        except (ValueError, KeyError, TypeError, OSError) as error:
            if request_id is not None:
                self.respond(request_id, error={"code": -32602, "message": str(error)})
            else:
                print(f"CSharpier LSP: {error}", file=sys.stderr)
        return True

    def close(self):
        self.closed.set()
        with self.documents_lock:
            for timer in self.timers.values():
                timer.cancel()
        self.executor.shutdown(wait=True, cancel_futures=True)
        self.formatter.close()

    def run(self, stream):
        try:
            while (message := read_message(stream)) is not None:
                if not self.handle(message):
                    break
        except (ValueError, OSError) as error:
            print(f"CSharpier LSP: {error}", file=sys.stderr)
        finally:
            self.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    Server(args.root).run(sys.stdin.buffer)


if __name__ == "__main__":
    main()
