"""MCP server exposing the Pack-Brain tools (OpenWebUI via an MCP bridge, Hermes-Agent, Claude Code ...).

  python -m packbrain.mcp_server            stdio transport
  python -m packbrain.mcp_server --http     streamable HTTP transport
The tools are the same functions the in-game assistant uses; no LLM is involved here.
"""
import sys

from mcp.server.mcpserver import MCPServer

from . import brain

mcp = MCPServer("pack-brain", instructions=(
    "Knowledge base of a modded Minecraft 1.21.1 (NeoForge) modpack: items, recipes, guides, wiki texts, TACZ guns. "
    "Use find_item first to get item ids. Data is English."))


@mcp.tool()
def find_item(query: str) -> str:
    """Find items by (English) name or id."""
    return brain.run_tool("find_item", {"query": query})


@mcp.tool()
def item_info(item_id: str) -> str:
    """Mod, recipes, usages and docs for one item id, e.g. create:brass_ingot."""
    return brain.run_tool("item_info", {"item_id": item_id})


@mcp.tool()
def crafting_tree(item_id: str, depth: int = 3) -> str:
    """Multi-step recipe tree for an item."""
    return brain.run_tool("crafting_tree", {"item_id": item_id, "depth": depth})


@mcp.tool()
def search_docs(query: str, mod: str = "") -> str:
    """Keyword search over guides, Patchouli, tooltips, wikis (English keywords)."""
    return brain.run_tool("search_docs", {"query": query, "mod": mod or None})


@mcp.tool()
def read_doc(doc_id: int) -> str:
    """Full text of a doc returned by search_docs."""
    return brain.run_tool("read_doc", {"doc_id": doc_id})


@mcp.tool()
def gun_info(query: str) -> str:
    """TACZ gun stats by name or id."""
    return brain.run_tool("gun_info", {"query": query})


@mcp.tool()
def gun_rankings() -> str:
    """Rankings over all TACZ guns (fire rate, damage, magazine size)."""
    return brain.run_tool("gun_rankings", {})


@mcp.tool()
def rank_items(category: str, stat: str = "attack_damage") -> str:
    """Rank items by base stat: category sword/axe/pickaxe/helmet/.../armor/food, stat attack_damage/dps/armor/durability/..."""
    return brain.run_tool("rank_items", {"category": category, "stat": stat})


@mcp.tool()
def armor_sets(mod: str = "") -> str:
    """All armor sets in the pack with the effort to obtain the chestplate."""
    return brain.run_tool("armor_sets", {"mod": mod or None})


@mcp.tool()
def compare_items(item_ids: list[str]) -> str:
    """Compare 2-5 items: mod, stats (if exported), effort to obtain, description."""
    return brain.run_tool("compare_items", {"item_ids": item_ids})


@mcp.tool()
def where_found(item_id: str) -> str:
    """Mob drops, chest loot, block drops and ore generation for an item."""
    return brain.run_tool("where_found", {"item_id": item_id})


@mcp.tool()
def drops_of(name: str) -> str:
    """What a mob or block drops (English name)."""
    return brain.run_tool("drops_of", {"name": name})


@mcp.tool()
def mob_info(name: str) -> str:
    """Mob stats, drops and spawning by English name."""
    return brain.run_tool("mob_info", {"name": name})


@mcp.tool()
def web_search(query: str) -> str:
    """Fallback web search through SearXNG (untrusted external content)."""
    return brain.run_tool("web_search", {"query": query})


@mcp.tool()
def search_mods(query: str) -> str:
    """Which installed mods match a topic."""
    return brain.run_tool("search_mods", {"query": query})


if __name__ == "__main__":
    mcp.run("streamable-http" if "--http" in sys.argv else "stdio")
