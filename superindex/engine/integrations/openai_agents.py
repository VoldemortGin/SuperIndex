"""OpenAI Agents SDK adapter for the Agent(tools=...) slot.

The tool set reaches the framework as an in-process MCP server over the
local store, and the FunctionTools are the framework's own conversion of
it: the schema goes to the model
verbatim, and tool results reach it in the framework's shapes (text as
text, images as images). The SDK carries MCP types and renders nothing.
"""
from __future__ import annotations

import asyncio

from ..errors import SuperIndexAPIError, _superindex_cause


def _tool_failure(ctx, error):
    """The framework's tool-failure formatter, narrowed: a SuperIndex failure
    a tool re-raised escapes the run instead of becoming model-visible text;
    anything else keeps the framework default."""
    from agents.tool import default_tool_error_function
    if _superindex_cause(error) is not None:
        raise error
    return default_tool_error_function(ctx, error)


def build_mcp_server(client, include_management: bool = False, doc_ids=None):
    """The tool set as an in-process MCP server for the Agents SDK."""
    from agents.mcp import MCPServer
    from mcp import types as mcp_types
    from ..agent_tools import _tool_specs

    specs = _tool_specs(client, include_management, doc_ids)

    class _ToolServer(MCPServer):
        def __init__(self):
            super().__init__(failure_error_function=_tool_failure)
            self.tools = [mcp_types.Tool(name=name, description=description,
                                         inputSchema=schema)
                          for name, description, schema, _ in specs]
            self._invoke = {name: invoke for name, _, _, invoke in specs}

        @property
        def name(self) -> str:
            return "pageindex"

        async def connect(self):
            pass

        async def cleanup(self):
            pass

        async def list_tools(self, run_context=None, agent=None):
            return self.tools

        async def call_tool(self, tool_name: str, arguments, meta=None):
            blocks, is_error = await asyncio.to_thread(
                self._invoke[tool_name], arguments or {})
            return mcp_types.CallToolResult.model_validate(
                {"content": blocks, "isError": is_error})

        async def list_prompts(self):
            return mcp_types.ListPromptsResult(prompts=[])

        async def get_prompt(self, name: str, arguments=None):
            raise ValueError(f"No prompt named {name!r}")

    return _ToolServer()


def build_openai_tools(client, include_management: bool = False,
                       doc_ids=None) -> list:
    try:
        from agents.mcp import MCPUtil
    except ImportError as exc:
        raise SuperIndexAPIError(
            "as_openai_tools requires the OpenAI Agents SDK — "
            "pip install openai-agents."
        ) from exc
    server = build_mcp_server(client, include_management, doc_ids)
    return [MCPUtil.to_function_tool(tool, server, False)
            for tool in server.tools]
