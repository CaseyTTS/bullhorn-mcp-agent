"""Dry-run preview rendering for write-capable tools."""


def render_preview(tool: str, would_execute: dict) -> dict:
    """Build the response a dry-run tool call returns instead of executing.

    Args:
        tool: The tool name the preview is for.
        would_execute: A description of the write that would have happened.

    Returns:
        A dict marking this as a dry run, naming the tool, and describing
        what would have been executed.
    """
    return {
        "dry_run": True,
        "tool": tool,
        "would_execute": would_execute,
    }
