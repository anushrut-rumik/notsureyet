"""Enable with AGENT_TOOL_MODULES=examples.my_tools in .env."""


def register_tools(registry):
    @registry.tool(evidence=True)
    async def word_count(ctx, filename: str):
        """Count the words in a file inside workspace/."""
        root = ctx.engine.config.workspace.resolve()
        path = (root / filename).resolve()
        if not path.is_relative_to(root):
            raise ValueError("Use a file inside workspace/")
        return {"filename": filename, "words": len(path.read_text().split())}
