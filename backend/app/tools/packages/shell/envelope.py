"""跨平台 Shell command envelope：解析 executable / 参数 / 能力标记。"""

from __future__ import annotations

import re
import shlex
import sys
from dataclasses import dataclass, field
from typing import Any


_META_CHARS = ("|", "&&", "||", ";", "`", "$(", ">", "<", "&")
_PATH_HINT = re.compile(
    r"(?:^|[\s=\"'])("
    r"(?:\./|\.\./|/[^\s\"']+|[A-Za-z]:\\[^\s\"']+|"
    r"[^\s\"']+\.(?:py|js|ts|json|md|txt|yml|yaml|toml|sh|ps1|bat|cmd))"
    r")"
)


@dataclass(frozen=True)
class CommandEnvelope:
    raw: str
    executable: str | None
    args: tuple[str, ...]
    cwd: str | None
    explicit_paths: tuple[str, ...]
    capabilities: tuple[str, ...]
    parse_ok: bool
    platform: str
    notes: tuple[str, ...] = ()
    background: bool = False
    timeout_seconds: float | None = None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "raw": self.raw[:500] + ("…" if len(self.raw) > 500 else ""),
            "executable": self.executable,
            "args": list(self.args[:40]),
            "cwd": self.cwd,
            "explicit_paths": list(self.explicit_paths[:20]),
            "capabilities": list(self.capabilities),
            "parse_ok": self.parse_ok,
            "platform": self.platform,
            "notes": list(self.notes),
            "background": self.background,
            "timeout_seconds": self.timeout_seconds,
            "normalized_command": self.normalized_command(),
        }

    def normalized_command(self) -> str:
        if self.executable is None:
            return self.raw.strip()
        parts = [self.executable, *self.args]
        return " ".join(parts)


def _detect_capabilities(command: str, tokens: list[str]) -> list[str]:
    caps: list[str] = []
    lowered = command.lower()
    for meta in _META_CHARS:
        if meta in command:
            caps.append("shell_metachar")
            break
    if any(t in {"sudo", "su"} for t in tokens):
        caps.append("privilege_escalation")
    if re.search(r"\b(curl|wget|Invoke-WebRequest|iwr)\b", command, re.I):
        caps.append("network")
    if re.search(r"\b(chmod|chown|icacls|takeown)\b", command, re.I):
        caps.append("permission_change")
    if re.search(r"\b(kill|taskkill|pkill)\b", command, re.I):
        caps.append("process_control")
    if ".mailin/snapshots" in lowered or ".mailin\\snapshots" in lowered:
        caps.append("sidecar_access")
    if re.search(r"\b(python|python3|node|ruby|perl|pwsh|powershell|bash|sh|cmd)\b", command, re.I):
        # 嵌套解释器：可能执行任意代码
        if any(x in command for x in (" -c ", " -e ", " -Command ", " /c ")):
            caps.append("nested_interpreter")
    return list(dict.fromkeys(caps))


def _extract_paths(command: str, tokens: list[str]) -> list[str]:
    paths: list[str] = []
    for tok in tokens[1:]:
        if tok.startswith("-"):
            continue
        if "/" in tok or "\\" in tok or tok.startswith(".") or re.search(r"\.\w+$", tok):
            paths.append(tok.strip("\"'"))
    for m in _PATH_HINT.finditer(command):
        paths.append(m.group(1).strip("\"'"))
    return list(dict.fromkeys(p for p in paths if p))


def parse_command_envelope(
    command: str,
    *,
    cwd: str | None = None,
    background: bool = False,
    timeout_seconds: float | None = None,
    platform: str | None = None,
) -> CommandEnvelope:
    raw = (command or "").strip()
    plat = platform or sys.platform
    notes: list[str] = []
    if not raw:
        return CommandEnvelope(
            raw="",
            executable=None,
            args=(),
            cwd=cwd,
            explicit_paths=(),
            capabilities=(),
            parse_ok=False,
            platform=plat,
            notes=("empty_command",),
            background=background,
            timeout_seconds=timeout_seconds,
        )

    parse_ok = True
    tokens: list[str] = []
    try:
        if plat.startswith("win"):
            # Windows: 优先 posix=False；含复杂引号时可能失败
            tokens = shlex.split(raw, posix=False)
        else:
            tokens = shlex.split(raw, posix=True)
    except ValueError:
        parse_ok = False
        notes.append("shlex_failed")
        tokens = raw.split()

    # 含管道/重定向等时，整体作为 shell 脚本，标记不可靠结构化
    if any(m in raw for m in ("|", "&&", "||", ";", "`", "$(", ">", "<")) and not raw.startswith("git "):
        # git 偶尔带参数；其它元字符视为不可靠
        if any(m in raw for m in ("|", "&&", "||", ";", "`", "$(")):
            parse_ok = False
            notes.append("shell_metachar_unreliable")

    executable = tokens[0] if tokens else None
    args = tuple(tokens[1:]) if len(tokens) > 1 else ()
    caps = _detect_capabilities(raw, tokens)
    paths = _extract_paths(raw, tokens)

    return CommandEnvelope(
        raw=raw,
        executable=executable,
        args=args,
        cwd=cwd,
        explicit_paths=tuple(paths),
        capabilities=tuple(caps),
        parse_ok=parse_ok,
        platform=plat,
        notes=tuple(notes),
        background=background,
        timeout_seconds=timeout_seconds,
    )
