from app.tools.packages.base import ToolPackage
from app.tools.packages.calculator import PACKAGE as calculator_package
from app.tools.packages.datetime import PACKAGE as datetime_package
from app.tools.packages.filesystem import PACKAGE as filesystem_package
from app.tools.packages.memory import PACKAGE as memory_package
from app.tools.packages.session import PACKAGE as session_package
from app.tools.packages.shell import PACKAGE as shell_package
from app.tools.packages.skills import PACKAGE as skills_package
from app.tools.packages.web import PACKAGE as web_package

BUILTIN_PACKAGES: list[ToolPackage] = [
    filesystem_package,
    shell_package,
    session_package,
    skills_package,
    memory_package,
    calculator_package,
    datetime_package,
    web_package,
]

__all__ = ["BUILTIN_PACKAGES", "ToolPackage"]
