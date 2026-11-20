#!/usr/bin/env python3
"""Measure complete stdio LSP request/response latency, not just worker time."""
import argparse
import json
from pathlib import Path
import platform
import statistics
import subprocess
import time

from client import Client, ROOT
from test_server import apply_edits


def summary(values):
    ordered = sorted(values)
    return {"samples": len(values), "median_ms": round(statistics.median(values), 3), "p95_ms": round(ordered[min(len(ordered) - 1, int(len(ordered) * .95))], 3), "max_ms": round(max(values), 3)}


def measure(client, path):
    start = time.perf_counter_ns()
    response = client.format(path)
    elapsed = (time.perf_counter_ns() - start) / 1_000_000
    if "error" in response:
        raise RuntimeError(response["error"])
    return elapsed, response["result"]


def benchmark(iterations):
    source = (ROOT / "backend/Example.cs").read_text()
    path = ROOT / "backend/Benchmark.cs"
    report = {"platform": platform.system() + " " + platform.machine(), "python": platform.python_version(), "source_bytes": len(source.encode()), "measurement": "stdio LSP round trip including JSON, IPC, config checks and edits; excludes Zed UI rendering"}
    with Client(options={"preformat_on_change": False}) as client:
        client.open(path, source)
        cold, edits = measure(client, path)
        report["cold_first_save_ms"] = round(cold, 3)
        formatted = apply_edits(source, edits)
        values = []
        for i in range(iterations):
            # Every buffer is different: these requests cannot hit the content cache.
            changed = source.rstrip() + f"\n// Unique benchmark edit {i}\n"
            client.change(path, changed)
            elapsed, edits = measure(client, path)
            values.append(elapsed)
            assert apply_edits(changed, edits)
        report["changed_buffer_warm_worker"] = summary(values)
        client.change(path, formatted)
        measure(client, path)
        values = [measure(client, path)[0] for _ in range(iterations)]
        report["unchanged_buffer_cached"] = summary(values)
        project = ROOT / "backend/Benchmark.csproj"
        xml = '<Project Sdk="Microsoft.NET.Sdk.Web"><PropertyGroup><TargetFramework>net11.0</TargetFramework></PropertyGroup></Project>'
        client.open(project, xml)
        measure(client, project)
        values = []
        for i in range(iterations):
            client.change(project, xml + f"\n<!-- Unique edit {i} -->")
            values.append(measure(client, project)[0])
        report["changed_xml_warm_worker"] = summary(values)
    with Client() as client:
        client.open(path, source)
        measure(client, path)
        values = []
        for i in range(min(iterations, 15)):
            client.change(path, source.rstrip() + f"\n// Preformatted edit {i}\n")
            time.sleep(.15)  # Simulate a short pause between typing and Save.
            values.append(measure(client, path)[0])
        report["save_after_150ms_typing_pause"] = summary(values)
    command = ["mise", "exec", "--", "dotnet", "csharpier", "format", "--write-stdout", "--stdin-path", str(path)]
    values = []
    for _ in range(5):
        start = time.perf_counter_ns()
        subprocess.run(command, input=source, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, cwd=ROOT)
        values.append((time.perf_counter_ns() - start) / 1_000_000)
    report["previous_external_formatter"] = summary(values)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=100)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = benchmark(args.iterations)
    text = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(text)
    print(text)
