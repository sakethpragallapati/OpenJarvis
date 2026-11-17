"""Agentic desktop assistant: Windows control tools, vision, memory, MCP.

``jarvis listen`` drives this through the ``desktop`` agent:

- :mod:`.server` exposes every PC tool as a FastMCP server (also usable by
  other MCP clients via ``jarvis desktop mcp``), with external MCP servers
  mounted next to them.
- :mod:`.graph` is the LangGraph agent loop that plans and calls the tools.
- :mod:`.fastpath` answers simple commands ("open Task Manager", "volume to
  30") without the language model, which is most of a voice turn's latency.
- :mod:`.memory` keeps long-term facts and learned places in SQLite.
"""
