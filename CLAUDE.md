# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A Claude Code LSP plugin that provides C# language intelligence via the Roslyn Language Server. The core is a Python wrapper (`plugins/roslyn-ls/roslyn-wrapper.py`) that acts as a thin proxy between Claude Code's LSP framework and the Roslyn Language Server, bridging capability gaps.

## Architecture

The wrapper solves a specific problem: Roslyn expects a rich IDE client, but Claude Code sends bare-bones LSP init. The wrapper:

1. **Enhances `initialize`** — patches capabilities Roslyn needs (hierarchical symbols, workDoneProgress, workspace folders, etc.)
2. **Handles server requests** — responds to `workspace/configuration`, `window/workDoneProgress/create`, `client/registerCapability`, and `_roslyn_projectNeedsRestore` on behalf of Claude Code
3. **Auto-discovers projects** — BFS scans for `.sln` (preferred) or `.csproj` files and sends `solution/open` + `project/open` notifications after initialization
4. **Suppresses noise** — filters `$/progress` and `window/logMessage` notifications from reaching Claude Code

Communication is JSON-RPC over stdio. Three threads: client→server (main thread), server→client, and stderr logging.

## Prerequisites

- .NET SDK installed with `dotnet` on PATH
- Roslyn Language Server: `cd /tmp && dotnet tool install --global roslyn-language-server --prerelease`
- Python 3

## Plugin Structure

- `.claude-plugin/marketplace.json` — plugin registry, declares the `roslyn-ls` plugin
- `plugins/roslyn-ls/.claude-plugin/plugin.json` — plugin metadata
- `plugins/roslyn-ls/roslyn-wrapper.py` — the wrapper (single file, ~440 lines)

## Debugging

Logs go to `~/.claude/plugins/logs/roslyn-ls/wrapper.log`. The wrapper logs preflight checks, Roslyn process lifecycle, handled server requests, and stderr output from Roslyn.
