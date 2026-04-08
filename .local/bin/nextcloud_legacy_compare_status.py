#!/usr/bin/env python3
"""
Nextcloud Legacy Compare Status Summary
Inline Rich dashboard with collapsible checkpoints.
"""
import json
import pathlib
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.tree import Tree

STATE_DIR = pathlib.Path("/Users/adrian/Projects/LifeRadar/.local/state/nextcloud-legacy-compare")
STATUS_PATH = STATE_DIR / "status.json"
MANIFEST_PATH = STATE_DIR / "manifest.json"
DONE_PATH = STATE_DIR / "done.json"
FAILURE_PATH = STATE_DIR / "failed.json"
REPORTS_DIR = STATE_DIR / "reports" / "tasks"
ERR_PATH = pathlib.Path("/Users/adrian/Library/Logs/nextcloud-legacy-compare.err")

console = Console()


def load_json(path: pathlib.Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def get_state_color(state: str) -> str:
    mapping = {
        "waiting": "yellow",
        "running": "blue",
        "retrying": "yellow",
        "completed": "green",
        "failed": "red",
    }
    return mapping.get(state, "white")


def last_errors(limit: int = 5) -> list[str]:
    if not ERR_PATH.exists():
        return []
    lines = [line.rstrip() for line in ERR_PATH.read_text().splitlines() if line.strip()]
    return lines[-limit:]


def render_progress_bar(percent: int, width: int = 36) -> str:
    filled = int((percent / 100) * width)
    empty = width - filled
    return f"[green]{'█' * filled}[/green][grey37]{'░' * empty}[/grey37]"


def make_status_panel(manifest: dict, status: dict, done: dict, failed: dict) -> Panel:
    tasks = manifest.get("tasks", [])
    total_tasks = len(tasks)
    completed_ids = set(status.get("completed_task_ids", []))
    if done and total_tasks and len(completed_ids) < total_tasks:
        completed_ids = {task["id"] for task in tasks}
    completed = len(completed_ids)
    remaining = max(total_tasks - completed, 0)
    percent = int((completed / total_tasks) * 100) if total_tasks else 0
    state = failed.get("state") or done.get("state") or status.get("state") or "unknown"
    consecutive_errors = status.get("consecutive_errors", failed.get("consecutive_errors", 0)) or 0
    last_error = status.get("last_error") or failed.get("last_error")

    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column("Label", style="cyan", ratio=1)
    table.add_column("Value", style="white", ratio=3)

    table.add_row("State", f"[bold {get_state_color(state)}]{state.upper()}[/bold {get_state_color(state)}]")
    table.add_row("Progress", f"{render_progress_bar(percent)} {percent}%")
    table.add_row("Completed", f"{completed}/{total_tasks}")
    table.add_row("Remaining", str(remaining))
    table.add_row("Consecutive Errors", str(consecutive_errors))

    if last_error:
        table.add_row("Last Error", f"[red]{last_error}[/red]")

    return Panel(table, title="[bold]Nextcloud Legacy Compare[/bold]", expand=False)


def make_current_task_panel(status: dict, done: dict, failed: dict) -> Panel:
    state = failed.get("state") or done.get("state") or status.get("state") or "unknown"
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column("Label", style="cyan", ratio=1)
    table.add_column("Value", style="white", ratio=3)

    if status.get("current_task"):
        task = status["current_task"]
        task_name = task.get("name") or "<root files>"
        table.add_row("Folder", task.get("label", "Unknown"))
        table.add_row("Task", task_name)
    elif state == "waiting":
        reason = status.get("reason", "unspecified")
        table.add_row("Waiting Reason", f"[yellow]{reason}[/yellow]")
    else:
        table.add_row("Status", f"[dim]{state}[/dim]")

    return Panel(table, title="[bold]Current Task[/bold]", expand=False)


def make_statistics_panel(manifest: dict, status: dict, done: dict) -> Panel:
    tasks = manifest.get("tasks", [])
    completed_ids = set(status.get("completed_task_ids", []))
    if done and tasks and len(completed_ids) < len(tasks):
        completed_ids = {task["id"] for task in tasks}

    # Build tree with collapsible entries
    tree = Tree("📁 Checkpoints")

    # Group by label (folder)
    by_label: dict[str, list[dict]] = {}
    for task in tasks:
        label = task.get("label", "Unknown")
        if label not in by_label:
            by_label[label] = []
        by_label[label].append(task)

    for label in sorted(by_label.keys()):
        task_list = by_label[label]
        completed_in_label = sum(1 for t in task_list if t["id"] in completed_ids)
        total_in_label = len(task_list)
        percent = int((completed_in_label / total_in_label) * 100) if total_in_label else 0

        # Determine icon based on progress
        if completed_in_label == total_in_label:
            icon = "✅"
            branch = tree.add(f"[green]{icon} {label} ({completed_in_label}/{total_in_label})[/green]")
        elif completed_in_label > 0:
            icon = "🔄"
            branch = tree.add(f"[cyan]{icon} {label} ({completed_in_label}/{total_in_label})[/cyan]")
        else:
            icon = "⏳"
            branch = tree.add(f"[dim]{icon} {label} (0/{total_in_label})[/dim]")

        # Add individual tasks as expandable entries
        for task in sorted(task_list, key=lambda t: t["id"]):
            task_id = task["id"]
            if task_id in completed_ids:
                branch.add(f"[green]  ✓ {task.get('name', task_id.split('__', 1)[1])}[/green]")
            else:
                branch.add(f"[dim]  ○ {task.get('name', task_id.split('__', 1)[1])}[/dim]")

    # Summary footer
    total_completed = len(completed_ids)
    total_tasks = len(tasks)
    overall_percent = int((total_completed / total_tasks) * 100) if total_tasks else 0
    tree.add(f"\n[bold]Progress:[/bold] {total_completed}/{total_tasks} ({overall_percent}%)")

    if REPORTS_DIR.exists():
        written_meta = len(list(REPORTS_DIR.glob("*/*/meta.json")))
        tree.add(f"[bold]Reports:[/bold] {written_meta} written")

    return Panel(tree, title="[bold]📋 Checkpoints[/bold]", expand=False)


def make_errors_panel(recent: list[str]) -> Panel:
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column("Error", style="red")

    if recent:
        for line in recent:
            if len(line) > 100:
                line = line[:97] + "..."
            table.add_row(line)
    else:
        table.add_row("[dim]No recent errors[/dim]")

    return Panel(table, title="[bold]Recent Errors[/bold]", expand=False)


def make_summary_panel(done: dict, failed: dict) -> Panel:
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column("Info", style="cyan", ratio=1)
    table.add_column("Value", style="white", ratio=3)

    if done:
        summary_path = done.get("summary_path", "No path recorded")
        table.add_row("Summary Report", f"[green]{summary_path}[/green]")
    elif failed:
        table.add_row("Status", "[red]Stopped after repeated errors[/red]")
    else:
        table.add_row("Status", "[yellow]In Progress[/yellow]")

    return Panel(table, title="[bold]Summary[/bold]", expand=False)


def main() -> int:
    manifest = load_json(MANIFEST_PATH)
    status = load_json(STATUS_PATH)
    done = load_json(DONE_PATH)
    failed = load_json(FAILURE_PATH)

    panels = [
        make_status_panel(manifest, status, done, failed),
        make_current_task_panel(status, done, failed),
        make_statistics_panel(manifest, status, done),
        make_errors_panel(last_errors()),
        make_summary_panel(done, failed),
    ]

    for panel in panels:
        console.print(panel)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
