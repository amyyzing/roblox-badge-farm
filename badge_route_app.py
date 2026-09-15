"""Small Windows controller for the direct Roblox badge route."""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

import direct_badge_route as route


ROOT = Path(__file__).resolve().parent


class BadgeRouteApp:
    def __init__(self, window: tk.Tk) -> None:
        self.window = window
        self.window.title("Roblox Badge Route")
        self.window.geometry("700x500")
        self.window.minsize(620, 420)
        self.worker: subprocess.Popen[str] | None = None
        self.output_queue: queue.Queue[str] = queue.Queue()
        self.stop_requested = False

        self.games_var = tk.StringVar(value=str(ROOT / "games.txt"))
        self.state_var = tk.StringVar(value=str(ROOT / "badge-route-state.json"))
        self.badge_output_var = tk.StringVar(value=str(ROOT / "game-badges.txt"))
        self.user_id_var = tk.StringVar()
        self.seconds_var = tk.StringVar(value="10")
        self.startup_var = tk.StringVar(value="5")
        self.launch_var = tk.BooleanVar(value=True)
        self.badge_check_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="Idle")
        self.progress_var = tk.StringVar(value="0 / 0 completed")
        self.badges_var = tk.StringVar(value="0 badge games")
        self.failed_var = tk.StringVar(value="0 failed")
        self.current_var = tk.StringVar(value="No destination running")

        self._build_ui()
        self.window.protocol("WM_DELETE_WINDOW", self._close)
        self._refresh()

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.window, padding=12)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(7, weight=1)

        ttk.Label(outer, text="Games file").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=3)
        ttk.Entry(outer, textvariable=self.games_var).grid(row=0, column=1, sticky="ew", pady=3)
        ttk.Label(outer, text="User ID").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=3)
        ttk.Entry(outer, textvariable=self.user_id_var).grid(row=1, column=1, sticky="ew", pady=3)
        ttk.Label(outer, text="Badge output").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=3)
        ttk.Entry(outer, textvariable=self.badge_output_var).grid(row=2, column=1, sticky="ew", pady=3)

        settings = ttk.Frame(outer)
        settings.grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 2))
        ttk.Label(settings, text="Stay (seconds)").pack(side="left")
        ttk.Entry(settings, textvariable=self.seconds_var, width=7).pack(side="left", padx=(6, 16))
        ttk.Label(settings, text="Startup (seconds)").pack(side="left")
        ttk.Entry(settings, textvariable=self.startup_var, width=7).pack(side="left", padx=(6, 16))
        ttk.Checkbutton(settings, text="Open Roblox", variable=self.launch_var).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(settings, text="Check badges", variable=self.badge_check_var).pack(side="left")

        controls = ttk.Frame(outer)
        controls.grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 8))
        self.start_button = ttk.Button(controls, text="Start", command=self._start)
        self.start_button.pack(side="left", padx=(0, 6))
        self.pause_button = ttk.Button(controls, text="Pause", command=lambda: self._command("pause"))
        self.pause_button.pack(side="left", padx=6)
        self.resume_button = ttk.Button(controls, text="Resume", command=lambda: self._command("resume"))
        self.resume_button.pack(side="left", padx=6)
        self.stop_button = ttk.Button(controls, text="Stop", command=self._stop)
        self.stop_button.pack(side="left", padx=6)
        ttk.Button(controls, text="Reset progress", command=self._reset_progress).pack(side="left", padx=(16, 0))
        ttk.Button(controls, text="Clear badge list", command=self._clear_badges).pack(side="left", padx=6)

        summary = ttk.Frame(outer)
        summary.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        summary.columnconfigure(0, weight=1)
        ttk.Label(summary, textvariable=self.status_var).grid(row=0, column=0, sticky="w")
        ttk.Label(summary, textvariable=self.current_var).grid(row=1, column=0, sticky="w")
        stats = ttk.Frame(summary)
        stats.grid(row=2, column=0, sticky="w", pady=(4, 0))
        ttk.Label(stats, textvariable=self.progress_var).pack(side="left", padx=(0, 16))
        ttk.Label(stats, textvariable=self.badges_var).pack(side="left", padx=(0, 16))
        ttk.Label(stats, textvariable=self.failed_var).pack(side="left")

        self.progress = ttk.Progressbar(outer, mode="determinate")
        self.progress.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(0, 8))

        log_frame = ttk.Frame(outer)
        log_frame.grid(row=7, column=0, columnspan=2, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log = tk.Text(log_frame, height=12, state="disabled", wrap="none")
        self.log.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=scrollbar.set)

    def _is_running(self) -> bool:
        return self.worker is not None and self.worker.poll() is None

    def _write_control(self, command: str) -> None:
        route.write_json_file(ROOT / ".badge-route-ui-control.json", {"command": command})

    def _start(self) -> None:
        if self._is_running():
            return
        try:
            user_id = int(self.user_id_var.get().strip()) if self.badge_check_var.get() else None
            seconds = float(self.seconds_var.get())
            startup_seconds = float(self.startup_var.get())
            if self.badge_check_var.get() and (user_id is None or user_id <= 0):
                raise ValueError("enter a positive Roblox user ID")
            if seconds < 0 or startup_seconds < 0:
                raise ValueError("timers must be non-negative")
        except ValueError as exc:
            messagebox.showerror("Invalid settings", str(exc))
            return

        state_path = Path(self.state_var.get()).expanduser()
        games_path = Path(self.games_var.get()).expanduser()
        badge_output_path = Path(self.badge_output_var.get()).expanduser()
        status_path = ROOT / ".badge-route-ui-status.json"
        control_path = ROOT / ".badge-route-ui-control.json"
        command = [
            sys.executable,
            "-u",
            str(ROOT / "direct_badge_route.py"),
            "--games",
            str(games_path),
            "--state",
            str(state_path),
            "--badge-output",
            str(badge_output_path),
            "--seconds",
            str(seconds),
            "--startup-seconds",
            str(startup_seconds),
            "--status-file",
            str(status_path),
            "--control-file",
            str(control_path),
        ]
        if self.launch_var.get():
            command.append("--launch")
        if self.badge_check_var.get():
            command.extend(["--user-id", str(user_id)])
        else:
            command.append("--no-badge-check")

        try:
            self._write_control("resume")
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            self.worker = subprocess.Popen(
                command,
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                creationflags=creation_flags,
            )
            self.stop_requested = False
            threading.Thread(target=self._read_output, daemon=True).start()
            self._append_log("Started route worker")
        except OSError as exc:
            self.worker = None
            messagebox.showerror("Could not start", str(exc))

    def _read_output(self) -> None:
        if self.worker is None or self.worker.stdout is None:
            return
        for line in self.worker.stdout:
            self.output_queue.put(line.rstrip())

    def _command(self, command: str) -> None:
        if self._is_running():
            self._write_control(command)
            self._append_log(command.capitalize() + " requested")

    def _stop(self) -> None:
        if not self._is_running():
            return
        self.stop_requested = True
        self._write_control("stop")
        self._append_log("Stop requested")
        self.window.after(5000, self._force_stop_if_needed)

    def _force_stop_if_needed(self) -> None:
        if self._is_running() and self.stop_requested:
            self.worker.terminate()
            self._append_log("Worker terminated after it did not stop")

    def _reset_progress(self) -> None:
        if self._is_running():
            messagebox.showinfo("Route running", "Stop the route before resetting progress.")
            return
        path = Path(self.state_var.get()).expanduser()
        try:
            path.unlink(missing_ok=True)
            self._append_log("Progress reset")
        except OSError as exc:
            messagebox.showerror("Reset failed", str(exc))

    def _clear_badges(self) -> None:
        if self._is_running():
            messagebox.showinfo("Route running", "Stop the route before clearing the badge list.")
            return
        path = Path(self.badge_output_var.get()).expanduser()
        try:
            route.write_universe_ids(path, [])
            self._append_log("Badge output cleared")
        except OSError as exc:
            messagebox.showerror("Clear failed", str(exc))

    def _append_log(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", message + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _refresh(self) -> None:
        while True:
            try:
                self._append_log(self.output_queue.get_nowait())
            except queue.Empty:
                break

        status_path = ROOT / ".badge-route-ui-status.json"
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
            self.status_var.set(str(status.get("status", "Idle")).capitalize())
            total = int(status.get("total", 0))
            completed = int(status.get("completed", 0))
            self.progress.configure(maximum=max(1, total), value=completed)
            self.progress_var.set(f"{completed} / {total} completed")
            self.badges_var.set(f"{int(status.get('badges', 0))} badge games")
            self.failed_var.set(f"{int(status.get('failed', 0))} failed")
            current = status.get("current_name") or status.get("current_universe")
            self.current_var.set(f"Current: {current}" if current else str(status.get("message") or "No destination running"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            self.current_var.set("No status yet")

        running = self._is_running()
        self.start_button.configure(state="disabled" if running else "normal")
        for button in (self.pause_button, self.resume_button, self.stop_button):
            button.configure(state="normal" if running else "disabled")
        if self.worker is not None and not running:
            code = self.worker.returncode
            if code:
                self.status_var.set("Worker error")
                self.current_var.set("Worker stopped before completing; see the log")
            else:
                self.status_var.set("Stopped")
            self._append_log(f"Route worker exited ({code})")
            self.worker = None
        self.window.after(500, self._refresh)

    def _close(self) -> None:
        if self._is_running() and not messagebox.askyesno("Route is running", "Stop the route and close the controller?"):
            return
        if self._is_running():
            self._write_control("stop")
        self.window.destroy()


def main() -> None:
    window = tk.Tk()
    BadgeRouteApp(window)
    window.mainloop()


if __name__ == "__main__":
    main()
