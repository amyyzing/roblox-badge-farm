"""Launch a Roblox badge route from Windows instead of chaining TeleportService.

The in-game script can only teleport to a third-party experience when the
current experience allows it.  This small controller starts each destination
from the desktop, so that source-game teleport settings do not strand the
route.  It uses only public Roblox endpoints and never needs an account
cookie.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any, Callable, Iterable


GAMES_API = "https://games.roblox.com/v1/games"
BADGES_API = "https://badges.roblox.com/v1/universes/{universe_id}/badges"
OWNERSHIP_API = (
    "https://inventory.roblox.com/v1/users/{user_id}/items/Badge/{badge_id}/is-owned"
)
DEFAULT_SECONDS = 10.0
DEFAULT_POLL_SECONDS = 1.0
DEFAULT_STARTUP_SECONDS = 5.0
DEFAULT_STATE = "badge-route-state.json"
DEFAULT_BADGE_OUTPUT = "game-badges.txt"
USER_AGENT = "roblox-badge-farm-direct/1.0"


class RouteError(RuntimeError):
    """An expected route or API error."""


class RouteStopped(RouteError):
    """The desktop controller requested a clean stop."""


def write_json_file(path: Path, payload: Any) -> None:
    """Write JSON atomically so the UI never reads a half-written file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def read_control(path: Path | None) -> str | None:
    """Read a controller command, if a GUI supplied one."""

    if path is None or not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    command = payload.get("command") if isinstance(payload, dict) else None
    if not isinstance(command, str):
        return None
    command = command.lower()
    return command if command in {"pause", "resume", "stop"} else None


def write_status(path: Path | None, payload: dict[str, Any]) -> None:
    if path is not None:
        write_json_file(path, payload)


def parse_universe_ids(path: Path, *, allow_empty: bool = False) -> list[str]:
    """Read positive universe IDs, preserving order and removing duplicates."""

    result: list[str] = []
    seen: set[str] = set()
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        value = raw_line.split("#", 1)[0].strip()
        if not value:
            continue
        if not re.fullmatch(r"\d+", value) or int(value) <= 0:
            raise RouteError(f"{path}:{line_number}: expected a positive universe ID")
        value = str(int(value))
        if value not in seen:
            seen.add(value)
            result.append(value)
    if not result and not allow_empty:
        raise RouteError(f"{path} does not contain any universe IDs")
    return result


def write_universe_ids(path: Path, universe_ids: Iterable[str]) -> None:
    """Write a normalized one-universe-ID-per-line list atomically."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    lines = list(dict.fromkeys(str(item) for item in universe_ids))
    temporary.write_text("" if not lines else "\n".join(lines) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _request_json_once(url: str, timeout: float = 20.0) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def get_json(
    url: str,
    *,
    timeout: float = 20.0,
    retries: int = 3,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    """Fetch JSON with short retries for transient Roblox/API failures."""

    last_error: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            return _request_json_once(url, timeout)
        except urllib.error.HTTPError as exc:
            last_error = exc
            if attempt + 1 < retries:
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                try:
                    if retry_after:
                        delay = max(1.0, float(retry_after))
                    elif exc.code == 429:
                        delay = 15.0 * (attempt + 1)
                    else:
                        delay = float(attempt + 1)
                except ValueError:
                    delay = 15.0 * (attempt + 1) if exc.code == 429 else float(attempt + 1)
                sleep(delay)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt + 1 < retries:
                sleep(float(attempt + 1))
    raise RouteError(f"could not read {url}: {last_error}")


def _batches(values: list[str], size: int) -> Iterable[list[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def resolve_destinations(
    universe_ids: list[str],
    *,
    request_json: Callable[[str], Any] = get_json,
    batch_size: int = 50,
    batch_pause: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
    before_batch: Callable[[], None] | None = None,
    on_batch: Callable[[int, int, int], None] | None = None,
) -> dict[str, dict[str, Any]]:
    """Resolve universe IDs to root places and return the API metadata."""

    destinations: dict[str, dict[str, Any]] = {}
    batches = list(_batches(universe_ids, batch_size))
    for batch_number, batch in enumerate(batches):
        if before_batch is not None:
            before_batch()
        if batch_number and batch_pause > 0:
            sleep(batch_pause)
        query = urllib.parse.urlencode({"universeIds": ",".join(batch)})
        payload = request_json(f"{GAMES_API}?{query}")
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise RouteError("Roblox returned an invalid game lookup response")
        for item in payload["data"]:
            if not isinstance(item, dict):
                continue
            universe_id = str(item.get("id", ""))
            root_place_id = item.get("rootPlaceId")
            if universe_id in batch and str(root_place_id or "").isdigit() and int(root_place_id) > 0:
                destinations[universe_id] = {
                    "universe_id": universe_id,
                    "place_id": int(root_place_id),
                    "name": str(item.get("name") or universe_id),
                    "creator": item.get("creator"),
                }
        if on_batch is not None:
            on_batch(batch_number + 1, len(batches), len(destinations))
    return destinations


def list_badge_ids(
    universe_id: str,
    *,
    request_json: Callable[[str], Any] = get_json,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> list[str]:
    """List all badge IDs belonging to a universe."""

    result: list[str] = []
    seen: set[str] = set()
    cursor: str | None = None
    seen_cursors: set[str] = set()
    while True:
        if deadline is not None and monotonic() >= deadline:
            raise RouteError("badge check time budget expired")
        params = {"limit": "100", "sortOrder": "Asc"}
        if cursor:
            params["cursor"] = cursor
        url = BADGES_API.format(universe_id=universe_id) + "?" + urllib.parse.urlencode(params)
        payload = request_json(url)
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise RouteError(f"Roblox returned an invalid badge response for {universe_id}")
        for item in payload["data"]:
            if not isinstance(item, dict):
                continue
            badge_id = item.get("id")
            if str(badge_id or "").isdigit() and str(badge_id) not in seen:
                seen.add(str(badge_id))
                result.append(str(badge_id))
        next_cursor = payload.get("nextPageCursor")
        if not next_cursor or next_cursor in seen_cursors:
            break
        seen_cursors.add(str(next_cursor))
        cursor = str(next_cursor)
    return result


def _owned_value(payload: Any) -> bool:
    if isinstance(payload, bool):
        return payload
    if isinstance(payload, dict):
        return bool(payload.get("isOwned", payload.get("owned", False)))
    return False


class BadgeChecker:
    """Poll public badge ownership while respecting the endpoint's rate limit."""

    def __init__(
        self,
        user_id: int,
        *,
        request_json: Callable[[str], Any] = get_json,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        min_interval: float = 1.0,
    ) -> None:
        self.user_id = int(user_id)
        self.request_json = request_json
        self.sleep = sleep
        self.monotonic = monotonic
        self.min_interval = max(0.0, float(min_interval))
        self._last_request: float | None = None
        self._known_owned: set[str] = set()
        self.timed_out = False

    def _is_owned(self, badge_id: str, *, deadline: float | None = None) -> bool | None:
        now = self.monotonic()
        if deadline is not None and now >= deadline:
            self.timed_out = True
            return None
        if self._last_request is not None:
            delay = self.min_interval - (now - self._last_request)
            if delay > 0:
                if deadline is not None:
                    delay = min(delay, max(0.0, deadline - now))
                self.sleep(delay)
                if deadline is not None and self.monotonic() >= deadline:
                    self.timed_out = True
                    return None
        url = OWNERSHIP_API.format(user_id=self.user_id, badge_id=badge_id)
        payload = self.request_json(url)
        self._last_request = self.monotonic()
        if deadline is not None and self._last_request >= deadline:
            self.timed_out = True
            return None
        owned = _owned_value(payload)
        if owned:
            self._known_owned.add(str(badge_id))
        return owned

    def snapshot(self, badge_ids: Iterable[str], *, deadline: float | None = None) -> set[str]:
        """Return the badges already owned before entering a destination."""

        self.timed_out = False
        baseline: set[str] = set()
        for badge_id in badge_ids:
            owned = self._is_owned(str(badge_id), deadline=deadline)
            if owned is None:
                break
            if owned:
                baseline.add(str(badge_id))
        return baseline

    def find_new(
        self,
        badge_ids: Iterable[str],
        baseline: set[str],
        *,
        deadline: float | None = None,
    ) -> str | None:
        """Return the first badge that changed from unowned to owned."""

        self.timed_out = False
        for badge_id in badge_ids:
            badge_id = str(badge_id)
            if badge_id in baseline or badge_id in self._known_owned:
                continue
            owned = self._is_owned(badge_id, deadline=deadline)
            if owned is None:
                break
            if owned:
                return badge_id
        return None


def build_launch_uri(place_id: int | str) -> str:
    return "roblox://experiences/start?placeId=" + urllib.parse.quote(str(int(place_id)))


def open_place(uri: str) -> None:
    """Open a Roblox protocol URI on Windows, with a browser fallback."""

    if os.name == "nt":
        try:
            os.startfile(uri)  # type: ignore[attr-defined]
            return
        except OSError:
            pass
    if not webbrowser.open(uri):
        raise RouteError(f"Windows could not open {uri}")


def new_state() -> dict[str, Any]:
    return {
        "version": 2,
        "completed_universes": [],
        "failed_universes": {},
        "badge_universes": [],
        "last_universe": None,
    }


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return new_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RouteError(f"cannot read state file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RouteError(f"state file {path} must contain a JSON object")
    completed = data.get("completed_universes", [])
    failed = data.get("failed_universes", {})
    badges = data.get("badge_universes", [])
    if not isinstance(completed, list) or not isinstance(failed, dict) or not isinstance(badges, list):
        raise RouteError(f"state file {path} has an invalid shape")
    normalized = new_state()
    normalized["completed_universes"] = list(dict.fromkeys(str(item) for item in completed if str(item).isdigit()))
    normalized["failed_universes"] = {str(key): str(value) for key, value in failed.items()}
    normalized["badge_universes"] = list(dict.fromkeys(str(item) for item in badges if str(item).isdigit()))
    last = data.get("last_universe")
    normalized["last_universe"] = str(last) if last is not None else None
    return normalized


def save_state(path: Path, state: dict[str, Any]) -> None:
    payload = {
        "version": 2,
        "completed_universes": list(dict.fromkeys(str(item) for item in state.get("completed_universes", []))),
        "failed_universes": {str(key): str(value) for key, value in state.get("failed_universes", {}).items()},
        "badge_universes": list(dict.fromkeys(str(item) for item in state.get("badge_universes", []))),
        "last_universe": state.get("last_universe"),
    }
    write_json_file(path, payload)


def _mark_completed(state: dict[str, Any], universe_id: str) -> None:
    completed = state.setdefault("completed_universes", [])
    if universe_id not in completed:
        completed.append(universe_id)
    state.setdefault("failed_universes", {}).pop(universe_id, None)
    state["last_universe"] = universe_id


def run_route(
    universe_ids: list[str],
    *,
    state: dict[str, Any],
    destinations: dict[str, dict[str, Any]],
    user_id: int | None,
    seconds: float = DEFAULT_SECONDS,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    startup_seconds: float = DEFAULT_STARTUP_SECONDS,
    launch: bool = False,
    badge_check: bool = True,
    request_json: Callable[[str], Any] = get_json,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    launch_fn: Callable[[str], None] = open_place,
    output: Callable[[str], None] = print,
    save_progress: Callable[[dict[str, Any]], None] | None = None,
    save_badges: Callable[[dict[str, Any]], None] | None = None,
    control_file: Path | None = None,
    status_update: Callable[[str, str | None, str | None, str | None], None] | None = None,
    badge_request_json: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Process the route once and return the updated state."""

    if launch and badge_check and user_id is None:
        raise RouteError("--user-id is required with --launch unless --no-badge-check is used")
    if seconds < 0 or poll_seconds <= 0 or startup_seconds < 0:
        raise RouteError("seconds and startup settings must be non-negative; poll-seconds must be positive")

    checker = (
        BadgeChecker(
            user_id,
            request_json=badge_request_json or request_json,
            sleep=sleep,
            monotonic=monotonic,
        )
        if launch and badge_check and user_id is not None
        else None
    )
    completed = set(str(item) for item in state.get("completed_universes", []))

    def emit(status: str, universe_id: str | None = None, title: str | None = None, message: str | None = None) -> None:
        if status_update is not None:
            status_update(status, universe_id, title, message)

    def wait_if_paused(
        deadline: float | None,
        universe_id: str | None,
        title: str | None,
    ) -> float | None:
        paused_at: float | None = None
        while True:
            command = read_control(control_file)
            if command == "stop":
                raise RouteStopped("stopped by controller")
            if command != "pause":
                if paused_at is not None:
                    if deadline is not None:
                        deadline += monotonic() - paused_at
                    emit("running", universe_id, title, "resumed")
                return deadline
            if paused_at is None:
                paused_at = monotonic()
                emit("paused", universe_id, title, "paused by controller")
            sleep(0.2)

    for universe_id in universe_ids:
        wait_if_paused(None, universe_id, None)
        if universe_id in completed:
            output(f"skip {universe_id}: already completed")
            continue
        destination = destinations.get(universe_id)
        if not destination:
            message = "no root place was returned by Roblox"
            state.setdefault("failed_universes", {})[universe_id] = message
            if save_progress is not None:
                save_progress(state)
            output(f"skip {universe_id}: {message}")
            continue

        uri = build_launch_uri(destination["place_id"])
        title = destination.get("name") or universe_id
        wait_if_paused(None, universe_id, title)
        output(f"{universe_id} -> {title} (place {destination['place_id']})")
        emit("launching", universe_id, title, f"place {destination['place_id']}")
        if not launch:
            continue

        try:
            launch_fn(uri)
        except Exception as exc:  # a bad protocol registration should not stop the route
            message = f"launch failed: {exc}"
            state.setdefault("failed_universes", {})[universe_id] = message
            if save_progress is not None:
                save_progress(state)
            output(f"skip {universe_id}: {message}")
            continue

        launch_started = monotonic()
        deadline = launch_started + startup_seconds + seconds
        badge_ids: list[str] = []
        baseline: set[str] = set()
        badge_poll_enabled = checker is not None
        if checker is not None:
            try:
                badge_ids = list_badge_ids(
                    universe_id,
                    request_json=badge_request_json or request_json,
                    deadline=deadline,
                    monotonic=monotonic,
                )
                if badge_ids:
                    baseline = checker.snapshot(badge_ids, deadline=deadline)
                if checker.timed_out:
                    output(f"badge check timed out for {universe_id}; using the timer")
                    badge_poll_enabled = False
            except RouteError as exc:
                output(f"badge check unavailable for {universe_id}: {exc}; using the timer")
                badge_poll_enabled = False

        startup_remaining = launch_started + startup_seconds - monotonic()
        if startup_remaining > 0:
            sleep(startup_remaining)
        emit("waiting", universe_id, title, f"{seconds:g}-second window")
        awarded: str | None = None
        while monotonic() < deadline:
            deadline = wait_if_paused(deadline, universe_id, title)
            if deadline is None:
                break
            if monotonic() >= deadline:
                break
            if badge_poll_enabled and checker is not None and badge_ids:
                try:
                    awarded = checker.find_new(badge_ids, baseline, deadline=deadline)
                except RouteError as exc:
                    output(f"badge check unavailable in {universe_id}: {exc}; using the timer")
                    badge_poll_enabled = False
                if awarded:
                    output(f"badge {awarded} detected in {universe_id}; continuing")
                    badge_universes = state.setdefault("badge_universes", [])
                    if universe_id not in badge_universes:
                        badge_universes.append(universe_id)
                        if save_badges is not None:
                            save_badges(state)
                    break
            remaining = deadline - monotonic()
            if remaining > 0:
                sleep(min(0.2, poll_seconds, remaining))
        if not awarded:
            output(f"{seconds:g}-second window elapsed for {universe_id}; continuing")
        _mark_completed(state, universe_id)
        completed.add(universe_id)
        if save_progress is not None:
            save_progress(state)
        emit("completed", universe_id, title, "badge detected" if awarded else "timer elapsed")
        output(f"completed {universe_id}")

    return state


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--games", type=Path, default=Path("games.txt"), help="universe IDs, one per line")
    parser.add_argument("--badge-output", type=Path, default=Path(DEFAULT_BADGE_OUTPUT), help="output IDs that award a new badge")
    parser.add_argument("--state", type=Path, default=Path(DEFAULT_STATE), help="local progress JSON")
    parser.add_argument("--user-id", type=int, help="Roblox user ID used for public badge ownership checks")
    parser.add_argument("--seconds", type=float, default=DEFAULT_SECONDS, help="seconds to stay after launch")
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS, help="badge polling interval")
    parser.add_argument("--startup-seconds", type=float, default=DEFAULT_STARTUP_SECONDS, help="time for Roblox to load")
    parser.add_argument("--limit", type=int, help="process only the first N uncompleted destinations")
    parser.add_argument("--launch", action="store_true", help="open Roblox places; default is a dry-run")
    parser.add_argument("--dry-run", action="store_true", help="resolve and print places without opening Roblox")
    parser.add_argument("--no-badge-check", action="store_true", help="use the timer without ownership API calls")
    parser.add_argument("--reset", action="store_true", help="delete the saved route state and exit")
    parser.add_argument("--status-file", type=Path, help="JSON status file for the desktop controller")
    parser.add_argument("--control-file", type=Path, help="JSON pause/resume/stop command file")
    return parser


def configure_console() -> None:
    """Keep Windows' legacy console code pages from aborting on game names."""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")


def main(argv: list[str] | None = None) -> int:
    configure_console()
    args = build_parser().parse_args(argv)
    if args.launch and args.dry_run:
        print("error: choose either --launch or --dry-run", file=sys.stderr)
        return 1
    if args.reset:
        try:
            args.state.unlink()
        except FileNotFoundError:
            pass
        print(f"reset {args.state}")
        return 0
    state: dict[str, Any] | None = None
    status_update: Callable[[str, str | None, str | None, str | None], None] | None = None
    try:
        universe_ids = parse_universe_ids(args.games)
        state = load_state(args.state)
        positions = {universe_id: index + 1 for index, universe_id in enumerate(universe_ids)}

        def update_status(
            status: str,
            universe_id: str | None = None,
            title: str | None = None,
            message: str | None = None,
        ) -> None:
            if args.status_file is None or state is None:
                return
            write_status(
                args.status_file,
                {
                    "status": status,
                    "total": len(universe_ids),
                    "completed": len(state.get("completed_universes", [])),
                    "badges": len(state.get("badge_universes", [])),
                    "failed": len(state.get("failed_universes", {})),
                    "current_index": positions.get(universe_id),
                    "current_universe": universe_id,
                    "current_name": title,
                    "message": message,
                    "updated_at": time.time(),
                },
            )

        status_update = update_status
        update_status("resolving", message="resolving game places")

        def wait_for_resolution_control() -> None:
            paused = False
            while True:
                command = read_control(args.control_file)
                if command == "stop":
                    raise RouteStopped("stopped by controller")
                if command != "pause":
                    if paused:
                        update_status("resolving", message="resumed place resolution")
                    return
                if not paused:
                    paused = True
                    update_status("paused", message="paused during place resolution")
                time.sleep(0.2)

        def controlled_sleep(seconds: float) -> None:
            remaining = max(0.0, seconds)
            while remaining > 0:
                wait_for_resolution_control()
                interval = min(0.2, remaining)
                time.sleep(interval)
                remaining -= interval

        def controlled_request(url: str) -> Any:
            return get_json(url, sleep=controlled_sleep)

        def controlled_badge_request(url: str) -> Any:
            # Badge polling must not hold a destination open behind the route
            # timer when Roblox is slow or rate-limits the ownership endpoint.
            return get_json(url, timeout=3.0, retries=1, sleep=controlled_sleep)

        source_ids = set(universe_ids)
        output_badges = (
            [item for item in parse_universe_ids(args.badge_output, allow_empty=True) if item in source_ids]
            if args.badge_output.exists()
            else []
        )
        state_badges = [str(item) for item in state.get("badge_universes", []) if str(item) in source_ids]
        state["badge_universes"] = list(dict.fromkeys(output_badges + state_badges))
        write_universe_ids(args.badge_output, [item for item in universe_ids if item in set(state["badge_universes"])])
        completed = set(str(item) for item in state.get("completed_universes", []))
        pending = [item for item in universe_ids if item not in completed]
        if args.limit is not None:
            if args.limit <= 0:
                raise RouteError("--limit must be positive")
            pending = pending[: args.limit]
        if not pending:
            update_status("finished", message="no uncompleted destinations")
            print("no uncompleted destinations")
            return 0
        destinations = resolve_destinations(
            pending,
            request_json=controlled_request,
            sleep=controlled_sleep,
            before_batch=wait_for_resolution_control,
            on_batch=lambda current, total, found: update_status(
                "resolving",
                message=f"resolved {current}/{total} place batches ({found} destinations)",
            ),
        )
        if not args.launch:
            print("dry-run: no Roblox windows will be opened")
        run_route(
            pending,
            state=state,
            destinations=destinations,
            user_id=args.user_id,
            seconds=args.seconds,
            poll_seconds=args.poll_seconds,
            startup_seconds=args.startup_seconds,
            launch=args.launch,
            badge_check=not args.no_badge_check,
            request_json=controlled_request,
            save_progress=lambda current: save_state(args.state, current),
            save_badges=lambda current: write_universe_ids(
                args.badge_output,
                [item for item in universe_ids if item in set(current.get("badge_universes", []))],
            ),
            control_file=args.control_file,
            status_update=status_update,
            badge_request_json=controlled_badge_request,
        )
        save_state(args.state, state)
        update_status("finished", message="route complete")
        return 0
    except RouteStopped as exc:
        if state is not None:
            save_state(args.state, state)
        if status_update is not None:
            status_update("stopped", message=str(exc))
        print(str(exc))
        return 0
    except (OSError, RouteError, ValueError) as exc:
        if status_update is not None:
            status_update("error", message=str(exc))
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
