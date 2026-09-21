from __future__ import annotations

import fnmatch
import json
import re
import shutil
from pathlib import Path

from app.context.budget import estimate_tokens
from app.context.tool_cache import is_tool_results_path
from app.core.settings import get_settings
from app.storage.workspace import assert_not_agent_space, _resolve_safe_path, normalize_workspace_path
from app.tools.packages.filesystem.snapshots import (
    SnapshotStore,
    atomic_write_text,
    is_snapshot_sidecar_path,
    load_snapshot_config,
    restore_from_snapshot,
)
from app.tools.runtime import get_project_workspace, tool_session_id

_MAX_LIST_ENTRIES = 200
_MAX_SEARCH_RESULTS = 30
_MAX_READ_BYTES = 512_000
# 普通文件无显式分页时的单次读取 token 上限（超出则自动分页）
_DEFAULT_READ_MAX_TOKENS = 6_000
# 落盘工具结果(tool_results/)的强制单次上限：必须 < context.tool_result_max_tokens(4000)，
# 以保证回取结果永远不会再触发 Layer A 二次落盘（杜绝“套娃”）。
_TOOL_RESULTS_READ_MAX_TOKENS = 3_000


def _workspace_root() -> Path:
    root = get_project_workspace()
    if root is None:
        raise ValueError("未绑定项目工作区")
    return root


def _safe_path(relative: str, *, for_write: bool = False) -> Path:
    rel = normalize_workspace_path(relative, for_write=for_write)
    path = _resolve_safe_path(_workspace_root(), rel)
    assert_not_agent_space(path)
    return path


def _rel_path(path: Path) -> str:
    return str(path.resolve().relative_to(_workspace_root().resolve()))


def _skip_sidecar(path: Path) -> bool:
    try:
        return is_snapshot_sidecar_path(path, _workspace_root())
    except Exception:
        return False


def _require_snapshot_or_error(store: SnapshotStore, meta_factory) -> tuple[object | None, str | None]:
    """创建 pre-change snapshot；失败返回错误字符串。"""
    cfg = load_snapshot_config()
    if not cfg.enabled:
        return None, None
    try:
        meta = meta_factory()
        return meta, None
    except Exception as exc:
        return None, f"快照创建失败，已拒绝变更: {exc}"


def _format_mutation_result(message: str, snapshot_id: str | None) -> str:
    if not snapshot_id:
        return message
    return f"{message}\n[snapshot_id={snapshot_id}] [undo=undo_file_change]"


def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    """按字符二分截断到 token 预算内（用于无换行的超长单行/blob）。"""
    if estimate_tokens(text) <= max_tokens:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if estimate_tokens(text[:mid]) <= max_tokens:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo]


def _window_by_lines(
    text: str, offset: int, limit: int | None, max_tokens: int
) -> tuple[str, int, int, int, bool]:
    """按行返回窗口并强制 token 上限。

    offset 为 1 起始行号，limit 为行数（None/<=0 表示到文件末尾）。
    返回 (window, total_lines, start_line, end_line, truncated)。
    """
    lines = text.split("\n")
    total = len(lines)
    start = max(0, (offset or 1) - 1)
    if start >= total:
        return "", total, total, total, False

    hard_end = total if not limit or limit <= 0 else min(total, start + limit)
    out_lines: list[str] = []
    acc = 0
    truncated = False
    for ln in lines[start:hard_end]:
        cost = estimate_tokens(ln) + 1  # +1 近似换行
        if out_lines and acc + cost > max_tokens:
            truncated = True
            break
        out_lines.append(ln)
        acc += cost

    window = "\n".join(out_lines)
    # 单行即超限（无换行 blob）：硬截断字符
    if estimate_tokens(window) > max_tokens:
        window = _truncate_to_tokens(window, max_tokens)
        truncated = True
    end_line = start + len(out_lines)
    return window, total, start + 1, end_line, truncated


def _prefix_line_numbers(text: str, start_line: int) -> str:
    if text == "":
        return ""
    lines = text.split("\n")
    return "\n".join(f"{start_line + i}|{line}" for i, line in enumerate(lines))


def _format_read_window(
    file_path: str,
    window: str,
    total: int,
    start_line: int,
    end_line: int,
    truncated: bool,
    is_offload: bool,
) -> str:
    numbered = _prefix_line_numbers(window, start_line)
    has_more = end_line < total
    if not has_more and start_line <= 1 and not truncated:
        return numbered
    notes: list[str] = [f"共 {total} 行，本次显示第 {start_line}–{end_line} 行"]
    if truncated:
        notes[0] += "（达到单次 token 上限被截断）"
    if has_more:
        notes.append(f'继续下一段：read_file(file_path="{file_path}", offset={end_line + 1})')
    if is_offload:
        notes.append("这是落盘的工具结果，请按需分页/检索，不要试图一次读完全文")
    return numbered + "\n\n---\n[分页] " + "；".join(notes)


def read_file(file_path: str, offset: int = 1, limit: int | None = None) -> str:
    """读取工作区内的 UTF-8 文本文件，支持按行分页。

    路径必须为相对工作区根目录的路径。文件不存在时返回错误说明，不要猜测内容。

    分页参数：
    - offset：起始行号（1 起始，默认 1）
    - limit：读取行数（默认读到末尾或单次 token 上限为止）

    超长文件会自动分页，并在结尾提示如何继续读取下一段。读取 tool_results/ 下的
    落盘工具结果时会强制单次上限，请按需分页，不要试图一次读完。
    """
    try:
        path = _safe_path(file_path)
    except ValueError as e:
        return str(e)
    if _skip_sidecar(path):
        return "禁止读取 snapshot sidecar"
    if not path.exists():
        return f"文件不存在: {file_path}"
    if path.is_dir():
        return f"路径是目录而非文件: {file_path}"
    if path.stat().st_size > _MAX_READ_BYTES:
        return f"文件过大（>{_MAX_READ_BYTES} 字节），请用 offset/limit 分段读取: {file_path}"
    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return f"无法以 UTF-8 读取（可能是二进制文件）: {file_path}"

    is_offload = is_tool_results_path(file_path)
    # 落盘文件结构为 {"tool_call_id":..., "content": <原始大文本>}，分页只针对真正的 content
    body = raw
    if is_offload:
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            payload = None
        if isinstance(payload, dict) and "content" in payload:
            body = str(payload.get("content") or "")

    explicit_window = (offset and offset > 1) or (limit is not None and limit > 0)
    if is_offload:
        max_tokens = _TOOL_RESULTS_READ_MAX_TOKENS
    else:
        max_tokens = _DEFAULT_READ_MAX_TOKENS
        # 普通小文件且未显式分页：保持整文件返回的旧行为
        if not explicit_window and estimate_tokens(body) <= max_tokens:
            return _prefix_line_numbers(body, 1)

    window, total, start_line, end_line, truncated = _window_by_lines(
        body, offset, limit, max_tokens
    )
    if not window and start_line >= total and total > 0:
        return f"[分页] offset={offset} 超出文件总行数 {total}，无内容可读: {file_path}"
    return _format_read_window(
        file_path, window, total, start_line, end_line, truncated, is_offload
    )


def write_file(file_path: str, content: str) -> str:
    """向工作区内写入文件（覆盖）。

    路径必须为相对路径。HTML/JS/CSS 等产物文件的裸文件名会自动写入 `.mailin/artifacts/`。
    会自动创建父目录。仅允许写入工作区沙箱内。启用快照时先落盘 snapshot，再原子写入。
    """
    try:
        path = _safe_path(file_path, for_write=True)
    except ValueError as e:
        return str(e)
    if _skip_sidecar(path):
        return "禁止写入 snapshot sidecar"

    rel = _rel_path(path) if path.exists() else None
    try:
        store = SnapshotStore()
    except ValueError as e:
        return str(e)

    existed = path.exists() and path.is_file()
    op = "write" if existed else "create"
    # 解析最终相对路径（含 artifacts 重定向）
    target_rel = str(path.resolve().relative_to(_workspace_root().resolve()))

    meta, err = _require_snapshot_or_error(
        store,
        lambda: store.create_pre_change_snapshot(
            operation=op,  # type: ignore[arg-type]
            target=path,
            target_rel=target_rel,
            session_id=tool_session_id.get(),
        ),
    )
    if err:
        return err

    try:
        atomic_write_text(path, content)
    except OSError as e:
        return f"写入失败: {e}"

    snapshot_id = None
    if meta is not None:
        meta = store.finalize_post_fingerprint(meta, path)  # type: ignore[arg-type]
        snapshot_id = meta.snapshot_id
        try:
            store.cleanup()
        except Exception:
            pass

    return _format_mutation_result(f"已写入: {target_rel}", snapshot_id)


def replace_in_file(file_path: str, old_text: str, new_text: str, replace_all: bool = False) -> str:
    """替换工作区文件中的文本片段。

    路径必须为相对路径。默认仅替换首次匹配；replace_all=true 时替换全部匹配。
    未找到 old_text 时返回错误，不会修改文件。启用快照时先 snapshot 再原子写入。
    """
    try:
        path = _safe_path(file_path)
    except ValueError as e:
        return str(e)
    if _skip_sidecar(path):
        return "禁止修改 snapshot sidecar"
    if not path.exists():
        return f"文件不存在: {file_path}"
    if path.is_dir():
        return f"路径是目录而非文件: {file_path}"
    content = path.read_text(encoding="utf-8")
    if old_text not in content:
        return f"未找到要替换的文本: {old_text[:80]}"
    if replace_all:
        updated = content.replace(old_text, new_text)
        count = content.count(old_text)
    else:
        updated = content.replace(old_text, new_text, 1)
        count = 1

    try:
        store = SnapshotStore()
    except ValueError as e:
        return str(e)
    target_rel = _rel_path(path)
    meta, err = _require_snapshot_or_error(
        store,
        lambda: store.create_pre_change_snapshot(
            operation="replace",
            target=path,
            target_rel=target_rel,
            session_id=tool_session_id.get(),
        ),
    )
    if err:
        return err

    try:
        atomic_write_text(path, updated)
    except OSError as e:
        return f"写入失败: {e}"

    snapshot_id = None
    if meta is not None:
        meta = store.finalize_post_fingerprint(meta, path)  # type: ignore[arg-type]
        snapshot_id = meta.snapshot_id
        try:
            store.cleanup()
        except Exception:
            pass

    return _format_mutation_result(f"已编辑: {file_path}（替换 {count} 处）", snapshot_id)


def list_directory(directory_path: str = ".") -> str:
    """列出工作区目录下的文件和子目录。

    directory_path 为相对路径，默认为工作区根目录。返回 [file] / [dir] 标记的条目列表。
    """
    try:
        path = _safe_path(directory_path)
    except ValueError as e:
        return str(e)
    if not path.exists():
        return f"目录不存在: {directory_path}"
    if not path.is_dir():
        return f"不是目录: {directory_path}"

    entries: list[str] = []
    for item in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        if _skip_sidecar(item):
            continue
        # 在 .mailin 下列出时隐藏 snapshots 目录名
        if item.name == "snapshots" and path.name == ".mailin":
            continue
        kind = "dir" if item.is_dir() else "file"
        entries.append(f"[{kind}] {_rel_path(item)}")
        if len(entries) >= _MAX_LIST_ENTRIES:
            entries.append(f"... 仅显示前 {_MAX_LIST_ENTRIES} 项")
            break
    return "\n".join(entries) if entries else "（空目录）"


def search_files(
    query: str,
    directory_path: str = ".",
    file_pattern: str = "*",
    max_results: int = 20,
) -> str:
    """在工作区文件中搜索匹配 query 的文本（正则，默认忽略大小写）。

    query 按 Python 正则编译；非法正则立即失败且不扫描文件。
    支持 file_pattern 过滤文件名（如 *.md、*.py）。仅搜索 UTF-8 文本文件。
    """
    query = query.strip()
    if not query:
        return "搜索关键词不能为空。"

    try:
        pattern = re.compile(query, re.IGNORECASE)
    except re.error as exc:
        return f"无效的正则表达式: {exc}"

    try:
        base = _safe_path(directory_path)
    except ValueError as e:
        return str(e)
    if not base.exists():
        return f"目录不存在: {directory_path}"
    if not base.is_dir():
        return f"不是目录: {directory_path}"

    max_results = min(max(max_results, 1), _MAX_SEARCH_RESULTS)
    hits: list[str] = []

    for path in sorted(base.rglob("*")):
        if _skip_sidecar(path):
            continue
        if not path.is_file():
            continue
        if not fnmatch.fnmatch(path.name, file_pattern):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue

        rel = _rel_path(path)
        line_hits: list[str] = []
        for idx, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                preview = line.strip()
                if len(preview) > 120:
                    preview = preview[:120] + "..."
                line_hits.append(f"  L{idx}: {preview}")
            if len(line_hits) >= 3:
                break
        if not line_hits:
            continue
        hits.append(f"{rel}\n" + "\n".join(line_hits))
        if len(hits) >= max_results:
            break

    if not hits:
        return f"未找到匹配「{query}」的文件（目录: {directory_path}，过滤: {file_pattern}）"
    suffix = f"\n\n（共 {len(hits)} 个匹配文件" + (
        f"，上限 {max_results}）" if len(hits) >= max_results else "）"
    )
    return "\n\n".join(hits) + suffix


def glob_search(glob_pattern: str, directory_path: str = ".") -> str:
    """按 glob 模式搜索工作区内的文件或目录。

    glob_pattern 示例：*.md、**/*.py、src/**/test_*.py。
    directory_path 为搜索起点的相对目录，默认为工作区根目录。
    """
    glob_pattern = glob_pattern.strip()
    if not glob_pattern:
        return "glob 模式不能为空。"

    try:
        base = _safe_path(directory_path)
    except ValueError as e:
        return str(e)
    if not base.exists():
        return f"目录不存在: {directory_path}"
    if not base.is_dir():
        return f"不是目录: {directory_path}"

    matches: list[str] = []
    for path in sorted(base.glob(glob_pattern)):
        if _skip_sidecar(path):
            continue
        if not str(path.resolve()).startswith(str(_workspace_root().resolve())):
            continue
        kind = "dir" if path.is_dir() else "file"
        matches.append(f"[{kind}] {_rel_path(path)}")
        if len(matches) >= _MAX_LIST_ENTRIES:
            matches.append(f"... 仅显示前 {_MAX_LIST_ENTRIES} 项")
            break

    if not matches:
        return f"未匹配到任何路径（模式: {glob_pattern}，目录: {directory_path}）"
    return "\n".join(matches)


def mkdir(directory_path: str, parents: bool = True) -> str:
    """在工作区内创建目录。

    directory_path 为相对路径。裸目录名会在 artifacts/ 下创建（仅当路径无 `/` 且用于产物目录时）。
    parents=true 时会一并创建上级目录。
    """
    try:
        path = _safe_path(directory_path, for_write=True)
    except ValueError as e:
        return str(e)
    path.mkdir(parents=parents, exist_ok=True)
    return f"已创建目录: {_rel_path(path)}"


def move_file(source_path: str, destination_path: str) -> str:
    """移动或重命名工作区内的文件或目录。

    source_path 与 destination_path 均必须为相对路径，且位于工作区沙箱内。
    """
    try:
        src = _safe_path(source_path)
        dst = _safe_path(destination_path)
    except ValueError as e:
        return str(e)
    if _skip_sidecar(src) or _skip_sidecar(dst):
        return "禁止移动 snapshot sidecar 内的路径"
    if not src.exists():
        return f"源路径不存在: {source_path}"

    try:
        store = SnapshotStore()
    except ValueError as e:
        return str(e)

    src_rel = _rel_path(src)
    # destination 可能尚不存在
    try:
        dst_rel = str(dst.resolve().relative_to(_workspace_root().resolve()))
    except ValueError:
        dst_rel = destination_path

    meta, err = _require_snapshot_or_error(
        store,
        lambda: store.create_pre_change_snapshot(
            operation="move",
            target=src if src.is_file() else dst,
            target_rel=src_rel,
            session_id=tool_session_id.get(),
            source=src if src.is_file() else None,
            source_rel=src_rel,
            destination_rel=dst_rel,
        ),
    )
    if err:
        return err

    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(src), str(dst))
    except OSError as e:
        return f"移动失败: {e}"

    snapshot_id = None
    if meta is not None:
        post_path = dst if dst.exists() else None
        meta = store.finalize_post_fingerprint(meta, post_path if post_path and post_path.is_file() else None)  # type: ignore[arg-type]
        snapshot_id = meta.snapshot_id
        try:
            store.cleanup()
        except Exception:
            pass

    return _format_mutation_result(f"已移动: {source_path} -> {destination_path}", snapshot_id)


def delete_file(file_path: str) -> str:
    """删除工作区内的文件。

    路径必须为相对路径。若路径是目录或非空目录，将拒绝删除并返回错误。
    """
    try:
        path = _safe_path(file_path)
    except ValueError as e:
        return str(e)
    if _skip_sidecar(path):
        return "禁止删除 snapshot sidecar"
    if not path.exists():
        return f"文件不存在: {file_path}"
    if path.is_dir():
        if any(path.iterdir()):
            return f"目录非空，无法删除: {file_path}"
        path.rmdir()
        return f"已删除空目录: {file_path}"

    try:
        store = SnapshotStore()
    except ValueError as e:
        return str(e)
    target_rel = _rel_path(path)
    meta, err = _require_snapshot_or_error(
        store,
        lambda: store.create_pre_change_snapshot(
            operation="delete",
            target=path,
            target_rel=target_rel,
            session_id=tool_session_id.get(),
        ),
    )
    if err:
        return err

    try:
        path.unlink()
    except OSError as e:
        return f"删除失败: {e}"

    snapshot_id = None
    if meta is not None:
        meta = store.finalize_post_fingerprint(meta, None)  # type: ignore[arg-type]
        snapshot_id = meta.snapshot_id
        try:
            store.cleanup()
        except Exception:
            pass

    return _format_mutation_result(f"已删除: {file_path}", snapshot_id)


def undo_file_change(snapshot_id: str) -> str:
    """按 snapshot_id 回滚同 session 内的一次文件变更。

    若目标 fingerprint 已变化、快照过期或不属于当前 session，则拒绝且不覆盖新内容。
    """
    snapshot_id = (snapshot_id or "").strip()
    if not snapshot_id:
        return "snapshot_id 不能为空"
    try:
        store = SnapshotStore()
    except ValueError as e:
        return str(e)
    return restore_from_snapshot(store, snapshot_id, session_id=tool_session_id.get())
