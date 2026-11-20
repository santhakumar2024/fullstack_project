# MCP Approval Guard

MCP Approval Guard is a Python application that places a human approval gate in front of MCP tool calls. It keeps an MCP request pending until the user chooses **Allow** or **Block**, and supports concurrent requests.

## What is intercepted

- **Claude Desktop**: configure MCP servers to launch through the included stdio wrapper. Claude Desktop sends MCP JSON-RPC messages to the wrapper; the wrapper forwards allowed requests to the real MCP server and returns denied responses for blocked requests.
- **Claude Web / remote MCP**: run the HTTP proxy and point Claude's remote MCP configuration at the proxy. The proxy asks the guard before forwarding JSON-RPC tool calls to the upstream MCP HTTP endpoint.

The guard can only see traffic that is routed through a wrapper or proxy. It cannot silently intercept unmodified third-party applications or traffic protected by another process. This is an MCP-integrator boundary, not an OS-wide packet sniffer.

## Architecture

```
Claude Desktop -> stdio wrapper -> broker -> approval UI
                                      | allow
                                      -> real stdio MCP server

Claude Web -> HTTP proxy -> broker -> approval UI
                          | allow
                          -> upstream HTTP/SSE MCP server
```

### Components

| File | Purpose |
|------|---------|
| `approval.py` | Thread-safe queue and decision model for pending requests |
| `app.py` | Original Tkinter confirmation UI and local broker |
| `app_enhanced.py` | **Enhanced** broker with system tray, logging, health checks, graceful shutdown |
| `stdio_wrapper.py` | Original stdio JSON-RPC bridge for Claude Desktop MCP servers |
| `stdio_wrapper_enhanced.py` | **Enhanced** wrapper with retries, binary mode, health checks, structured logging |
| `http_proxy.py` | Original HTTP reverse bridge for remote MCP servers |
| `http_proxy_enhanced.py` | **Enhanced** proxy with async retries, batch support, health endpoint |
| `config.py` | Configuration loading and secure defaults |

### Enhanced Features

The enhanced versions (`*_enhanced.py`) add:
- **Background operation**: Runs as daemon with proper signal handling (SIGINT/SIGTERM)
- **System tray support**: Minimizes to tray (via `pystray` if available)
- **Always-on-top popups**: Confirmation dialogs appear above all windows
- **Structured logging**: File + console output with timestamps
- **Health checks**: `/health` endpoints for monitoring
- **Retry logic**: Exponential backoff for broker communication
- **Graceful error handling**: Fail-closed behavior, proper cleanup on termination
- **Concurrent request handling**: Thread-safe queue processes requests sequentially

## Installation

Requires Python 3.10+ (Windows 10/11, Linux, macOS).

```powershell
cd <repository-folder>
py -m venv .venv
.venv\Scripts\Activate.ps1
py -m pip install --upgrade pip
pip install -e .
# For system tray support (optional):
pip install pystray pillow
```

## Quick Start (Demo)

### 1. Start the broker (Terminal 1)
```powershell
python -m mcp_guard.app_enhanced
```
This starts the broker on `http://127.0.0.1:8765` and shows a system tray icon. The GUI popup appears when a tool call needs approval.

### 2. Test with the demo server (Terminal 2)
```powershell
# Initialize
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' | python -m mcp_guard.stdio_wrapper_enhanced --broker http://127.0.0.1:8765 --server-name demo --server-command python --server-arg scripts/demo_mcp_server.py --health-check

# Tool call - popup will appear asking Allow/Block
echo '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"demo_search","arguments":{"query":"MCP approval"}}}' | python -m mcp_guard.stdio_wrapper_enhanced --broker http://127.0.0.1:8765 --server-name demo --server-command python --server-arg scripts/demo_mcp_server.py --health-check
```

### 3. For Claude Web / remote MCP (Terminal 3)
```powershell
python -m mcp_guard.http_proxy_enhanced --broker http://127.0.0.1:8765 --listen-port 8766 --upstream https://your-mcp-server.example/mcp --server-name remote --health-check
```

## Configure Claude Desktop

The wrapper uses the real MCP server command and arguments as `--server-command`, plus an optional server name:

```text
python -m mcp_guard.stdio_wrapper_enhanced --broker http://127.0.0.1:8765 --server-name filesystem --server-command "C:\tools\server.exe" --server-arg "C:\allowed-folder"
```

Example Claude Desktop `claude_desktop_config.json` entry:

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "C:\\path\\to\\venv\\Scripts\\python.exe",
      "args": [
        "-m", "mcp_guard.stdio_wrapper_enhanced",
        "--broker", "http://127.0.0.1:8765",
        "--server-name", "filesystem",
        "--server-command", "C:\\path\\to\\server.exe",
        "--server-arg", "C:\\allowed-folder",
        "--health-check"
      ]
    }
  }
}
```

Start the broker (`python -m mcp_guard.app_enhanced`) before starting Claude Desktop. If the broker is unavailable, the wrapper fails closed and denies MCP requests instead of forwarding them.

## Configure Claude Web / Remote MCP

Start the HTTP proxy:

```powershell
python -m mcp_guard.http_proxy_enhanced --broker http://127.0.0.1:8765 --listen-host 127.0.0.1 --listen-port 8766 --upstream https://your-mcp-server.example/mcp --server-name remote --health-check
```

Configure Claude Web to use the local proxy URL `http://127.0.0.1:8766/mcp`. Only use an HTTP proxy on a trusted network, and expose it over TLS when accessed outside the local machine.

## Block and Allow Behavior

Tool calls are parsed from JSON-RPC `tools/call` requests. Blocked requests receive:

```json
{"jsonrpc":"2.0","id":"<request id>","error":{"code":-32001,"message":"Request denied by user"}}
```

Initialization and non-tool protocol messages are allowed so MCP setup can continue. Parameters are displayed in the UI for informed approval.

### Concurrent Requests

Multiple simultaneous tool calls are queued and presented sequentially. The user must respond to each popup before the next is shown. The broker waits up to the configured timeout (default 300s) for a decision before defaulting to "block".

## Security Notes

- Bind default network services to loopback only (`127.0.0.1`)
- Review parameters; parameters may contain secrets
- The application cannot guarantee client compatibility where the MCP client requires direct HTTPS or vendor-specific authentication
- No credential or parameter is written to disk in normal operation
- Unexpected subprocess termination is reported and the wrapper fails closed
- Broker communication uses a session ID for traceability

## Configuration

Create `~/.mcp-approval-guard.json` to customize:

```json
{
  "broker_host": "127.0.0.1",
  "broker_port": 8765,
  "approval_timeout": 300.0
}
```

Or set environment variable `MCP_GUARD_CONFIG` to a custom config file path.

## Command Reference

### Enhanced Broker (with GUI)
```powershell
python -m mcp_guard.app_enhanced
# Runs broker + Tkinter UI with system tray support
```

### Enhanced stdio Wrapper (Claude Desktop)
```powershell
python -m mcp_guard.stdio_wrapper_enhanced --help
# Options:
#   --broker URL              Broker URL (default: http://127.0.0.1:8765)
#   --server-name NAME        Server name for approval (default: unknown)
#   --server-command CMD      Command to run MCP server (required)
#   --server-arg ARG          Arguments for server command (repeatable)
#   --max-retries N           Max broker retries (default: 3)
#   --retry-delay SEC         Base retry delay (default: 1.0)
#   --health-check            Check broker health on startup
```

### Enhanced HTTP Proxy (Claude Web / Remote)
```powershell
python -m mcp_guard.http_proxy_enhanced --help
# Options:
#   --broker URL              Broker URL (default: http://127.0.0.1:8765)
#   --listen-host HOST        Listen host (default: 127.0.0.1)
#   --listen-port PORT        Listen port (default: 8766)
#   --upstream URL            Upstream MCP server URL (required)
#   --server-name NAME        Server name for approval (default: remote)
#   --max-retries N           Max broker retries (default: 3)
#   --retry-delay SEC         Base retry delay (default: 1.0)
#   --health-check            Check broker health on startup
```

## Testing

```powershell
# Run unit tests
py -m unittest discover -s tests -v
```

### Manual Test (Allow)
```powershell
# Terminal 1: Start broker
python -m mcp_guard.app_enhanced

# Terminal 2: Send requests
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' | python -m mcp_guard.stdio_wrapper_enhanced --broker http://127.0.0.1:8765 --server-name demo --server-command python --server-arg scripts/demo_mcp_server.py --health-check

echo '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"demo_search","arguments":{"query":"test"}}}' | python -m mcp_guard.stdio_wrapper_enhanced --broker http://127.0.0.1:8765 --server-name demo --server-command python --server-arg scripts/demo_mcp_server.py --health-check
# Click "Allow" in popup -> response contains "Executed demo tool: demo_search"
```

### Manual Test (Block)
```powershell
# Repeat above but click "Block" in popup
# Response contains: {"jsonrpc":"2.0","id":2,"error":{"code":-32001,"message":"Request denied by user"}}
```

## Architecture Explanation

### Approach to Monitoring and Interception

MCP Approval Guard operates at the **integration boundary** — it does not perform OS-level interception or packet sniffing. Instead:

1. **Claude Desktop (stdio)**: The MCP server command in `claude_desktop_config.json` is replaced with `mcp-guard-wrapper` (or `stdio_wrapper_enhanced`), which launches the real MCP server as a subprocess. All JSON-RPC messages flow through the wrapper's stdin/stdout pipes, allowing inspection and filtering.

2. **Claude Web / Remote (HTTP)**: A local HTTP proxy (`http_proxy_enhanced`) sits between Claude Web and the upstream MCP server. Claude Web is configured to point at the local proxy, which forwards requests to the real upstream after broker approval.

3. **Broker**: A local HTTP server (`app_enhanced`) receives approval requests from both wrappers, queues them, and presents a native UI for user decisions. The broker is the single point of coordination.

### Why This Architecture?

- **No special privileges required**: Runs as a regular user process
- **Cross-platform**: Works on Windows, Linux, macOS
- **Transparent to MCP servers**: Real servers are unmodified
- **Fail-safe**: If broker is unreachable, requests are blocked (fail-closed)
- **Auditable**: All decisions logged with timestamps

## Demo Video

See `demo/` folder for recorded demonstrations of:
1. Successful request approval (Allow)
2. Request blocking (Block)
3. Concurrent request handling
4. Broker startup/shutdown

## License

MIT