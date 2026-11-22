"""
image_gen_server.py

MCP server: generar y editar imágenes con la API key de la persona
(OpenRouter u OpenAI/compatibles). La lógica está en src/image_gen.py.
"""

import asyncio
import sys
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

server = Server("image_gen")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="generate_image",
            description=("Create an image from a description with the user's own API key (OpenRouter: "
                         "google/gemini-2.5-flash-image…; OpenAI: gpt-image-1…). Returns the image URL."),
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Detailed description of the image"},
                    "model": {"type": "string", "description": "Model (optional; default: the configured one)"},
                    "size": {"type": "string", "description": "1024x1024, 1536x1024, 1024x1536 or an aspect ratio like 16:9"},
                    "quality": {"type": "string", "description": "low, medium, high, auto (OpenAI models)"},
                },
                "required": ["prompt"],
            },
        ),
        Tool(
            name="edit_image",
            description=("Modify existing images (add/remove/change elements, background, style; combine images). "
                         "images: attachment ids, /api/generated-image/... URLs, or 'last'."),
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "What to change and what must stay the same"},
                    "images": {"type": "array", "items": {"type": "string"}, "description": "Images to edit (default: last)"},
                    "model": {"type": "string"},
                    "size": {"type": "string"},
                },
                "required": ["prompt"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    # Dentro de RUFFS estas llamadas se ejecutan en el propio RUFFS (con la
    # persona y el chat: src/tool_execution.py). Esto es para clientes MCP externos.
    from src.settings import get_setting
    from src import image_gen
    if not get_setting("image_gen_enabled", True):
        return [TextContent(type="text", text="Error: Image generation is disabled by the administrator.")]
    a = arguments or {}
    if name == "generate_image":
        res = await image_gen.run(a.get("prompt", ""), model_spec=a.get("model", ""), size=a.get("size", ""),
                                  quality=a.get("quality", ""))
    elif name == "edit_image":
        refs = a.get("images") or ["last"]
        images, missing = image_gen.load_images(refs if isinstance(refs, list) else [str(refs)], owner=None)
        if not images:
            return [TextContent(type="text", text="Error: no encontré la imagen a editar " + ", ".join(missing))]
        res = await image_gen.run(a.get("prompt", ""), images=images, model_spec=a.get("model", ""),
                                  size=a.get("size", ""))
    else:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]
    if "error" in res:
        return [TextContent(type="text", text="Error: " + res["error"])]
    return [TextContent(type="text", text=res["results"])]


async def run():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(run())
