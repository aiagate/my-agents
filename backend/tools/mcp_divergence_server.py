from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

mcp = FastMCP("divergence", instructions="External associative divergence provider.")

_ASSOCIATIONS: dict[str, tuple[str, ...]] = {
    "海": ("波", "潮風", "水平線", "砂浜", "夕凪"),
    "森": ("木漏れ日", "苔", "鳥の声", "湿った土", "静けさ"),
    "夜": ("月", "窓明かり", "静寂", "星", "眠り"),
    "仕事": ("締切", "同僚", "集中", "休憩", "達成感"),
    "音楽": ("旋律", "リズム", "余韻", "ヘッドホン", "ライブ"),
}
_FALLBACK = ("余白", "気配", "連なり", "リズム", "距離")


@mcp.tool(
    name="diverge_associations",
    description=(
        "Expand a seed word into a small set of associative angles. "
        "This tool is hosted by an external FastMCP server."
    ),
)
def diverge_associations(
    seed: Annotated[str, Field(description="Seed word or short phrase.")],
    context: Annotated[str, Field(description="Optional surrounding context.")] = "",
    limit: Annotated[
        int, Field(description="Maximum associations to return.", ge=1, le=8)
    ] = 3,
) -> dict[str, object]:
    key = seed.strip().lower()
    source = _ASSOCIATIONS.get(key, _FALLBACK)
    rotated = source if not context else source[1:] + source[:1]
    return {
        "provider": "external-fastmcp",
        "seed": seed,
        "context": context,
        "associations": list(rotated[:limit]),
    }


if __name__ == "__main__":
    mcp.run(transport="stdio", show_banner=False)
