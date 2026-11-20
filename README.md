# MCP Approval Guard

MCP Approval Guard is a Windows-first Python application that places a human approval gate in front of MCP tool calls. It keeps an MCP request pending until the user chooses **Allow** or **Block**, and supports concurrent requests.

## What is intercepted

- **Claude Desktop**: configure MCP servers to launch through the included stdio wrapper. Claude Desktop sends MCP JSON-RPC messages to the wrapper; the wrapper forwards allowed requests to the real MCP server and returns denied responses for blocked requests.
- **Claude Web / remote MCP**: run the HTTP proxy and point Claude's remote MCP configuration at the proxy. The proxy asks the guard before forwarding JSON-RPC tool calls to the upstream MCP HTTP endpoint.

The guard can only see traffic that is routed through a wrapper or proxy. It cannot silently intercept unmodified third-party applications or traffic protected by another process. This is an MCP-integrator boundary, not an OS-wide packet sniffer.

## Architecture

```text
Claude Desktop -> stdio wrapper -> broker -> approval UI
                                      | allow
                                      -> real stdio MCP server

Claude Web -> HTTP proxy -> broker -> approval UI
                              | allow
                              -> upstream HTTP/SSE MCP server
```

- `approval.py`: thread-safe queue and decision model.
- `app.py`: system-tray-ready Tkinter confirmation UI and local broker.
- `stdio_wrapper.py`: stdio JSON-RPC bridge for Claude Desktop MCP servers.
- `http_proxy.py`: HTTP reverse bridge for remote MCP servers.
- `config.py`: configuration loading and secure defaults.

## Installation

Requires Windows 10/11 and Python 3.10 or newer.

```powershell
cd <repository-folder>
py -m venv .venv
.venv\Scripts\Activate.ps1
py -m pip install --upgrade pip
pip install -e .
```

## Run the desktop guard

```powershell
mcp-guard
```

The UI keeps the broker running while waiting. Requests appear as topmost sequential modals, so concurrent requests remain pending and are presented one at a time.

## Configure Claude Desktop

The wrapper uses the real MCP server command and arguments as `--server-command`, plus an optional server name:

```text
mcp-guard-wrapper --broker http://127.0.0.1:8765 --server-name filesystem --server-command "C:\tools\server.exe" --server-arg "C:\allowed-folder"
```

Example Claude Desktop `claude_desktop_config.json` entry:

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "C:\\path\\to\\venv\\Scripts\\mcp-guard-wrapper.exe",
      "args": [
        "--broker", "http://127.0.0.1:8765",
        "--server-name", "filesystem",
        "--server-command", "C:\\path\\to\\server.exe",
        "--server-arg", "C:\\allowed-folder"
      ]
    }
  }
}
```

Start `mcp-guard` before starting Claude Desktop. If the broker is unavailable, the wrapper fails closed and denies MCP requests instead of forwarding them.

## Configure Claude Web / remote MCP

Start the HTTP proxy:

```powershell
mcp-guard-http --broker http://127.0.0.1:8765 --listen-host 127.0.0.1 --listen-port 8766 --upstream https://your-mcp-server.example/mcp
```

Configure Claude Web to use the local proxy URL `http://127.0.0.1:8766/mcp`. Only use an HTTP proxy on a trusted network, and expose it over TLS when accessed outside the local machine.

## Block and allow behavior

Tool calls are parsed from JSON-RPC `tools/call` requests. Blocked requests receive:

```json
{"jsonrpc":"2.0","id":"<request id>","error":{"code":-32001,"message":"Request denied by user"}}
```

Initialization and non-tool protocol messages are allowed so MCP setup can continue. Parameters are displayed in the UI for informed approval.

## Security notes

- Bind default network services to loopback only.
- Review parameters; parameters may contain secrets.
- The application cannot guarantee client compatibility where the MCP client requires direct HTTPS or vendor-specific authentication.
- No credential or parameter is written to disk in normal operation.
- Unexpected subprocess termination is reported and the wrapper fails closed.

## Tests

```powershell
py -m unittest discover -s tests -v
```

## Recording the demonstration

1. Start the guard:

   ```powershell
   mcp-guard
   ```

2. In a second terminal, start the wrapper and send initialization and tool calls:

   ```powershell
   printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"demo_search","arguments":{"query":"MCP approval"}}}\n' | mcp-guard-wrapper --server-name demo --server-command py --server-arg scripts/demo_mcp_server.py
   ```

3. Choose **Allow** for the tool popup and verify the response contains `Executed demo tool: demo_search`.
4. Repeat and choose **Block**. Verify the response contains `-32001` and `Request denied by user`.
5. Record both decisions and a short view of the request parameters as the demonstration video.
