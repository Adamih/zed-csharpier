# CSharpier for Zed

A Zed extension that keeps CSharpier running instead of launching .NET on every save.
It provides formatting only; Roslyn remains responsible for completion, navigation,
and diagnostics. Supports C#, C# project files, MSBuild files, and XML solution files.

This is a standalone dev extension, not yet listed in Zed's extension marketplace.

## Install

### Prebuilt release (no Rust build required)

Download the tarball and `SHA256SUMS` from
[GitHub Releases](https://github.com/Adamih/zed-csharpier/releases), verify its checksum,
and extract it. Run `python3 install.py` inside the extracted folder. The release
installer copies the prebuilt WebAssembly extension into Zed's extensions directory;
it does not compile anything or change project installation tasks.

Python 3.10+, Zed's C# extension, and the project's local CSharpier tool are still
required. The installer refuses to overwrite an existing installation; uninstall an
older CSharpier extension through Zed before installing the release.

### Build from source

Requirements:

- Zed with the [C# extension](https://github.com/zed-extensions/csharp).
- Python 3.10+ on PATH; the bridge uses only the standard library.
- Rust installed through rustup to build the extension. The toolchain file pins the
  compiler and the `wasm32-wasip2` target.
- The .NET SDK/runtime required by your project's CSharpier tool version.
- CSharpier installed as a local .NET tool in each project you want to format.

```sh
git clone https://github.com/Adamih/zed-csharpier.git
cd zed-csharpier
python3 install.py
```

The installer builds the WebAssembly plugin and registers it using Zed's standard
local dev-extension symlink. Zed watches the installed extensions directory and loads
it automatically. You can alternatively run `python3 install.py --build-only`, then use
**Extensions → Install Dev Extension** and select this directory.

Keep the cloned directory in place while the dev extension is installed. After changing
the plugin, run `python3 install.py` again to rebuild it. The installer refuses to replace
an unrelated installed extension. `--zed-data-dir PATH` supports alternate Zed profiles.

Inside a project without CSharpier yet:

```sh
dotnet new tool-manifest
dotnet tool install csharpier
```

For an existing project, `dotnet tool restore` restores its pinned version. The plugin
never installs tools or downloads executable code at runtime.

## Zed settings

Add this to the project's `.zed/settings.json` or your Zed user settings:

```json
{
  "languages": {
    "CSharp": {
      "language_servers": ["csharpier", "roslyn", "!csharp-ls", "!omnisharp", "..."],
      "formatter": { "language_server": { "name": "csharpier" } },
      "format_on_save": "on",
      "use_on_type_format": false,
      "remove_trailing_whitespace_on_save": false
    },
    "C# Project File": {
      "language_servers": ["csharpier", "..."],
      "formatter": { "language_server": { "name": "csharpier" } },
      "format_on_save": "on",
      "remove_trailing_whitespace_on_save": false
    },
    "MSBuild File": {
      "language_servers": ["csharpier", "..."],
      "formatter": { "language_server": { "name": "csharpier" } },
      "format_on_save": "on",
      "remove_trailing_whitespace_on_save": false
    },
    "C# Solution File": {
      "language_servers": ["csharpier", "..."],
      "formatter": { "language_server": { "name": "csharpier" } },
      "format_on_save": "on",
      "remove_trailing_whitespace_on_save": false
    }
  }
}
```

The language names come from Zed's C# extension. C# uses `CSharp`, not `C#`.
The C# configuration keeps Roslyn and disables the alternative C# servers to avoid
starting redundant servers. The `...` preserves other configured language servers.
Only the named CSharpier server handles full-document formatting.

CSharpier supports `.cs`, `.csproj`, `.props`, `.targets`, and `.slnx` by default.
For the `.proj` extension, add a `.csharpierrc.json` override:

```json
{
  "overrides": [
    { "files": "**/*.proj", "formatter": "xml", "indentSize": 2 }
  ]
}
```

Traditional `.sln` files are not XML and are not supported. This extension does not
replace the XML formatter for arbitrary resource files or change project source files
on disk; it returns edits to Zed for the buffer being formatted.

## Configuration

Optional configuration under Zed's `lsp` key:

```json
{
  "lsp": {
    "csharpier": {
      "initialization_options": {
        "timeout_ms": 5000,
        "idle_timeout_seconds": 300,
        "cache_entries": 64,
        "cache_bytes": 8388608,
        "preformat_on_change": true,
        "debounce_ms": 75
      }
    }
  }
}
```

- `timeout_ms`: Maximum time for a worker response, including its first startup.
- `idle_timeout_seconds`: Stop unused workers after this period; `0` keeps them alive
  until Zed closes the language server. Cached results remain available.
- `cache_entries` / `cache_bytes`: Bounds for the in-memory result cache. Set either to
  `0` to disable caching. Source text is never cached on disk.
- `preformat_on_change`: Format a recent buffer snapshot in the background so Save
  can retrieve a cached result. This does not apply edits while you type.
- `debounce_ms`: Wait for a typing pause before speculative formatting. Superseded
  snapshots are skipped.
- `command`: Optional string array specifying a complete pipe-mode worker command.
  For a global install, use `["csharpier", "pipe-files"]`.

By default, the bridge finds the nearest `dotnet-tools.json` or
`.config/dotnet-tools.json` containing CSharpier. It starts a worker in that directory,
using `mise exec -- dotnet csharpier pipe-files` when mise is on PATH, otherwise
`dotnet csharpier pipe-files`. Each project uses its own pinned tool rather than
silently upgrading or sharing a global version. Nested local tool manifests are
supported. A root manifest without CSharpier produces a clear restore/install error.

Changing formatter configuration or tool manifests invalidates matching cache keys;
relevant metadata includes inherited/nested `.editorconfig`, `.csharpierrc*`, ignore
files, tool manifests, `global.json`, and mise files. Restart the language server after
changing the extension's initialization options or upgrading the extension itself.

## Performance

The first request needs to start .NET and warm up CSharpier. After that, the worker
and the LSP connection are reused. A bounded cache handles repeated buffer contents;
preformatting can populate it during a short pause before Save.

Measurements in `benchmark-results.json` include the **complete stdio LSP request and
response**: JSON, IPC, config checks, CSharpier, and edit generation. They exclude Zed's
UI rendering. These are local measurements, not a universal latency guarantee.

Representative results on Linux x86_64 for a small C# file:

| Case | Median |
| --- | ---: |
| Previous external formatter, new process each save | about 499 ms |
| Changed C# buffer, warm worker, no cache hit | about 3.3 ms |
| Changed XML project file, warm worker | about 2.6 ms |
| Unchanged/cached save | about 1.1 ms |
| Save after a 150 ms typing pause | about 1.4 ms |

The cold first save was about 432 ms. Opening a file normally starts speculative
formatting before the first Save. Larger files, cold processes and heavy system load
can take longer; sub-millisecond formatting of arbitrary changed files is not promised.

Run the benchmark on your own machine:

```sh
dotnet tool restore --tool-manifest tests/fixtures/project/dotnet-tools.json
python3 tests/benchmark.py --output benchmark-results.json
```

The benchmark uses synthetic fixtures contained in this repository. It never reads
or uploads source from another project. It separates genuinely different buffers from
cached requests, and compares against the old one-process-per-save command.

## Reliability and lifecycle

- Formatting is serialized per bridge to keep CSharpier's pipe protocol synchronized.
- Requests are asynchronous: document updates and cancellation remain responsive.
- Stale results return `ContentModified`; they cannot overwrite newer typing.
- UTF-16 LSP positions, Unicode and CRLF are handled when producing minimal edits.
- A failed, ignored, unsupported or invalid nonempty buffer cannot become an empty file.
  CSharpier errors are returned to Zed instead of replacing buffer contents.
- A timed-out or crashed worker is terminated and restarted on the next request.
- Workers are cleaned up on idle expiry, language-server shutdown or stdin EOF.
- No HTTP ports are opened and no credentials are needed by the bridge.
- Linux installation and execution are verified. macOS/Windows paths are supported
  by the installer but have not been tested here; the Python command can be overridden
  through Zed's `lsp.csharpier.binary` settings if needed.

## Development

The plugin builds in its own repository. Application dependency installation should
not compile or install editor plugins; installation is always a separate explicit step.

```sh
cargo fmt --check
python3 install.py --build-only
dotnet tool restore --tool-manifest tests/fixtures/project/dotnet-tools.json
python3 -m unittest discover -s tests -v
```

Run `python3 release.py` to build the precompiled tarball and SHA-256 checksums in
`dist/`. The release archive contains only the WASM, manifest, installer, README and
license.

Or use the `build`, `install`, `test`, and `benchmark` mise tasks. Tests cover real
CSharpier integration, XML project files, invalid syntax, Unicode and CRLF ranges,
cache invalidation, timeouts, cancellation, stale results, debounce and worker cleanup.

The extension uses Zed's language-server API, not an unsupported formatter hook.
References: [Zed language servers](https://zed.dev/docs/extensions/languages),
[CSharpier pipe mode](https://csharpier.com/docs/CLI#pipe-files).
