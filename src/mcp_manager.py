"""
mcp_manager.py

Manages connections to MCP (Model Context Protocol) tool servers.
Each server exposes tools that are made available to the agent loop.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import sys
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# RUFFS: límites de conexión. Los lanzadores de paquetes (npx, uvx, pipx…)
# pueden descargar el servidor la primera vez, así que tienen más margen.
_CONNECT_TIMEOUT = float(os.environ.get("RUFFS_MCP_CONNECT_TIMEOUT", "30"))
_CONNECT_TIMEOUT_LAUNCHER = float(os.environ.get("RUFFS_MCP_CONNECT_TIMEOUT_LAUNCHER", "120"))
_PACKAGE_LAUNCHERS = {"npx", "npx.cmd", "uvx", "uvx.exe", "pipx", "pipx.exe", "bunx", "pnpx", "pnpm", "yarn"}
_MAX_PARALLEL_CONNECTS = 4


def _connect_timeout(command: Optional[str], args: Optional[List[str]]) -> float:
    # Both separators: configs copied between Windows and macOS keep their paths.
    base = re.split(r"[\\/]", command or "")[-1].lower()
    return _CONNECT_TIMEOUT_LAUNCHER if base in _PACKAGE_LAUNCHERS else _CONNECT_TIMEOUT


_RUFFS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _expand_placeholders(command: Optional[str], args: Optional[List[str]]):
    """RUFFS: ``{ruffs_python}`` y ``{ruffs_dir}`` → el Python y la carpeta de
    esta instalación, calculados al conectar (sirven en cualquier carpeta, en
    Windows y en macOS). Lo usan los MCP incluidos del catálogo (Ghidra)."""
    def sub(text):
        if not isinstance(text, str) or "{ruffs_" not in text:
            return text
        out = text.replace("{ruffs_python}", sys.executable).replace("{ruffs_dir}", _RUFFS_DIR)
        return os.path.normpath(out) if os.path.isabs(out) else out
    return sub(command), [sub(a) for a in (args or [])]


def _resolve_command(command: Optional[str]) -> Optional[str]:
    """Resuelve el ejecutable en el PATH (en Windows, ``npx`` → ``npx.cmd``).

    Las rutas absolutas o relativas se dejan como están. Si no se encuentra,
    se devuelve tal cual para que el error del proceso sea el original.
    """
    if not command or os.path.dirname(command):
        return command
    found = shutil.which(command)
    return found or command

def _format_mcp_connection_error(name: str, command: str = "", args: Optional[List[str]] = None, error: Exception = None) -> str:
    """Return a user-actionable MCP connection error message."""
    args = args or []
    raw_error = str(error) if error else "Unknown error"
    command_line = " ".join([command or "", *args]).strip()
    lower_command = command_line.lower()

    if "@playwright/mcp" in lower_command:
        return (
            f"{raw_error}\n\n"
            "Browser MCP could not start. On fresh installs, cache the Playwright MCP package once before connecting:\n\n"
            "npx -y @playwright/mcp@latest --version\n\n"
            "Then restart RUFFS and reconnect the Browser MCP server."
        )

    return raw_error



class McpManager:
    """Manages MCP server connections and tool routing."""

    def __init__(self):
        # server_id -> connection state
        self._connections: Dict[str, Dict[str, Any]] = {}
        # server_id -> list of tool schemas
        self._tools: Dict[str, List[Dict]] = {}
        # server_id -> MCP ClientSession
        self._sessions: Dict[str, Any] = {}
        # server_id -> exit stack (for cleanup)
        self._stacks: Dict[str, Any] = {}
        # Tracking updates to tools/connections for RAG indexing
        self._generation = 0
        # Deduplication set: prevents duplicate connections (e.g., okppt)
        self._connected_ids: set = set()


    async def connect_server(
        self,
        server_id: str,
        name: str,
        transport: str,
        command: Optional[str] = None,
        args: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
        url: Optional[str] = None,
    ) -> bool:
        """Connect to an MCP server via stdio or SSE transport."""
        if transport == "stdio":
            command, args = _expand_placeholders(command, args)
        # Deduplication: reject if same command+args already connected under another ID
        # This prevents okppt and other servers from registering twice.
        if server_id in self._connected_ids:
            logger.warning(f"MCP server already connected (skipping duplicate): {name} ({server_id})")
            return True  # silently succeed
        # Also check by command signature for NPX servers (same package, diff IDs)
        if transport == "stdio" and command and not server_id.startswith("builtin_"):
            sig = (command, tuple(args or []))
            for sid in list(self._connected_ids):
                conn = self._connections.get(sid, {})
                if conn.get("transport") == "stdio" and conn.get("command") == command and conn.get("args") == (args or []):
                    logger.warning(f"MCP server {name} ({server_id}) is duplicate of {sid} (same command signature) — skipping")
                    return True
        # RUFFS: cada servidor tiene su propio límite de tiempo. En Odysseus una
        # conexión podía quedarse esperando para siempre (initialize() sin
        # timeout) y bloquear el resto.
        timeout = _connect_timeout(command, args)
        try:
            if transport == "stdio":
                coro = self._connect_stdio(server_id, name, _resolve_command(command), args or [], env or {})
            elif transport == "sse":
                coro = self._connect_sse(server_id, name, url)
            elif transport in ("http", "streamable_http", "streamable-http"):
                coro = self._connect_http(server_id, name, url)
            else:
                logger.error(f"Unknown MCP transport: {transport}")
                self._connections[server_id] = {"status": "error", "error": f"Transporte desconocido: {transport}", "name": name}
                return False
            res = await asyncio.wait_for(coro, timeout=timeout)
            if res:
                self._generation += 1
            return res
        except asyncio.TimeoutError:
            msg = (f"El servidor MCP '{name}' no respondió en {timeout:.0f}s. "
                   "Comprueba que el comando funcione en una terminal; si usa npx/uvx, "
                   "la primera ejecución descarga el paquete y puede tardar más.")
            logger.error(msg)
            self._connections[server_id] = {"status": "error", "error": msg, "name": name,
                                             "command": command or "", "args": list(args or [])}
            self._generation += 1
            return False
        except Exception as e:
            logger.error(f"Failed to connect MCP server {name} ({server_id}): {e}")
            error_message = _format_mcp_connection_error(name, command or "", args or [], e)
            self._connections[server_id] = {"status": "error", "error": error_message, "name": name, "command": command or "", "args": list(args or [])}
            self._generation += 1
            return False

    async def _connect_stdio(self, server_id: str, name: str, command: str, args: List[str], env: Dict[str, str]) -> bool:
        """Connect to an MCP server via stdio transport."""
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
            from contextlib import AsyncExitStack

            server_params = StdioServerParameters(
                command=command,
                args=args,
                env={**os.environ, **env} if env else None,
            )

            stack = AsyncExitStack()
            try:
                transport = await stack.enter_async_context(stdio_client(server_params))
                read_stream, write_stream = transport
                session = await stack.enter_async_context(ClientSession(read_stream, write_stream))

                await session.initialize()

                # Discover tools
                tools_result = await session.list_tools()
            except BaseException:
                # BaseException: también al cancelar por timeout, para no dejar
                # el proceso del servidor huérfano.
                await stack.aclose()
                raise
            tools = []
            for tool in tools_result.tools:
                tools.append({
                    "name": tool.name,
                    "description": tool.description or "",
                    "input_schema": tool.inputSchema if hasattr(tool, 'inputSchema') else {},
                })

            self._sessions[server_id] = session
            self._stacks[server_id] = stack
            self._tools[server_id] = tools
            # Extract identity hints from env vars (e.g. email address, API name)
            # so tool descriptions can distinguish between multiple instances of
            # the same MCP server (e.g. two email accounts).
            identity_hints = []
            for k, v in (env or {}).items():
                k_lower = k.lower()
                if any(x in k_lower for x in ['email_address', 'account', 'user', 'username']):
                    identity_hints.append(v)
            identity = ", ".join(identity_hints) if identity_hints else ""

            self._connections[server_id] = {
                "status": "connected",
                "name": name,
                "transport": "stdio",
                "tool_count": len(tools),
                "identity": identity,
                "command": command,
                "args": list(args or []),
            }

            self._connected_ids.add(server_id)
            logger.info(f"MCP server connected: {name} ({server_id}) - {len(tools)} tools via stdio")
            return True

        except ImportError:
            logger.warning("MCP package not installed. Install with: pip install mcp")
            self._connections[server_id] = {"status": "error", "error": "mcp package not installed", "name": name}
            return False

    async def _connect_sse(self, server_id: str, name: str, url: str) -> bool:
        """Connect to an MCP server via SSE transport."""
        try:
            from mcp import ClientSession
            from mcp.client.sse import sse_client
            from contextlib import AsyncExitStack

            stack = AsyncExitStack()
            try:
                transport = await stack.enter_async_context(sse_client(url))
                read_stream, write_stream = transport
                session = await stack.enter_async_context(ClientSession(read_stream, write_stream))

                await session.initialize()

                # Discover tools
                tools_result = await session.list_tools()
            except BaseException:
                await stack.aclose()
                raise
            tools = []
            for tool in tools_result.tools:
                tools.append({
                    "name": tool.name,
                    "description": tool.description or "",
                    "input_schema": tool.inputSchema if hasattr(tool, 'inputSchema') else {},
                })

            self._sessions[server_id] = session
            self._stacks[server_id] = stack
            self._tools[server_id] = tools
            self._connections[server_id] = {
                "status": "connected",
                "name": name,
                "transport": "sse",
                "tool_count": len(tools),
                "command": "",
                "args": [],
            }

            self._connected_ids.add(server_id)
            logger.info(f"MCP server connected: {name} ({server_id}) - {len(tools)} tools via SSE")
            return True

        except ImportError:
            logger.warning("MCP package not installed. Install with: pip install mcp")
            self._connections[server_id] = {"status": "error", "error": "mcp package not installed", "name": name}
            return False

    async def _connect_http(self, server_id: str, name: str, url: str) -> bool:
        """RUFFS: transporte HTTP ("streamable HTTP"), el estándar actual de los
        servidores MCP remotos. Odysseus solo tenía stdio y SSE, y los remotos
        había que conectarlos a través de un proxy como mcp-remote."""
        if not url:
            self._connections[server_id] = {"status": "error", "error": "Falta la URL del servidor MCP", "name": name}
            return False
        try:
            from mcp import ClientSession
            from mcp.client.streamable_http import streamablehttp_client
            from contextlib import AsyncExitStack
        except ImportError:
            self._connections[server_id] = {"status": "error", "error": "mcp package not installed", "name": name}
            return False

        stack = AsyncExitStack()
        try:
            read_stream, write_stream, _get_session_id = await stack.enter_async_context(streamablehttp_client(url))
            session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
            await session.initialize()
            tools_result = await session.list_tools()
        except BaseException:
            await stack.aclose()
            raise
        tools = [{
            "name": t.name,
            "description": t.description or "",
            "input_schema": t.inputSchema if hasattr(t, "inputSchema") else {},
        } for t in tools_result.tools]
        self._sessions[server_id] = session
        self._stacks[server_id] = stack
        self._tools[server_id] = tools
        self._connections[server_id] = {
            "status": "connected", "name": name, "transport": "http",
            "tool_count": len(tools), "command": "", "args": [], "url": url,
        }
        self._connected_ids.add(server_id)
        logger.info(f"MCP server connected: {name} ({server_id}) - {len(tools)} tools via HTTP")
        return True

    async def disconnect_server(self, server_id: str):
        """Disconnect from an MCP server."""
        stack = self._stacks.pop(server_id, None)
        if stack:
            try:
                await stack.aclose()
            except Exception as e:
                # On shutdown the stdio client is closed from a different task
                # than the one that opened it; anyio then complains about the
                # cancel scope although the subprocess is already gone. Harmless
                # — keep it out of the console.
                if "cancel scope" in str(e):
                    logger.debug(f"MCP server {server_id} closed from another task: {e}")
                else:
                    logger.warning(f"Error closing MCP server {server_id}: {e}")

        self._sessions.pop(server_id, None)
        self._tools.pop(server_id, None)
        self._connections.pop(server_id, None)
        self._connected_ids.discard(server_id)
        self._generation += 1
        logger.info(f"MCP server disconnected: {server_id}")

    async def disconnect_all(self):
        """Disconnect from all MCP servers."""
        ids = list(self._sessions.keys())
        for sid in ids:
            await self.disconnect_server(sid)

    async def connect_all_enabled(self):
        """Connect to all enabled MCP servers from the database."""
        from src.database import McpServer, SessionLocal

        db = SessionLocal()
        try:
            rows = db.query(McpServer).filter(McpServer.is_enabled == True).all()
            servers = []
            for srv in rows:
                try:
                    args = json.loads(srv.args) if srv.args else []
                    env = json.loads(srv.env) if srv.env else {}
                except (TypeError, ValueError) as e:
                    msg = f"Configuración inválida (args/env no son JSON válido): {e}"
                    logger.error(f"MCP {srv.name} ({srv.id}): {msg}")
                    self._connections[srv.id] = {"status": "error", "error": msg, "name": srv.name}
                    continue
                servers.append(dict(server_id=srv.id, name=srv.name, transport=srv.transport,
                                    command=srv.command, args=args, env=env, url=srv.url))
        finally:
            db.close()

        # RUFFS: en paralelo (con tope de concurrencia). Odysseus los conectaba
        # uno a uno dentro de un único plazo de 20 s para todos: con varios
        # servidores, los últimos de la cola nunca llegaban a conectarse.
        sem = asyncio.Semaphore(_MAX_PARALLEL_CONNECTS)

        async def _one(cfg):
            async with sem:
                return await self.connect_server(**cfg)

        results = await asyncio.gather(*(_one(c) for c in servers), return_exceptions=True)
        ok = sum(1 for r in results if r is True)
        if servers:
            logger.info(f"MCP: {ok}/{len(servers)} servidores conectados")
        return ok

    async def call_tool(self, qualified_name: str, arguments: Dict) -> Dict:
        """Call an MCP tool by its qualified name (mcp__{server_id}__{tool_name}).

        Returns a result dict compatible with agent_tools format.
        """
        parts = qualified_name.split("__", 2)
        if len(parts) != 3 or parts[0] != "mcp":
            return {"error": f"Invalid MCP tool name: {qualified_name}", "exit_code": 1}

        server_id = parts[1]
        tool_name = parts[2]

        session = self._sessions.get(server_id)
        if not session:
            return {"error": f"MCP server not connected: {server_id}", "exit_code": 1}

        try:
            result = await self._do_call(session, tool_name, arguments)
        except Exception as e:
            # Auto-reconnect for builtin servers whose subprocess may have died
            if self.is_builtin(server_id):
                logger.warning(f"MCP call failed for {qualified_name}, attempting reconnect: {e}")
                reconnected = await self._reconnect_builtin(server_id)
                if reconnected:
                    session = self._sessions.get(server_id)
                    if session:
                        try:
                            result = await self._do_call(session, tool_name, arguments)
                        except Exception as e2:
                            logger.error(f"MCP tool call failed after reconnect: {qualified_name}: {e2}")
                            return {"error": str(e2), "exit_code": 1}
                    else:
                        return {"error": f"Reconnected but no session for {server_id}", "exit_code": 1}
                else:
                    logger.error(f"MCP reconnect failed for {server_id}")
                    return {"error": f"MCP server crashed and reconnect failed: {server_id}", "exit_code": 1}
            else:
                logger.error(f"MCP tool call failed: {qualified_name}: {e}")
                return {"error": str(e), "exit_code": 1}

        return result

    async def _do_call(self, session, tool_name: str, arguments: Dict) -> Dict:
        """Execute a single MCP tool call and return result dict."""
        result = await session.call_tool(tool_name, arguments)
        output_parts = []
        images = []
        for content in result.content:
            if hasattr(content, 'text'):
                output_parts.append(content.text)
            elif getattr(content, 'type', '') == 'image' and hasattr(content, 'data'):
                # Image content (e.g. Playwright screenshots)
                mime = getattr(content, 'mimeType', 'image/png')
                images.append({"data": content.data, "mimeType": mime})
                output_parts.append(f"[Screenshot captured ({mime})]")
            elif hasattr(content, 'data'):
                output_parts.append(str(content.data))

        output = "\n".join(output_parts)
        is_error = getattr(result, 'isError', False)

        result_dict = {
            "stdout": output if not is_error else "",
            "stderr": output if is_error else "",
            "exit_code": 1 if is_error else 0,
        }
        if images:
            result_dict["images"] = images
        return result_dict

    async def _reconnect_builtin(self, server_id: str) -> bool:
        """Tear down and reconnect a crashed builtin MCP server."""
        import sys
        from src.builtin_mcp import _BUILTIN_SERVERS

        # RUFFS: solo los integrados que trae el proyecto (mcp_servers/). Los
        # servidores propios del usuario se registran como servidores normales.
        if server_id not in _BUILTIN_SERVERS:
            return False

        script_rel, name = _BUILTIN_SERVERS[server_id]
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        script_path = os.path.join(base_dir, script_rel)

        # Clean up old connection
        await self.disconnect_server(server_id)

        try:
            ok = await self.connect_server(
                server_id=server_id,
                name=name,
                transport="stdio",
                command=sys.executable,
                args=[script_path],
                env={"PYTHONPATH": base_dir},
            )
            if ok:
                logger.info(f"Reconnected builtin MCP server: {name}")
            return ok
        except Exception as e:
            logger.error(f"Failed to reconnect builtin MCP server {name}: {e}")
            return False

    def get_all_openai_schemas(self, disabled_map: Optional[Dict[str, set]] = None) -> List[Dict]:
        """Return all MCP tools in OpenAI function-calling format.

        Tool names are namespaced as mcp__{server_id}__{tool_name}.
        disabled_map: optional {server_id: set_of_disabled_tool_names} to filter out.
        """
        schemas = []
        for server_id, tools in self._tools.items():
            # Skip builtin Python servers — they use the code-block tool format
            # But include NPX-based builtins (like browser) which need function calling
            if self.is_builtin(server_id) and server_id != "builtin_browser":
                continue
            conn = self._connections.get(server_id, {})
            server_name = conn.get("name", server_id)
            disabled = (disabled_map or {}).get(server_id, set())

            identity = conn.get("identity", "")
            label = f"{server_name} ({identity})" if identity else server_name

            for tool in tools:
                if tool["name"] in disabled:
                    continue
                qualified = f"mcp__{server_id}__{tool['name']}"
                schema = {
                    "type": "function",
                    "function": {
                        "name": qualified,
                        "description": f"[MCP:{label}] {tool['description']}",
                        "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
                    },
                }
                schemas.append(schema)

        return schemas

    def get_all_tools(self, disabled_map: Optional[Dict[str, set]] = None) -> List[Dict]:
        """Return a flat list of all discovered tools with server info."""
        result = []
        for server_id, tools in self._tools.items():
            conn = self._connections.get(server_id, {})
            disabled = (disabled_map or {}).get(server_id, set())
            for tool in tools:
                result.append({
                    "server_id": server_id,
                    "server_name": conn.get("name", server_id),
                    "name": tool["name"],
                    "qualified_name": f"mcp__{server_id}__{tool['name']}",
                    "description": tool.get("description", ""),
                    "is_disabled": tool["name"] in disabled,
                })
        return result

    def is_builtin(self, server_id: str) -> bool:
        """Check if a server is a built-in (auto-registered) server."""
        return server_id.startswith("builtin_") or server_id in {
            "image_gen",
            "memory",
            "rag",
            "email",
        }

    def get_server_status(self, server_id: str) -> Dict:
        """Get connection status for a server."""
        return self._connections.get(server_id, {"status": "disconnected"})

    def get_all_statuses(self) -> Dict[str, Dict]:
        """Get connection statuses for all servers."""
        return dict(self._connections)

    _cached_prompt_desc = None
    _cached_prompt_desc_key = None

    def get_tool_descriptions_for_prompt(self, disabled_map: Optional[Dict[str, set]] = None) -> str:
        """Generate text describing MCP tools for the agent system prompt. Cached."""
        cache_key = (
            frozenset((k, frozenset(v)) for k, v in (disabled_map or {}).items()),
            len(self._tools),
            self._generation,
        )
        if self._cached_prompt_desc is not None and self._cached_prompt_desc_key == cache_key:
            return self._cached_prompt_desc
        tools = self.get_all_tools(disabled_map)
        if not tools:
            return ""

        lines = ["\n\nYou also have access to external MCP tool servers. These tools are called via native function calling:"]
        by_server = {}
        for t in tools:
            # Skip builtin Python servers — they're already in the agent prompt
            # But include NPX-based builtins (like browser) which aren't hardcoded
            if self.is_builtin(t["server_id"]) and t["server_id"] != "builtin_browser":
                continue
            if t.get("is_disabled"):
                continue
            sn = t["server_name"]
            if sn not in by_server:
                by_server[sn] = []
            by_server[sn].append(t)

        if not by_server:
            return ""

        for server_name, server_tools in by_server.items():
            # Include identity (e.g. email address) if available
            sid = server_tools[0]["server_id"] if server_tools else ""
            identity = self._connections.get(sid, {}).get("identity", "")
            label = f"{server_name} ({identity})" if identity else server_name
            lines.append(f"\n**{label}:**")
            for t in server_tools:
                # Truncate long descriptions
                desc = t['description'][:120] + '...' if len(t['description']) > 120 else t['description']
                lines.append(f"  - {t['qualified_name']}: {desc}")

        result = "\n".join(lines)
        self._cached_prompt_desc = result
        self._cached_prompt_desc_key = cache_key
        return result
