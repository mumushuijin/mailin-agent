from pathlib import Path

from app.tools.card import ToolCard, make_card
from app.tools.packages.base import ToolPackage
from app.tools.packages.shell.config import load_shell_config
from app.tools.packages.shell.descriptions import TOOL_DOCS
from app.tools.packages.shell import handlers as shell_handlers


class ShellPackage(ToolPackage):
    def build_cards(self, config: dict | None = None) -> list[ToolCard]:
        cfg = load_shell_config(config)
        if not cfg.enabled:
            return []

        run_doc = TOOL_DOCS["run_shell"]
        proc_doc = TOOL_DOCS["process"]
        return [
            make_card(
                package="shell",
                name="run_shell",
                handler=shell_handlers.run_shell,
                summary=run_doc.summary,
                description=run_doc.description,
                display_name="Shell 命令",
                display_icon="💻",
                risk_level="moderate",
                requires_confirmation=True,
                sandbox_policy="workspace_only",
            ),
            make_card(
                package="shell",
                name="process",
                handler=shell_handlers.process,
                summary=proc_doc.summary,
                description=proc_doc.description,
                display_name="进程控制",
                display_icon="📟",
                risk_level="moderate",
                sandbox_policy="workspace_only",
            ),
        ]


PACKAGE = ShellPackage(Path(__file__).parent)
