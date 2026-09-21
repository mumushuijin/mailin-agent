"""结构化 session allowlist 与旧 auto_approve_patterns 兼容层。"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from app.tools.packages.shell.config import load_shell_config, matches_auto_approve
from app.tools.packages.shell.envelope import CommandEnvelope, parse_command_envelope


@dataclass(frozen=True)
class AllowlistEntry:
    executable: str
    args_prefix: tuple[str, ...] = ()
    allowed_roots: tuple[str, ...] = (".",)
    capabilities: tuple[str, ...] = ()


@dataclass
class SessionAllowlist:
    session_id: str
    entries: list[AllowlistEntry] = field(default_factory=list)


_lock = threading.RLock()
_session_lists: dict[str, SessionAllowlist] = {}


def get_session_allowlist(session_id: str | None) -> SessionAllowlist | None:
    if not session_id:
        return None
    with _lock:
        return _session_lists.get(session_id)


def set_session_allowlist(session_id: str, entries: list[AllowlistEntry]) -> SessionAllowlist:
    with _lock:
        al = SessionAllowlist(session_id=session_id, entries=list(entries))
        _session_lists[session_id] = al
        return al


def clear_session_allowlist(session_id: str) -> None:
    with _lock:
        _session_lists.pop(session_id, None)


def load_structured_policy(config: dict | None = None) -> dict[str, Any]:
    """从 CONFIG 读取 tools.shell.policy；缺省空。"""
    if config is None:
        from app.tools.registry import load_full_config

        config = load_full_config()
    tools = config.get("tools") if isinstance(config, dict) else None
    shell = tools.get("shell") if isinstance(tools, dict) else None
    if not isinstance(shell, dict):
        return {}
    policy = shell.get("policy")
    return policy if isinstance(policy, dict) else {}


def _entry_from_dict(data: dict[str, Any]) -> AllowlistEntry | None:
    exe = str(data.get("executable") or "").strip()
    if not exe:
        return None
    args = data.get("args_prefix") or data.get("args") or ()
    if isinstance(args, str):
        args_t = tuple(args.split())
    else:
        args_t = tuple(str(a) for a in args)
    roots = data.get("allowed_roots") or (".",)
    if isinstance(roots, str):
        roots_t = (roots,)
    else:
        roots_t = tuple(str(r) for r in roots)
    caps = data.get("capabilities") or ()
    if isinstance(caps, str):
        caps_t = (caps,)
    else:
        caps_t = tuple(str(c) for c in caps)
    return AllowlistEntry(
        executable=exe,
        args_prefix=args_t,
        allowed_roots=roots_t,
        capabilities=caps_t,
    )


def global_baseline_entries(config: dict | None = None) -> list[AllowlistEntry]:
    policy = load_structured_policy(config)
    commands = policy.get("commands") or []
    entries: list[AllowlistEntry] = []
    if isinstance(commands, list):
        for item in commands:
            if isinstance(item, dict):
                entry = _entry_from_dict(item)
                if entry:
                    entries.append(entry)
    return entries


def _cwd_allowed(cwd: str | None, roots: tuple[str, ...]) -> bool:
    if not roots:
        return False
    rel = (cwd or ".").replace("\\", "/").strip() or "."
    if rel in roots or "." in roots:
        return True
    for root in roots:
        r = root.replace("\\", "/").rstrip("/")
        if rel == r or rel.startswith(r + "/"):
            return True
    return False


def match_structured_allowlist(
    envelope: CommandEnvelope,
    *,
    session_id: str | None,
    config: dict | None = None,
) -> tuple[bool, str]:
    """返回 (matched, reason)。必须同时匹配 executable、参数前缀、cwd 根与能力子集。"""
    if not envelope.executable or not envelope.parse_ok:
        return False, "命令未可靠解析，不能命中结构化白名单"

    candidates = list(global_baseline_entries(config))
    session_al = get_session_allowlist(session_id)
    if session_al:
        candidates.extend(session_al.entries)

    policy = load_structured_policy(config)
    default_roots = policy.get("allowed_roots")
    if isinstance(default_roots, list) and default_roots:
        baseline_roots = tuple(str(r) for r in default_roots)
    else:
        baseline_roots = (".",)

    for entry in candidates:
        if envelope.executable.lower() != entry.executable.lower():
            # Windows 可执行名可能带 .exe
            exe = envelope.executable.lower()
            want = entry.executable.lower()
            if not (exe == want or exe == want + ".exe" or exe.rstrip(".exe") == want):
                continue
        prefix = entry.args_prefix
        if prefix and tuple(a.lower() for a in envelope.args[: len(prefix)]) != tuple(
            a.lower() for a in prefix
        ):
            continue
        roots = entry.allowed_roots or baseline_roots
        if not _cwd_allowed(envelope.cwd, roots):
            continue
        # 请求的能力必须是白名单允许能力的子集
        allowed_caps = set(entry.capabilities)
        requested = set(envelope.capabilities)
        if requested - allowed_caps:
            continue
        return True, f"命中结构化白名单: {entry.executable}"

    return False, "未命中结构化 session/全局白名单"


def evaluate_shell_auto_approve(
    command: str,
    *,
    session_id: str | None,
    cwd: str | None = None,
    background: bool = False,
    timeout_seconds: float | None = None,
    config: dict | None = None,
) -> tuple[bool, CommandEnvelope, str, dict[str, Any]]:
    """综合结构化白名单与旧 patterns；旧 patterns 不能放宽硬边界。

    返回 (auto_approve, envelope, reason, preview_bits)
    """
    from app.tools.packages.shell.guards import check_command_guard

    envelope = parse_command_envelope(
        command,
        cwd=cwd,
        background=background,
        timeout_seconds=timeout_seconds,
    )
    preview: dict[str, Any] = envelope.to_public_dict()

    hard = check_command_guard(command, safety_mode=True)
    if hard:
        preview["allowlist_matched"] = False
        preview["hard_deny"] = True
        return False, envelope, hard, preview

    # 硬边界能力：即使 pattern 命中也不 auto-approve
    hard_caps = {"sidecar_access", "privilege_escalation", "nested_interpreter"}
    if set(envelope.capabilities) & hard_caps:
        return False, envelope, "命中硬边界能力，拒绝自动批准", preview

    matched, reason = match_structured_allowlist(envelope, session_id=session_id, config=config)
    if matched:
        preview["allowlist_matched"] = True
        preview["allowlist_source"] = "structured"
        return True, envelope, reason, preview

    cfg = load_shell_config(config)
    if matches_auto_approve(command, cfg.auto_approve_patterns):
        # 兼容 fallback：仅降低交互频率，不绕过硬边界（已在上方检查）
        # 仍要求 parse_ok；不可靠解析不得仅靠字符串放行
        if not envelope.parse_ok:
            return False, envelope, "旧 auto_approve 命中但命令解析不可靠，仍需审批", preview
        preview["allowlist_matched"] = True
        preview["allowlist_source"] = "legacy_patterns"
        return True, envelope, "命中兼容 auto_approve_patterns", preview

    preview["allowlist_matched"] = False
    return False, envelope, reason, preview
