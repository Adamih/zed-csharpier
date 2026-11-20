use std::{env, fs};
use zed_extension_api::{self as zed, settings::LspSettings};

struct CSharpier;

impl zed::Extension for CSharpier {
    fn new() -> Self {
        Self
    }

    fn language_server_command(
        &mut self,
        id: &zed::LanguageServerId,
        worktree: &zed::Worktree,
    ) -> zed::Result<zed::Command> {
        let settings = LspSettings::for_worktree(id.as_ref(), worktree)?;
        if let Some(binary) = settings.binary {
            if let Some(path) = binary.path {
                return Ok(zed::Command {
                    command: path,
                    args: binary.arguments.unwrap_or_default(),
                    env: worktree.shell_env(),
                });
            }
        }
        let python = worktree
            .which("python3")
            .or_else(|| worktree.which("python"))
            .ok_or("CSharpier extension requires Python 3.10+ on PATH")?;
        // Embed the dependency-free bridge so the extension can be copied to any project.
        let script = env::current_dir()
            .map_err(|error| error.to_string())?
            .join("csharpier_lsp.py");
        fs::write(&script, include_bytes!("../server/csharpier_lsp.py"))
            .map_err(|error| error.to_string())?;
        Ok(zed::Command {
            command: python,
            args: vec![
                "-u".into(),
                script.to_string_lossy().into_owned(),
                "--root".into(),
                worktree.root_path(),
            ],
            env: worktree.shell_env(),
        })
    }

    fn language_server_initialization_options(
        &mut self,
        id: &zed::LanguageServerId,
        worktree: &zed::Worktree,
    ) -> zed::Result<Option<zed::serde_json::Value>> {
        Ok(LspSettings::for_worktree(id.as_ref(), worktree)?.initialization_options)
    }
}

zed::register_extension!(CSharpier);
