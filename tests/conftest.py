import os
import tempfile

# Import-time ledger creation must not touch the real runs/ directory.
os.environ.setdefault("MAEF_DB", os.path.join(tempfile.mkdtemp(prefix="maef-test-"), "maef.db"))


def copy_research_analyst(dest):
    """Copy the shipped research-analyst without its MCP server (needs a container runtime to run)."""
    import shutil

    from maef_api.registry import AgentRegistry
    shutil.copytree(AgentRegistry().root / 'research-analyst', dest / 'research-analyst')
    path = dest / 'research-analyst' / 'agent.yaml'
    text = path.read_text().replace('mcp_servers: ["fetch@0.1.0"]', 'mcp_servers: []')
    path.write_text(text.replace('permissions:\n  network_open: true\n', ''))
