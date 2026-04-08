#!/usr/bin/env python3
"""
Nextcloud Legacy Compare - Textual TUI
Interactive terminal dashboard with collapsible checkpoints.
"""
import json
import time
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Container, VerticalScroll
from textual.reactive import reactive
from textual.widgets import Header, Footer, Static, ProgressBar, Tree


STATE_DIR = Path("/Users/adrian/Projects/LifeRadar/.local/state/nextcloud-legacy-compare")
STATUS_PATH = STATE_DIR / "status.json"
MANIFEST_PATH = STATE_DIR / "manifest.json"
DONE_PATH = STATE_DIR / "done.json"
FAILURE_PATH = STATE_DIR / "failed.json"
REPORTS_DIR = STATE_DIR / "reports" / "tasks"
ERR_PATH = Path("/Users/adrian/Library/Logs/nextcloud-legacy-compare.err")


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def last_errors(limit: int = 5) -> list[str]:
    if not ERR_PATH.exists():
        return []
    lines = [line.rstrip() for line in ERR_PATH.read_text().splitlines() if line.strip()]
    return lines[-limit:]


def get_state_info(state: str) -> tuple[str, str]:
    """Return (emoji, color) for state."""
    mapping = {
        "waiting": ("⏸", "yellow"),
        "running": ("🔄", "cyan"),
        "retrying": ("⚡", "magenta"),
        "completed": ("✅", "green"),
        "failed": ("❌", "red"),
    }
    return mapping.get(state, ("❓", "white"))


class StatusWidget(Static):
    state = reactive("unknown")
    errors = reactive(0)

    def watch_state(self, state: str) -> None:
        emoji, color = get_state_info(state)
        self.update(f"[bold {color}]{emoji} {state.upper()}[/bold {color}]")

    def watch_errors(self, errors: int) -> None:
        if errors > 0:
            self.update(f"{self.state}  ⚠ {errors} errors")


class ProgressWidget(Static):
    completed = reactive(0)
    total = reactive(0)

    def render(self) -> str:
        if self.total == 0:
            return "[dim]No tasks loaded[/dim]"
        pct = int((self.completed / self.total) * 100) if self.total else 0
        return f"[cyan]▓[/cyan]" * (pct // 5) + "[dim]░[/dim]" * (20 - pct // 5) + f" {pct}% ({self.completed}/{self.total})"


class CheckpointTree(Tree):
    def __init__(self):
        super().__init__("📋 Checkpoints")

    def build_tree(self, manifest: dict, completed_ids: set) -> None:
        self.clear()
        tasks = manifest.get("tasks", [])

        # Group by label
        by_label: dict[str, list[dict]] = {}
        for task in tasks:
            label = task.get("label", "Unknown")
            if label not in by_label:
                by_label[label] = []
            by_label[label].append(task)

        # Build tree
        root = self.root
        for label in sorted(by_label.keys()):
            task_list = by_label[label]
            done = sum(1 for t in task_list if t["id"] in completed_ids)
            total = len(task_list)
            pct = int((done / total) * 100) if total else 0

            # Folder node
            if done == total:
                icon = "✅"
                folder_node = root.add(f"[green]{icon} {label} ({done}/{total})[/green]")
            elif done > 0:
                icon = "🔄"
                folder_node = root.add(f"[cyan]{icon} {label} ({done}/{total})[/cyan]")
            else:
                icon = "⏳"
                folder_node = root.add(f"[dim]{icon} {label} (0/{total})[/dim]")

            # Task nodes
            for task in sorted(task_list, key=lambda t: t["id"]):
                name = task.get("name") or task["id"].split("__", 1)[1]
                if task["id"] in completed_ids:
                    folder_node.add(f"[green]✓ {name}[/green]")
                else:
                    folder_node.add(f"[dim]○ {name}[/dim]")

        # Summary
        total_done = len(completed_ids)
        total_tasks = len(tasks)
        pct = int((total_done / total_tasks) * 100) if total_tasks else 0
        self.root.add(f"\n[bold]Progress:[/bold] {total_done}/{total_tasks} ({pct}%)")


class ErrorWidget(Static):
    errors = reactive([])

    def render(self) -> str:
        if not self.errors:
            return "[green]✅ No errors[/green]"
        lines = []
        for err in self.errors[-3:]:
            if len(err) > 50:
                err = err[:47] + "..."
            err = err.replace("Traceback", "⛔")
            lines.append(f"[red]•[/red] {err}")
        return "\n".join(lines) if lines else "[green]✅ No errors[/green]"


class NextcloudTUI(App):
    CSS = """
    Screen {
        background: $surface;
    }

    #main {
        height: 100%;
        layout: grid;
        grid-size: 2 3;
        grid-columns: 1fr 1fr;
        grid-rows: 1fr 1fr 1fr;
        padding: 1;
    }

    .card {
        background: $panel;
        border: solid $border;
        padding: 1;
        height: 100%;
    }

    #status-card { background: $panel-darken-1; }
    #progress-card { background: $panel-darken-2; }
    #checkpoints-card { background: $panel-darken-1; }
    #current-card { background: $panel-darken-2; }
    #errors-card { background: $panel-darken-3; }
    #eta-card { background: $panel-darken-1; }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh"),
        ("w", "watch", "Watch"),
    ]

    watching = reactive(False)

    def __init__(self):
        super().__init__()
        self._completed = 0
        self._total = 0
        self._start_time = 0.0
        self.checkpoint_tree = CheckpointTree()

    def compose(self) -> ComposeResult:
        yield Header()

        with Container(id="main"):
            # Row 1
            with VerticalScroll(id="status-card", classes="card"):
                yield Static("[bold]STATUS[/bold]")
                yield StatusWidget(id="status-widget")

            with VerticalScroll(id="progress-card", classes="card"):
                yield Static("[bold]PROGRESS[/bold]")
                yield ProgressWidget(id="progress-widget")

            # Row 2
            with VerticalScroll(id="checkpoints-card", classes="card"):
                yield Static("[bold]CHECKPOINTS[/bold]")
                yield self.checkpoint_tree

            with VerticalScroll(id="current-card", classes="card"):
                yield Static("[bold]CURRENT[/bold]")
                yield Static("[dim]Idle[/dim]", id="current-task")

            # Row 3
            with VerticalScroll(id="errors-card", classes="card"):
                yield Static("[bold]ERRORS[/bold]")
                yield ErrorWidget(id="error-widget")

            with VerticalScroll(id="eta-card", classes="card"):
                yield Static("[bold]ETA[/bold]")
                yield Static("[dim]Calculating...[/dim]", id="eta-widget")

        yield Footer()

    def on_mount(self) -> None:
        self.refresh_data()
        self.set_timer(10, self._schedule_refresh)

    def _schedule_refresh(self) -> None:
        if self.watching:
            self.refresh_data()
        self.set_timer(10, self._schedule_refresh)

    def refresh_data(self) -> None:
        manifest = load_json(MANIFEST_PATH)
        status = load_json(STATUS_PATH)
        done = load_json(DONE_PATH)
        failed = load_json(FAILURE_PATH)

        tasks = manifest.get("tasks", [])
        self._total = len(tasks)

        completed_ids = set(status.get("completed_task_ids", []))
        if done and self._total and len(completed_ids) < self._total:
            completed_ids = {task["id"] for task in tasks}
        self._completed = len(completed_ids)

        # Update status widget
        state = failed.get("state") or done.get("state") or status.get("state") or "unknown"
        status_widget = self.query_one("#status-widget", StatusWidget)
        status_widget.state = state
        status_widget.errors = status.get("consecutive_errors", 0)

        # Update progress widget
        progress_widget = self.query_one("#progress-widget", ProgressWidget)
        progress_widget.completed = self._completed
        progress_widget.total = self._total

        # Update checkpoint tree
        self.checkpoint_tree.build_tree(manifest, completed_ids)

        # Update current task
        current = self.query_one("#current-task", Static)
        if status.get("current_task"):
            task = status["current_task"]
            label = task.get("label", "Unknown")
            name = task.get("name") or "<root>"
            current.update(f"[cyan]{label}[/cyan] → {name}")
        else:
            current.update(f"[dim]{state}[/dim]")

        # Update errors
        errors_widget = self.query_one("#error-widget", ErrorWidget)
        errors_widget.errors = last_errors()

        # Update ETA
        eta_widget = self.query_one("#eta-widget", Static)
        if self._completed > 0:
            if self._start_time == 0:
                self._start_time = time.time() - 1
            elapsed = time.time() - self._start_time
            rate = self._completed / elapsed if elapsed > 0 else 0
            remaining = self._total - self._completed
            if rate > 0 and remaining > 0:
                eta_sec = remaining / rate
                if eta_sec > 3600:
                    eta = f"{eta_sec / 3600:.1f}h"
                elif eta_sec > 60:
                    eta = f"{eta_sec / 60:.1f}m"
                else:
                    eta = f"{eta_sec:.0f}s"
                eta_widget.update(f"[cyan]{eta}[/cyan] remaining | {rate:.1f}/task")
            else:
                eta_widget.update("[dim]∞ (calculating...)[/dim]")
        else:
            eta_widget.update("[dim]No progress yet[/dim]")

    def action_refresh(self) -> None:
        self.refresh_data()

    def action_watch(self) -> None:
        self.watching = not self.watching
        if self.watching:
            self.refresh_data()


def main():
    app = NextcloudTUI()
    app.run()


if __name__ == "__main__":
    main()