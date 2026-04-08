#!/usr/bin/env python3
import configparser
import fnmatch
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import time
from collections import Counter
from typing import Any, Optional


HOME = pathlib.Path.home()
STATE_DIR = pathlib.Path("/Users/adrian/Projects/LifeRadar/.local/state/nextcloud-legacy-compare")
REPORT_DIR = STATE_DIR / "reports"
LOCK_PATH = STATE_DIR / "compare.lock"
DONE_PATH = STATE_DIR / "done.json"
STATUS_PATH = STATE_DIR / "status.json"
FAILURE_STATE_PATH = STATE_DIR / "failed.json"
MANIFEST_PATH = STATE_DIR / "manifest.json"
SUMMARY_PATH = REPORT_DIR / "summary.txt"
ENV_PATH = HOME / ".config" / "nextcloud-legacy-compare.env"
LAUNCH_AGENT_PATH = HOME / "Library" / "LaunchAgents" / "com.user.nextcloud-legacy-compare.plist"
LOG_PATH = HOME / "Library" / "Logs" / "nextcloud-legacy-compare.log"
ERR_PATH = HOME / "Library" / "Logs" / "nextcloud-legacy-compare.err"

IGNORE_PATTERNS = [".DS_Store", ".sync_*.db"]
IGNORE_PREFIXES = [".TagStudio/"]

COMPARISONS = [
    ("Cloud", HOME / "Cloud", "nextcloud-canthat:"),
    ("PbW", HOME / "PbW", "nextcloud-pbw:"),
]

RCLONE_CONNECT_TIMEOUT = "20s"
RCLONE_IO_TIMEOUT = "60s"
RCLONE_RETRIES = "3"
RCLONE_LOW_LEVEL_RETRIES = "10"
RCLONE_COMMAND_TIMEOUT_SECONDS = 15 * 60
MAX_CONSECUTIVE_ERRORS = 5
MIN_BATTERY_PERCENT = 35
REQUIRED_HOSTS = ["cloud.pbw.org", "cloud.canthat.be"]
MAX_TASKS_PER_RUN = 2


def load_env(path: pathlib.Path) -> dict[str, str]:
    data: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key] = value
    return data


def build_rclone_config(env: dict[str, str], path: pathlib.Path) -> None:
    cfg = configparser.ConfigParser()
    cfg["nextcloud-pbw"] = {
        "type": "webdav",
        "url": env["NEXTCLOUD_PBW_URL"],
        "vendor": "nextcloud",
        "user": env["NEXTCLOUD_PBW_USER"],
        "pass": env["NEXTCLOUD_PBW_PASS_OBSCURED"],
    }
    cfg["nextcloud-canthat"] = {
        "type": "webdav",
        "url": env["NEXTCLOUD_CANTHAT_URL"],
        "vendor": "nextcloud",
        "user": env["NEXTCLOUD_CANTHAT_USER"],
        "pass": env["NEXTCLOUD_CANTHAT_PASS_OBSCURED"],
    }
    with path.open("w") as fh:
        cfg.write(fh)


def atomic_write_text(path: pathlib.Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as tmp:
        tmp.write(content)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = pathlib.Path(tmp.name)
    tmp_path.replace(path)


def ignored(path: str) -> bool:
    base = path.split("/")[-1]
    if any(fnmatch.fnmatch(base, pattern) for pattern in IGNORE_PATTERNS):
        return True
    return any(path.startswith(prefix) for prefix in IGNORE_PREFIXES)


def top_groups(paths: list[str], limit: int = 12) -> list[tuple[str, int]]:
    counter = Counter((path.split("/", 1)[0] if "/" in path else path) for path in paths)
    return counter.most_common(limit)


def notify(title: str, message: str) -> None:
    escaped_title = title.replace("\\", "\\\\").replace('"', '\\"')
    escaped_message = message.replace("\\", "\\\\").replace('"', '\\"')
    script = f'display notification "{escaped_message}" with title "{escaped_title}"'
    subprocess.run(["/usr/bin/osascript", "-e", script], check=False)


def unload_launch_agent() -> None:
    if not LAUNCH_AGENT_PATH.exists():
        return
    subprocess.run(["/bin/launchctl", "unload", str(LAUNCH_AGENT_PATH)], check=False)


def append_error(message: str) -> None:
    ERR_PATH.parent.mkdir(parents=True, exist_ok=True)
    with ERR_PATH.open("a") as fh:
        fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")


def write_status(payload: dict[str, Any]) -> None:
    atomic_write_text(STATUS_PATH, json.dumps(payload, indent=2) + "\n")


def load_previous_status() -> dict[str, Any]:
    if not STATUS_PATH.exists():
        return {}
    try:
        return json.loads(STATUS_PATH.read_text())
    except Exception:
        return {}


def battery_state() -> tuple[bool, Optional[int]]:
    try:
        output = subprocess.check_output(["/usr/bin/pmset", "-g", "batt"], text=True, timeout=10)
    except Exception:
        return False, None

    on_ac = "AC Power" in output
    percent = None
    for token in output.replace("\t", " ").split():
        if token.endswith("%;") or token.endswith("%"):
            try:
                percent = int(token.rstrip("%;"))
                break
            except ValueError:
                pass
    return on_ac, percent


def session_is_unlocked() -> bool:
    try:
        console_user = subprocess.check_output(["/usr/bin/stat", "-f", "%Su", "/dev/console"], text=True, timeout=10).strip()
    except Exception:
        return False
    return bool(console_user) and console_user not in {"root", "loginwindow"}


def hosts_reachable() -> tuple[bool, Optional[str]]:
    for host in REQUIRED_HOSTS:
        try:
            with socket.create_connection((host, 443), timeout=5):
                pass
        except OSError as exc:
            return False, f"{host}:443 unreachable ({exc})"
    return True, None


def mark_waiting(reason: str) -> int:
    previous = load_previous_status()
    write_status(
        {
            "state": "waiting",
            "reason": reason,
            "updated_at_epoch": int(time.time()),
            "consecutive_errors": int(previous.get("consecutive_errors", 0)),
            "run_criteria": {
                "requires_console_unlocked": True,
                "requires_ac_power": True,
                "minimum_battery_percent": MIN_BATTERY_PERCENT,
                "requires_https_to_hosts": REQUIRED_HOSTS,
            },
            "end_of_life": {
                "success_unloads_agent": True,
                "failure_after_consecutive_errors": MAX_CONSECUTIVE_ERRORS,
            },
        }
    )
    return 0


def record_error(message: str) -> int:
    previous = load_previous_status()
    consecutive_errors = int(previous.get("consecutive_errors", 0)) + 1
    completed_ids = previous.get("completed_task_ids", [])

    append_error(f"Compare failed: {message}")
    notify("Nextcloud Compare Error", message[:220])

    state_payload = {
        "state": "retrying",
        "last_error": message,
        "updated_at_epoch": int(time.time()),
        "consecutive_errors": consecutive_errors,
        "completed_task_ids": completed_ids,
        "run_criteria": {
            "requires_console_unlocked": True,
            "requires_ac_power": True,
            "minimum_battery_percent": MIN_BATTERY_PERCENT,
            "requires_https_to_hosts": REQUIRED_HOSTS,
        },
        "end_of_life": {
            "success_unloads_agent": True,
            "failure_after_consecutive_errors": MAX_CONSECUTIVE_ERRORS,
        },
    }

    if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
        state_payload["state"] = "failed"
        state_payload["failed_at_epoch"] = int(time.time())
        atomic_write_text(FAILURE_STATE_PATH, json.dumps(state_payload, indent=2) + "\n")
        write_status(state_payload)
        notify("Nextcloud Compare Stopped", f"Stopped after {consecutive_errors} consecutive errors. See {ERR_PATH}.")
        unload_launch_agent()
        return 0

    write_status(state_payload)
    return 75


def run_rclone(rclone_bin: str, cfg_path: pathlib.Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            rclone_bin,
            "--config",
            str(cfg_path),
            "--contimeout",
            RCLONE_CONNECT_TIMEOUT,
            "--timeout",
            RCLONE_IO_TIMEOUT,
            "--retries",
            RCLONE_RETRIES,
            "--low-level-retries",
            RCLONE_LOW_LEVEL_RETRIES,
            *args,
        ],
        text=True,
        capture_output=True,
        timeout=RCLONE_COMMAND_TIMEOUT_SECONDS,
    )


def lsf_lines(rclone_bin: str, cfg_path: pathlib.Path, remote: str, *, recursive: bool, files_only: bool, dirs_only: bool) -> list[str]:
    args = ["lsf", remote]
    if recursive:
        args.append("-R")
    if files_only:
        args.append("--files-only")
    if dirs_only:
        args.append("--dirs-only")
    result = run_rclone(rclone_bin, cfg_path, args)
    if result.returncode != 0:
        stderr = (result.stderr or "").lower()
        if "directory not found" in stderr or "object not found" in stderr or "not found" in stderr:
            return []
        raise subprocess.CalledProcessError(result.returncode, result.args, output=result.stdout, stderr=result.stderr)
    return sorted(line.strip().rstrip("/") for line in result.stdout.splitlines() if line.strip())


def compare_local(local_root: pathlib.Path) -> list[str]:
    return sorted(str(path.relative_to(local_root)) for path in local_root.rglob("*") if path.is_file())


def local_top_level_children(local_root: pathlib.Path) -> tuple[set[str], set[str]]:
    dirs: set[str] = set()
    files: set[str] = set()
    if not local_root.exists():
        return dirs, files
    for child in local_root.iterdir():
        if child.is_dir():
            dirs.add(child.name)
        elif child.is_file():
            files.add(child.name)
    return dirs, files


def remote_top_level_children(rclone_bin: str, cfg_path: pathlib.Path, remote: str) -> tuple[set[str], set[str]]:
    dirs = set(lsf_lines(rclone_bin, cfg_path, remote, recursive=False, files_only=False, dirs_only=True))
    files = set(lsf_lines(rclone_bin, cfg_path, remote, recursive=False, files_only=True, dirs_only=False))
    return dirs, files


def build_manifest(rclone_bin: str, cfg_path: pathlib.Path) -> dict[str, Any]:
    tasks: list[dict[str, str]] = []
    for label, local_root, remote in COMPARISONS:
        local_dirs, local_files = local_top_level_children(local_root)
        remote_dirs, remote_files = remote_top_level_children(rclone_bin, cfg_path, remote)
        if local_files or remote_files:
            tasks.append(
                {
                    "id": f"{label}__ROOT_FILES",
                    "label": label,
                    "remote": remote,
                    "local_root": str(local_root),
                    "mode": "root_files",
                    "name": "",
                }
            )
        for name in sorted(local_dirs | remote_dirs):
            tasks.append(
                {
                    "id": f"{label}__{name}",
                    "label": label,
                    "remote": remote,
                    "local_root": str(local_root),
                    "mode": "dir",
                    "name": name,
                }
            )
    return {"tasks": tasks}


def task_report_dir(task: dict[str, str]) -> pathlib.Path:
    return REPORT_DIR / "tasks" / task["label"] / task["id"]


def write_task_report(task: dict[str, str], payload: dict[str, Any]) -> None:
    report_dir = task_report_dir(task)
    report_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(report_dir / "meta.json", json.dumps(payload, indent=2) + "\n")
    atomic_write_text(report_dir / "local_only_filtered.txt", "\n".join(payload["local_only_filtered"]) + ("\n" if payload["local_only_filtered"] else ""))
    atomic_write_text(report_dir / "remote_only_filtered.txt", "\n".join(payload["remote_only_filtered"]) + ("\n" if payload["remote_only_filtered"] else ""))
    atomic_write_text(report_dir / "local_all.txt", "\n".join(payload["local_files"]) + ("\n" if payload["local_files"] else ""))
    atomic_write_text(report_dir / "remote_all.txt", "\n".join(payload["remote_files"]) + ("\n" if payload["remote_files"] else ""))


def process_task(rclone_bin: str, cfg_path: pathlib.Path, task: dict[str, str]) -> dict[str, Any]:
    local_root = pathlib.Path(task["local_root"])
    remote = task["remote"]
    name = task["name"]
    if task["mode"] == "root_files":
        local_files = sorted(p.name for p in local_root.iterdir() if p.is_file()) if local_root.exists() else []
        remote_files = lsf_lines(rclone_bin, cfg_path, remote, recursive=False, files_only=True, dirs_only=False)
    else:
        local_subdir = local_root / name
        local_files = sorted(str(path.relative_to(local_subdir)) for path in local_subdir.rglob("*") if path.is_file()) if local_subdir.exists() else []
        remote_target = f"{remote}{name}"
        remote_files = lsf_lines(rclone_bin, cfg_path, remote_target, recursive=True, files_only=True, dirs_only=False)

    local_set = set(local_files)
    remote_set = set(remote_files)
    local_only = sorted(local_set - remote_set)
    remote_only = sorted(remote_set - local_set)
    local_only_filtered = sorted(path for path in local_only if not ignored(path))
    remote_only_filtered = sorted(path for path in remote_only if not ignored(path))

    payload = {
        "task": task,
        "local_files": local_files,
        "remote_files": remote_files,
        "local_only_raw_count": len(local_only),
        "remote_only_raw_count": len(remote_only),
        "local_only_filtered": local_only_filtered,
        "remote_only_filtered": remote_only_filtered,
    }
    write_task_report(task, payload)
    return payload


def aggregate_reports(manifest: dict[str, Any]) -> tuple[str, dict[str, dict[str, int]]]:
    lines: list[str] = []
    done_payload: dict[str, dict[str, int]] = {}
    for label, local_root, remote in COMPARISONS:
        all_local_files: set[str] = set()
        all_remote_files: set[str] = set()
        all_local_only_filtered: set[str] = set()
        all_remote_only_filtered: set[str] = set()

        task_ids = [task["id"] for task in manifest["tasks"] if task["label"] == label]
        for task_id in task_ids:
            meta_path = REPORT_DIR / "tasks" / label / task_id / "meta.json"
            if not meta_path.exists():
                continue
            payload = json.loads(meta_path.read_text())
            all_local_files.update(payload["local_files"])
            all_remote_files.update(payload["remote_files"])
            all_local_only_filtered.update(payload["local_only_filtered"])
            all_remote_only_filtered.update(payload["remote_only_filtered"])

        local_files = sorted(all_local_files)
        remote_files = sorted(all_remote_files)
        local_only_filtered = sorted(all_local_only_filtered)
        remote_only_filtered = sorted(all_remote_only_filtered)

        atomic_write_text(REPORT_DIR / f"{label.lower()}_local_only_filtered.txt", "\n".join(local_only_filtered) + ("\n" if local_only_filtered else ""))
        atomic_write_text(REPORT_DIR / f"{label.lower()}_remote_only_filtered.txt", "\n".join(remote_only_filtered) + ("\n" if remote_only_filtered else ""))
        atomic_write_text(REPORT_DIR / f"{label.lower()}_local_all.txt", "\n".join(local_files) + ("\n" if local_files else ""))
        atomic_write_text(REPORT_DIR / f"{label.lower()}_remote_all.txt", "\n".join(remote_files) + ("\n" if remote_files else ""))

        lines.append(f"=== {label} ===")
        lines.append(f"Local root: {local_root}")
        lines.append(f"Remote: {remote}")
        lines.append(f"Local files: {len(local_files)}")
        lines.append(f"Remote files: {len(remote_files)}")
        lines.append(f"Local-only filtered: {len(local_only_filtered)}")
        lines.append(f"Remote-only filtered: {len(remote_only_filtered)}")
        lines.append("Top local-only groups:")
        for name, count in top_groups(local_only_filtered):
            lines.append(f"  {name}: {count}")
        lines.append("Top remote-only groups:")
        for name, count in top_groups(remote_only_filtered):
            lines.append(f"  {name}: {count}")
        lines.append("Sample local-only filtered:")
        for path in local_only_filtered[:30]:
            lines.append(f"  {path}")
        lines.append("Sample remote-only filtered:")
        for path in remote_only_filtered[:30]:
            lines.append(f"  {path}")
        lines.append("")

        done_payload[label] = {
            "local_files": len(local_files),
            "remote_files": len(remote_files),
            "local_only_filtered": len(local_only_filtered),
            "remote_only_filtered": len(remote_only_filtered),
        }

    summary = "\n".join(lines) + "\n"
    atomic_write_text(SUMMARY_PATH, summary)
    return summary, done_payload


def main() -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    if DONE_PATH.exists() and SUMMARY_PATH.exists():
        return 0
    if DONE_PATH.exists() and not SUMMARY_PATH.exists():
        append_error("done.json existed without summary.txt; removing stale done marker and retrying")
        DONE_PATH.unlink(missing_ok=True)
    if FAILURE_STATE_PATH.exists():
        return 0

    lock_fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        import fcntl

        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 0

    env = load_env(ENV_PATH)
    rclone_bin = env.get("RCLONE_BIN", "/Users/adrian/.go/bin/rclone")
    on_ac, battery_percent = battery_state()
    if not on_ac:
        return mark_waiting("Mac is not on AC power")
    if battery_percent is not None and battery_percent < MIN_BATTERY_PERCENT:
        return mark_waiting(f"Battery below threshold ({battery_percent}% < {MIN_BATTERY_PERCENT}%)")
    if not session_is_unlocked():
        return mark_waiting("Console session is locked or not owned by the user")
    network_ok, network_reason = hosts_reachable()
    if not network_ok:
        return mark_waiting(network_reason or "Required Nextcloud hosts are unreachable")

    previous = load_previous_status()
    completed = set(previous.get("completed_task_ids", []))

    write_status(
        {
            "state": "running",
            "started_at_epoch": int(time.time()),
            "report_dir": str(REPORT_DIR),
            "consecutive_errors": 0,
            "completed_task_ids": sorted(completed),
            "pending_task_count": 0,
            "run_criteria": {
                "requires_console_unlocked": True,
                "requires_ac_power": True,
                "minimum_battery_percent": MIN_BATTERY_PERCENT,
                "requires_https_to_hosts": REQUIRED_HOSTS,
            },
            "end_of_life": {
                "success_unloads_agent": True,
                "failure_after_consecutive_errors": MAX_CONSECUTIVE_ERRORS,
            },
        }
    )

    try:
        with tempfile.TemporaryDirectory(prefix="nextcloud-legacy-compare-") as temp_dir:
            cfg_path = pathlib.Path(temp_dir) / "rclone.conf"
            build_rclone_config(env, cfg_path)
            if not MANIFEST_PATH.exists():
                manifest = build_manifest(rclone_bin, cfg_path)
                atomic_write_text(MANIFEST_PATH, json.dumps(manifest, indent=2) + "\n")
            else:
                manifest = json.loads(MANIFEST_PATH.read_text())

            previous = load_previous_status()
            completed = set(previous.get("completed_task_ids", []))
            pending_tasks = [task for task in manifest["tasks"] if task["id"] not in completed]

            if pending_tasks:
                for task in pending_tasks[:MAX_TASKS_PER_RUN]:
                    write_status(
                        {
                            "state": "running",
                            "started_at_epoch": int(time.time()),
                            "report_dir": str(REPORT_DIR),
                            "consecutive_errors": 0,
                            "current_task": task,
                            "completed_task_ids": sorted(completed),
                            "pending_task_count": len([t for t in manifest["tasks"] if t["id"] not in completed]),
                            "run_criteria": {
                                "requires_console_unlocked": True,
                                "requires_ac_power": True,
                                "minimum_battery_percent": MIN_BATTERY_PERCENT,
                                "requires_https_to_hosts": REQUIRED_HOSTS,
                            },
                            "end_of_life": {
                                "success_unloads_agent": True,
                                "failure_after_consecutive_errors": MAX_CONSECUTIVE_ERRORS,
                                "partial_progress_persists_between_runs": True,
                            },
                        }
                    )
                    process_task(rclone_bin, cfg_path, task)
                    completed.add(task["id"])
                    write_status(
                        {
                            "state": "running",
                            "started_at_epoch": int(time.time()),
                            "report_dir": str(REPORT_DIR),
                            "consecutive_errors": 0,
                            "completed_task_ids": sorted(completed),
                            "pending_task_count": len([t for t in manifest["tasks"] if t["id"] not in completed]),
                            "run_criteria": {
                                "requires_console_unlocked": True,
                                "requires_ac_power": True,
                                "minimum_battery_percent": MIN_BATTERY_PERCENT,
                                "requires_https_to_hosts": REQUIRED_HOSTS,
                            },
                            "end_of_life": {
                                "success_unloads_agent": True,
                                "failure_after_consecutive_errors": MAX_CONSECUTIVE_ERRORS,
                                "partial_progress_persists_between_runs": True,
                            },
                        }
                    )

            remaining = [task for task in manifest["tasks"] if task["id"] not in completed]
            if remaining:
                return 0

            _, done_payload = aggregate_reports(manifest)

            atomic_write_text(
                DONE_PATH,
                json.dumps(
                    {
                        "completed_at_epoch": int(time.time()),
                        "summary_path": str(SUMMARY_PATH),
                        "report_dir": str(REPORT_DIR),
                        "results": done_payload,
                    },
                    indent=2,
                )
                + "\n",
            )
            FAILURE_STATE_PATH.unlink(missing_ok=True)
            write_status(
                {
                    "state": "completed",
                    "completed_at_epoch": int(time.time()),
                    "summary_path": str(SUMMARY_PATH),
                    "report_dir": str(REPORT_DIR),
                    "consecutive_errors": 0,
                    "completed_task_ids": sorted(completed),
                    "pending_task_count": 0,
                    "run_criteria": {
                        "requires_console_unlocked": True,
                        "requires_ac_power": True,
                        "minimum_battery_percent": MIN_BATTERY_PERCENT,
                        "requires_https_to_hosts": REQUIRED_HOSTS,
                    },
                    "end_of_life": {
                        "success_unloads_agent": True,
                        "failure_after_consecutive_errors": MAX_CONSECUTIVE_ERRORS,
                        "partial_progress_persists_between_runs": True,
                    },
                }
            )
    except subprocess.TimeoutExpired as exc:
        return record_error(f"Timed out after {exc.timeout}s while running remote comparison")
    except subprocess.CalledProcessError as exc:
        message = exc.output.strip() if exc.output else str(exc)
        return record_error(message)
    except Exception as exc:
        return record_error(f"Unexpected failure: {exc}")
    finally:
        os.close(lock_fd)

    notify("Nextcloud Compare Finished", f"Legacy folder comparison is ready in {REPORT_DIR}")
    unload_launch_agent()
    return 0


if __name__ == "__main__":
    sys.exit(main())
