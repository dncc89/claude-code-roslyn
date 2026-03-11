#!/usr/bin/env python3
"""
Thin proxy between Claude Code's LSP plugin and Roslyn Language Server.

Roslyn expects a rich client that:
  1. Provides proper capabilities in initialize (hierarchical symbols, workDoneProgress, etc.)
  2. Responds to workspace/configuration requests
  3. Handles window/workDoneProgress/create requests
  4. Handles client/registerCapability requests
  5. Receives solution/open + project/open notifications after init

Claude Code's LSP framework sends a bare-bones init. This wrapper patches the gap.
"""

import json
import os
import subprocess
import sys
import threading

# Ensure DOTNET_ROOT is set for the Roslyn subprocess
def _find_dotnet_root():
    """Auto-detect DOTNET_ROOT from the dotnet binary or common install paths."""
    # Already set by user/environment
    if os.environ.get("DOTNET_ROOT"):
        return os.environ["DOTNET_ROOT"]
    # Try resolving from `dotnet` on PATH
    import shutil
    dotnet = shutil.which("dotnet")
    if dotnet:
        real = os.path.realpath(dotnet)
        parent = os.path.dirname(real)
        # Homebrew layout: bin/dotnet is a wrapper, actual runtime is in libexec/
        libexec = os.path.join(os.path.dirname(parent), "libexec")
        if os.path.isfile(os.path.join(libexec, "dotnet")):
            return libexec
        # Standard layout: dotnet binary sits in the root
        if os.path.isfile(os.path.join(parent, "dotnet")):
            return parent
    # Common fallback paths
    for candidate in ["/usr/local/share/dotnet", "/usr/share/dotnet", "/opt/homebrew/share/dotnet"]:
        if os.path.isdir(candidate):
            return candidate
    return ""

DOTNET_ROOT = _find_dotnet_root()
if DOTNET_ROOT:
    os.environ["DOTNET_ROOT"] = DOTNET_ROOT
os.environ["PATH"] = os.path.expanduser("~/.dotnet/tools") + ":" + os.environ.get("PATH", "")

ROSLYN_CMD = [
    os.path.expanduser("~/.dotnet/tools/roslyn-language-server"),
    "--stdio",
    "--logLevel", "Warning",
    "--extensionLogDirectory", os.path.expanduser("~/.claude/plugins/logs/roslyn-ls"),
]

LOG_FILE = os.path.expanduser("~/.claude/plugins/logs/roslyn-ls/wrapper.log")


def log(msg):
    try:
        with open(LOG_FILE, "a") as f:
            f.write(msg + "\n")
            f.flush()
    except Exception:
        pass


def encode_message(obj):
    body = json.dumps(obj, separators=(",", ":"))
    encoded = body.encode("utf-8")
    return f"Content-Length: {len(encoded)}\r\n\r\n".encode("ascii") + encoded


def read_message(stream):
    """Read a JSON-RPC message from a byte stream. Returns parsed dict or None on EOF."""
    headers = {}
    while True:
        line = stream.readline()
        if not line:
            return None
        line = line.decode("ascii").strip()
        if line == "":
            break
        if ":" in line:
            key, val = line.split(":", 1)
            headers[key.strip()] = val.strip()

    length = int(headers.get("Content-Length", 0))
    if length == 0:
        return None

    body = b""
    while len(body) < length:
        chunk = stream.read(length - len(body))
        if not chunk:
            return None
        body += chunk

    return json.loads(body.decode("utf-8"))


def find_sln_or_csproj(root):
    """Breadth-first scan for .sln/.slnx, fallback to .csproj."""
    import collections
    queue = collections.deque([root])
    first_sln = None
    first_csproj = None
    while queue:
        d = queue.popleft()
        try:
            entries = sorted(os.listdir(d))
        except (PermissionError, OSError):
            continue
        dirs = []
        for e in entries:
            if e.startswith("."):
                continue
            full = os.path.join(d, e)
            if os.path.isdir(full):
                # Skip known heavy dirs
                if e in ("Library", "Temp", "obj", "bin", "node_modules", ".git", "Logs"):
                    continue
                dirs.append(full)
            elif os.path.isfile(full):
                if full.endswith(".sln") and first_sln is None:
                    first_sln = full
                if full.endswith(".slnx") and first_sln is None:
                    first_sln = full
                if full.endswith(".csproj") and first_csproj is None:
                    first_csproj = full
        if first_sln:
            return first_sln
        queue.extend(dirs)
    return first_csproj


def path_to_uri(path):
    from urllib.parse import quote
    abs_path = os.path.abspath(path)
    return "file://" + quote(abs_path, safe="/:")


def enhance_initialize(msg, root_path):
    """Patch the initialize request with capabilities Roslyn needs."""
    params = msg.get("params", {})

    # Ensure rootUri and rootPath
    if not params.get("rootUri"):
        params["rootUri"] = path_to_uri(root_path)
    if not params.get("rootPath"):
        params["rootPath"] = root_path

    # Ensure workspaceFolders
    if not params.get("workspaceFolders"):
        params["workspaceFolders"] = [{
            "uri": params["rootUri"],
            "name": os.path.basename(root_path),
        }]

    # Patch capabilities
    caps = params.setdefault("capabilities", {})

    # Window
    window = caps.setdefault("window", {})
    window["workDoneProgress"] = True
    window.setdefault("showMessage", {})["messageActionItem"] = {"additionalPropertiesSupport": True}
    window["showDocument"] = {"support": True}

    # Workspace
    ws = caps.setdefault("workspace", {})
    ws["applyEdit"] = True
    ws["workspaceEdit"] = {"documentChanges": True}
    ws["didChangeConfiguration"] = {"dynamicRegistration": True}
    ws["didChangeWatchedFiles"] = {"dynamicRegistration": True}
    ws["symbol"] = {"dynamicRegistration": True, "symbolKind": {"valueSet": list(range(1, 27))}}
    ws["executeCommand"] = {"dynamicRegistration": True}
    ws["configuration"] = True
    ws["workspaceFolders"] = True
    ws["workDoneProgress"] = True

    # TextDocument
    td = caps.setdefault("textDocument", {})
    td["synchronization"] = {
        "dynamicRegistration": True,
        "willSave": True,
        "willSaveWaitUntil": True,
        "didSave": True,
    }
    td["hover"] = {"dynamicRegistration": True, "contentFormat": ["markdown", "plaintext"]}
    td["signatureHelp"] = {
        "dynamicRegistration": True,
        "signatureInformation": {
            "documentationFormat": ["markdown", "plaintext"],
            "parameterInformation": {"labelOffsetSupport": True},
        },
    }
    td["definition"] = {"dynamicRegistration": True}
    td["references"] = {"dynamicRegistration": True}
    td["documentSymbol"] = {
        "dynamicRegistration": True,
        "symbolKind": {"valueSet": list(range(1, 27))},
        "hierarchicalDocumentSymbolSupport": True,
    }

    msg["params"] = params
    return msg


def handle_server_request(msg):
    """Handle requests FROM Roslyn that need a client response. Returns response or None."""
    method = msg.get("method", "")
    req_id = msg.get("id")

    if method == "workspace/configuration":
        items = msg.get("params", {}).get("items", [])
        result = []
        for item in items:
            section = item.get("section", "")
            if "enable" in section or "show" in section or "suppress" in section or "navigate" in section:
                result.append(False)
            elif "scope" in section:
                result.append("openFiles")
            elif section == "dotnet_member_insertion_location":
                result.append("with_other_members_of_the_same_kind")
            elif section == "dotnet_property_generation_behavior":
                result.append("prefer_throwing_properties")
            elif "location" in section or "behavior" in section:
                result.append(None)
            elif section in ("tab_width", "indent_size"):
                result.append(4)
            elif section == "insert_final_newline":
                result.append(True)
            else:
                result.append(None)
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    if method == "window/workDoneProgress/create":
        return {"jsonrpc": "2.0", "id": req_id, "result": None}

    if method == "client/registerCapability":
        return {"jsonrpc": "2.0", "id": req_id, "result": None}

    if method == "workspace/_roslyn_projectNeedsRestore":
        return {"jsonrpc": "2.0", "id": req_id, "result": None}

    return None


def send_solution_open(server_stdin, root_path):
    """Send solution/open and project/open notifications to Roslyn."""
    sol = find_sln_or_csproj(root_path)
    if sol and (sol.endswith(".sln") or sol.endswith(".slnx")):
        notif = {
            "jsonrpc": "2.0",
            "method": "solution/open",
            "params": {"solution": path_to_uri(sol)},
        }
        server_stdin.write(encode_message(notif))
        server_stdin.flush()
        log(f"Sent solution/open: {sol}")

    # Find all .csproj files
    csproj_files = []
    for root, dirs, files in os.walk(root_path):
        # Skip heavy dirs
        dirs[:] = [d for d in dirs if d not in ("Library", "Temp", "obj", "bin", "node_modules", ".git", "Logs")]
        for f in files:
            if f.endswith(".csproj"):
                csproj_files.append(os.path.join(root, f))

    if csproj_files:
        notif = {
            "jsonrpc": "2.0",
            "method": "project/open",
            "params": {"projects": [path_to_uri(p) for p in csproj_files]},
        }
        server_stdin.write(encode_message(notif))
        server_stdin.flush()
        log(f"Sent project/open: {csproj_files}")


def preflight_check():
    """Validate environment before launching Roslyn. Returns error message or None."""
    import shutil

    issues = []

    # Check DOTNET_ROOT resolved
    if not DOTNET_ROOT:
        issues.append("DOTNET_ROOT could not be detected — is dotnet installed?")
    elif not os.path.isdir(DOTNET_ROOT):
        issues.append(f"DOTNET_ROOT points to missing directory: {DOTNET_ROOT}")

    # Check dotnet binary
    dotnet = shutil.which("dotnet")
    if not dotnet:
        issues.append("'dotnet' not found on PATH")

    # Check roslyn-language-server
    roslyn_bin = ROSLYN_CMD[0]
    if not os.path.isfile(roslyn_bin):
        issues.append(f"roslyn-language-server not found at {roslyn_bin}")
    elif not os.access(roslyn_bin, os.X_OK):
        issues.append(f"roslyn-language-server not executable: {roslyn_bin}")

    return issues


def send_lsp_error(msg):
    """Send an LSP showMessage error to the client (best-effort, before server is up)."""
    notif = {
        "jsonrpc": "2.0",
        "method": "window/showMessage",
        "params": {"type": 1, "message": msg},  # 1 = Error
    }
    try:
        sys.stdout.buffer.write(encode_message(notif))
        sys.stdout.buffer.flush()
    except Exception:
        pass


def main():
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    log("=== Roslyn wrapper starting ===")

    # Preflight
    log(f"DOTNET_ROOT: {DOTNET_ROOT or '(not found)'}")
    log(f"roslyn-language-server: {ROSLYN_CMD[0]}")
    log(f"PATH: {os.environ.get('PATH', '')[:200]}")

    issues = preflight_check()
    if issues:
        for issue in issues:
            log(f"PREFLIGHT FAIL: {issue}")
        # Send error to client, then exit cleanly
        send_lsp_error("Roslyn LSP preflight failed: " + "; ".join(issues))
        sys.exit(1)

    log("Preflight OK")

    # Start Roslyn server
    try:
        proc = subprocess.Popen(
            ROSLYN_CMD,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (FileNotFoundError, PermissionError, OSError) as e:
        log(f"Failed to start Roslyn: {e}")
        send_lsp_error(f"Failed to start Roslyn: {e}")
        sys.exit(1)

    log(f"Roslyn server started, pid={proc.pid}")

    # Check it didn't immediately crash
    import time
    time.sleep(0.5)
    if proc.poll() is not None:
        stderr_out = proc.stderr.read().decode("utf-8", errors="replace")[:500]
        log(f"Roslyn exited immediately with code {proc.returncode}: {stderr_out}")
        send_lsp_error(f"Roslyn exited immediately (code {proc.returncode}): {stderr_out}")
        sys.exit(1)

    root_path = None
    initialized = False

    # Server → Client (Roslyn stdout → our stdout)
    def server_to_client():
        nonlocal root_path
        while True:
            msg = read_message(proc.stdout)
            if msg is None:
                log("Roslyn server closed stdout")
                break

            # If it's a request FROM the server, try to handle it
            if "id" in msg and "method" in msg:
                response = handle_server_request(msg)
                if response:
                    log(f"Handled server request: {msg['method']}")
                    proc.stdin.write(encode_message(response))
                    proc.stdin.flush()
                    continue

            # Suppress noisy notifications we don't need to forward
            method = msg.get("method", "")
            if method in ("$/progress", "window/logMessage"):
                continue

            # Forward everything else to Claude Code
            sys.stdout.buffer.write(encode_message(msg))
            sys.stdout.buffer.flush()

    # Stderr passthrough for debugging
    def stderr_reader():
        for line in proc.stderr:
            log(f"ROSLYN STDERR: {line.decode('utf-8', errors='replace').strip()}")

    t_server = threading.Thread(target=server_to_client, daemon=True)
    t_server.start()

    t_stderr = threading.Thread(target=stderr_reader, daemon=True)
    t_stderr.start()

    # Client → Server (our stdin → Roslyn stdin)
    while True:
        msg = read_message(sys.stdin.buffer)
        if msg is None:
            log("Client closed stdin, shutting down")
            break

        method = msg.get("method", "")

        # Intercept initialize to enhance capabilities
        if method == "initialize":
            root_path = msg.get("params", {}).get("rootPath") or msg.get("params", {}).get("rootUri", "").replace("file://", "")
            if not root_path:
                root_path = os.getcwd()
            log(f"Initialize with root: {root_path}")
            msg = enhance_initialize(msg, root_path)

        # After initialized, send solution/open
        if method == "initialized":
            log("Client sent initialized, forwarding then opening solution")
            proc.stdin.write(encode_message(msg))
            proc.stdin.flush()
            if root_path:
                send_solution_open(proc.stdin, root_path)
            continue

        # Forward to Roslyn
        proc.stdin.write(encode_message(msg))
        proc.stdin.flush()

    # Cleanup
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    log("=== Roslyn wrapper exiting ===")


if __name__ == "__main__":
    main()
