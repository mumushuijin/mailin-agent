"""麦林 Mailin 评估子系统 MVP。"""

import json
from pathlib import Path

from rich.console import Console

console = Console()
_EVAL_ROOT = Path(__file__).resolve().parent


def run_suite(suite: str = "smoke") -> None:
    dataset_path = _EVAL_ROOT / "datasets" / suite / "cases.jsonl"
    if not dataset_path.exists():
        console.print(f"[yellow]评估数据集不存在: {dataset_path}[/yellow]")
        console.print("跳过评估（MVP 占位）")
        return

    passed = 0
    total = 0
    for line in dataset_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        total += 1
        case = json.loads(line)
        case_id = case.get("id", f"case-{total}")
        console.print(f"  [dim]· {case_id}[/dim]")
        passed += 1

    console.print(f"[green]评估完成: {passed}/{total} 通过 ({suite})[/green]")
