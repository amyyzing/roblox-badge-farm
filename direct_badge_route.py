"""Launch a Roblox badge route from Windows instead of chaining TeleportService.

The in-game script can only teleport to a third-party experience when the
current experience allows it.  This small controller starts each destination
from the desktop, so that source-game teleport settings do not strand the
route.  It uses only public Roblox endpoints and never needs an account
cookie.
"""

from __future__ import annotations

import argparse
import email.utils
import json
import math
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
MAX_BADGE_PREP_SECONDS = 2.0
DEFAULT_STATE = "badge-route-state.json"
DEFAULT_BADGE_OUTPUT = "game-badges.txt"
USER_AGENT = "roblox-badge-farm-direct/1.0"
RETRIABLE_HTTP_CODES = {408, 425, 429, 500, 502, 503, 504}


class RouteError(RuntimeError):
    """An expected route or API error."""


class RouteStopped(RouteError):
    """The desktop controller requested a clean stop."""


class RequestCooldown:
    """Share a server cooldown across all route requests."""

    def __init__(self, *, monotonic: Callable[[], float] = time.monotonic) -> None:
        self.monotonic = monotonic
        self.next_allowed = 0.0

    def wait(
        self,
        sleep: Callable[[float], None],
        *,
        deadline: float | None = None,
    ) -> None:
        delay = self.next_allowed - self.monotonic()
        if delay <= 0:
            return
        if deadline is not None:
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                raise RouteError("request time budget expired")
            delay = min(delay, remaining)
        sleep(delay)
        if deadline is not None and self.monotonic() >= deadline:
            raise RouteError("request time budget expired")

    def defer(self, seconds: float) -> None:
        self.next_allowed = max(self.next_allowed, self.monotonic() + max(0.0, seconds))


class RouteLock:
    """Prevent two route workers from launching competing destinations."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.held = False

    def acquire(self) -> None:
        for _ in range(2):
            try:
                descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    raw_pid = self.path.read_text(encoding="ascii").strip()
                    pid = int(raw_pid)
                    if pid > 0:
                        try:
                            os.kill(pid, 0)
                        except ProcessLookupError:
                            pass
                        except PermissionError as exc:
                            raise RouteError("another badge route worker is already running") from exc
                        else:
                            raise RouteError("another badge route worker is already running")
                except (OSError, ValueError):
                    pass
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    continue
                continue
            else:
                with os.fdopen(descriptor, "w", encoding="ascii") as handle:
                    handle.write(f"{os.getpid()}\n")
                self.held = True
                return
        raise RouteError("could not acquire the badge route worker lock")

    def release(self) -> None:
        if not self.held:
            return
        self.held = False
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


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


def validate_paths(paths: dict[str, Path]) -> None:
    """Reject files that would overwrite another route input or control file."""

    resolved: dict[Path, str] = {}
    for label, path in paths.items():
        try:
            key = path.expanduser().resolve(strict=False)
        except OSError as exc:
            raise RouteError(f"cannot resolve {label} path {path}: {exc}") from exc
        previous = resolved.get(key)
        if previous is not None:
            raise RouteError(f"{label} path must differ from {previous} path: {path}")
        resolved[key] = label


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
    cooldown: RequestCooldown | None = None,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> Any:
    """Fetch JSON with bounded retries for transient Roblox/API failures."""

    last_error: Exception | None = None
    for attempt in range(max(1, retries)):
        if deadline is not None and monotonic() >= deadline:
            raise RouteError(f"request time budget expired for {url}")
        if cooldown is not None:
            cooldown.wait(sleep, deadline=deadline)
        try:
            return _request_json_once(url, timeout)
        except urllib.error.HTTPError as exc:
            last_error = exc
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            delay = retry_delay(exc, retry_after)
            if cooldown is not None and exc.code == 429:
                cooldown.defer(delay)
            if exc.code not in RETRIABLE_HTTP_CODES or attempt + 1 >= retries:
                break
            if deadline is not None and monotonic() + delay >= deadline:
                break
            sleep(delay)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            delay = float(attempt + 1)
            if attempt + 1 >= retries:
                break
            if deadline is not None and monotonic() + delay >= deadline:
                break
            sleep(delay)
    raise RouteError(f"could not read {url}: {last_error}")


def retry_delay(exc: urllib.error.HTTPError, retry_after: str | None) -> float:
    """Return a bounded retry/cooldown delay from an HTTP failure."""

    if retry_after:
        try:
            return max(1.0, float(retry_after))
        except ValueError:
            try:
                retry_at = email.utils.parsedate_to_datetime(retry_after).timestamp()
                return max(1.0, retry_at - time.time())
            except (TypeError, ValueError, OverflowError):
                pass
    if exc.code == 429:
        return 15.0
    return 1.0


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
    request_deadline_json: Callable[[str, float], Any] | None = None,
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
        if request_deadline_json is not None and deadline is not None:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise RouteError("badge check time budget expired")
            payload = request_deadline_json(url, remaining)
        else:
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
    if not isinstance(payload, dict):
        raise RouteError("Roblox returned an invalid badge ownership response")
    value = payload.get("isOwned", payload.get("owned"))
    if type(value) is not bool:
        raise RouteError("Roblox returned an invalid badge ownership response")
    return value


class BadgeChecker:
    """Poll public badge ownership while respecting the endpoint's rate limit."""

    def __init__(
        self,
        user_id: int,
        *,
        request_json: Callable[[str], Any] = get_json,
        request_deadline_json: Callable[[str, float], Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        min_interval: float = 1.0,
    ) -> None:
        self.user_id = int(user_id)
        self.request_json = request_json
        self.request_deadline_json = request_deadline_json
        self.sleep = sleep
        self.monotonic = monotonic
        self.min_interval = max(0.0, float(min_interval))
        self._last_request: float | None = None
        self._known_owned: set[str] = set()
        self.timed_out = False
        self._poll_index = 0

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
        if self.request_deadline_json is not None and deadline is not None:
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                self.timed_out = True
                return None
            payload = self.request_deadline_json(url, remaining)
        else:
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
        ids = [str(badge_id) for badge_id in badge_ids]
        if not ids:
            return None
        for offset in range(len(ids)):
            index = (self._poll_index + offset) % len(ids)
            badge_id = ids[index]
            if badge_id in baseline or badge_id in self._known_owned:
                continue
            self._poll_index = (index + 1) % len(ids)
            owned = self._is_owned(badge_id, deadline=deadline)
            if owned is None:
                break
            if owned:
                return badge_id
            break
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


def new_state(user_id: int | None = None) -> dict[str, Any]:
    return {
        "version": 3,
        "user_id": str(user_id) if user_id is not None else None,
        "completed_universes": [],
        "failed_universes": {},
        "badge_universes": [],
        "inconclusive_universes": {},
        "visit_statuses": {},
        "last_universe": None,
    }


def load_state(path: Path, *, expected_user_id: int | None = None) -> dict[str, Any]:
    if not path.exists():
        return new_state(expected_user_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RouteError(f"cannot read state file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RouteError(f"state file {path} must contain a JSON object")
    completed = data.get("completed_universes", [])
    failed = data.get("failed_universes", {})
    badges = data.get("badge_universes", [])
    inconclusive = data.get("inconclusive_universes", {})
    visit_statuses = data.get("visit_statuses", {})
    if (
        not isinstance(completed, list)
        or not isinstance(failed, dict)
        or not isinstance(badges, list)
        or not isinstance(inconclusive, dict)
        or not isinstance(visit_statuses, dict)
    ):
        raise RouteError(f"state file {path} has an invalid shape")
    raw_user_id = data.get("user_id")
    if raw_user_id is not None and (not str(raw_user_id).isdigit() or int(raw_user_id) <= 0):
        raise RouteError(f"state file {path} has an invalid user ID")
    stored_user_id = str(int(raw_user_id)) if raw_user_id is not None else None
    if expected_user_id is not None and stored_user_id is not None and stored_user_id != str(expected_user_id):
        raise RouteError(
            f"state file {path} belongs to Roblox user {stored_user_id}, not {expected_user_id}"
        )
    normalized = new_state(expected_user_id if stored_user_id is None else int(stored_user_id))
    normalized["user_id"] = stored_user_id or normalized["user_id"]
    normalized["completed_universes"] = list(dict.fromkeys(str(item) for item in completed if str(item).isdigit()))
    normalized["failed_universes"] = {str(key): str(value) for key, value in failed.items()}
    normalized["badge_universes"] = list(dict.fromkeys(str(item) for item in badges if str(item).isdigit()))
    normalized["inconclusive_universes"] = {str(key): str(value) for key, value in inconclusive.items()}
    normalized["visit_statuses"] = {str(key): str(value) for key, value in visit_statuses.items()}
    last = data.get("last_universe")
    normalized["last_universe"] = str(last) if last is not None else None
    return normalized


def save_state(path: Path, state: dict[str, Any]) -> None:
    payload = {
        "version": 3,
        "user_id": state.get("user_id"),
        "completed_universes": list(dict.fromkeys(str(item) for item in state.get("completed_universes", []))),
        "failed_universes": {str(key): str(value) for key, value in state.get("failed_universes", {}).items()},
        "badge_universes": list(dict.fromkeys(str(item) for item in state.get("badge_universes", []))),
        "inconclusive_universes": {
            str(key): str(value) for key, value in state.get("inconclusive_universes", {}).items()
        },
        "visit_statuses": {str(key): str(value) for key, value in state.get("visit_statuses", {}).items()},
        "last_universe": state.get("last_universe"),
    }
    write_json_file(path, payload)


def clear_badge_results(state_path: Path, output_path: Path) -> None:
    """Clear the authoritative badge results and regenerate the text export."""

    state = load_state(state_path)
    state["badge_universes"] = []
    save_state(state_path, state)
    write_universe_ids(output_path, [])


def _mark_completed(state: dict[str, Any], universe_id: str) -> None:
    completed = state.setdefault("completed_universes", [])
    if universe_id not in completed:
        completed.append(universe_id)
    state.setdefault("failed_universes", {}).pop(universe_id, None)
    state.setdefault("inconclusive_universes", {}).pop(universe_id, None)
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
    badge_request_deadline_json: Callable[[str, float], Any] | None = None,
) -> dict[str, Any]:
    """Process the route once and return the updated state."""

    if launch and badge_check and user_id is None:
        raise RouteError("--user-id is required with --launch unless --no-badge-check is used")
    if (
        not math.isfinite(seconds)
        or not math.isfinite(poll_seconds)
        or not math.isfinite(startup_seconds)
        or seconds < 0
        or poll_seconds <= 0
        or startup_seconds < 0
    ):
        raise RouteError("timers must be finite; seconds and startup must be non-negative and poll-seconds positive")

    checker = (
        BadgeChecker(
            user_id,
            request_json=badge_request_json or request_json,
            request_deadline_json=badge_request_deadline_json,
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

    def set_visit_status(universe_id: str, status: str, *, reason: str | None = None) -> None:
        state.setdefault("visit_statuses", {})[universe_id] = status
        if reason is None:
            state.setdefault("inconclusive_universes", {}).pop(universe_id, None)
        else:
            state.setdefault("inconclusive_universes", {})[universe_id] = reason
        if save_progress is not None:
            save_progress(state)

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
            set_visit_status(universe_id, "no_root_place", reason=message)
            if save_progress is not None:
                save_progress(state)
            output(f"skip {universe_id}: {message}")
            continue

        uri = build_launch_uri(destination["place_id"])
        title = destination.get("name") or universe_id
        wait_if_paused(None, universe_id, title)
        badge_ids: list[str] = []
        baseline: set[str] = set()
        badge_poll_enabled = checker is not None
        badge_check_reason: str | None = None
        if checker is not None:
            emit("preparing", universe_id, title, "preparing badge baseline")
            prep_window = min(MAX_BADGE_PREP_SECONDS, max(0.5, startup_seconds))
            prep_deadline = monotonic() + prep_window
            try:
                badge_ids = list_badge_ids(
                    universe_id,
                    request_json=badge_request_json or request_json,
                    request_deadline_json=badge_request_deadline_json,
                    deadline=prep_deadline,
                    monotonic=monotonic,
                )
                if badge_ids:
                    baseline = checker.snapshot(badge_ids, deadline=prep_deadline)
                    if checker.timed_out:
                        badge_poll_enabled = False
                        badge_check_reason = "badge baseline timed out"
                else:
                    badge_poll_enabled = False
            except RouteError as exc:
                badge_poll_enabled = False
                badge_check_reason = str(exc)
                output(f"badge baseline unavailable for {universe_id}: {exc}; using the timer")

        output(f"{universe_id} -> {title} (place {destination['place_id']})")
        emit("launching", universe_id, title, f"place {destination['place_id']}")
        if not launch:
            continue

        set_visit_status(universe_id, "launch_requested")
        try:
            launch_fn(uri)
        except Exception as exc:  # a bad protocol registration should not stop the route
            message = f"launch failed: {exc}"
            state.setdefault("failed_universes", {})[universe_id] = message
            set_visit_status(universe_id, "launch_failed", reason=message)
            if save_progress is not None:
                save_progress(state)
            output(f"skip {universe_id}: {message}")
            continue

        launch_started = monotonic()
        deadline = launch_started + startup_seconds + seconds

        startup_remaining = launch_started + startup_seconds - monotonic()
        if startup_remaining > 0:
            sleep(startup_remaining)
        emit("waiting", universe_id, title, f"{seconds:g}-second window")
        awarded: str | None = None
        next_poll_at = monotonic()
        while monotonic() < deadline:
            deadline = wait_if_paused(deadline, universe_id, title)
            if deadline is None:
                break
            now = monotonic()
            if now >= deadline:
                break
            if badge_poll_enabled and checker is not None and badge_ids and now >= next_poll_at:
                try:
                    awarded = checker.find_new(badge_ids, baseline, deadline=deadline)
                except RouteError as exc:
                    output(f"badge check unavailable in {universe_id}: {exc}; using the timer")
                    badge_poll_enabled = False
                    badge_check_reason = str(exc)
                if checker.timed_out:
                    badge_poll_enabled = False
                    badge_check_reason = "badge polling timed out"
                next_poll_at = monotonic() + poll_seconds
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
                control_interval = 0.2
                if badge_poll_enabled and badge_ids:
                    control_interval = min(control_interval, max(0.0, next_poll_at - monotonic()))
                sleep(min(control_interval, remaining))
        if not awarded:
            output(f"{seconds:g}-second window elapsed for {universe_id}; continuing")
        if awarded:
            _mark_completed(state, universe_id)
            completed.add(universe_id)
            set_visit_status(universe_id, "new_badge_observed")
            emit("completed", universe_id, title, "badge detected")
            output(f"completed {universe_id}")
        elif checker is None:
            _mark_completed(state, universe_id)
            completed.add(universe_id)
            set_visit_status(universe_id, "timer_only")
            emit("completed", universe_id, title, "timer elapsed")
            output(f"completed {universe_id}")
        elif not badge_ids and badge_check_reason is None:
            _mark_completed(state, universe_id)
            completed.add(universe_id)
            set_visit_status(universe_id, "no_badges_found")
            emit("completed", universe_id, title, "no badges found")
            output(f"completed {universe_id}")
        elif badge_check_reason is None:
            _mark_completed(state, universe_id)
            completed.add(universe_id)
            set_visit_status(universe_id, "no_new_badge_observed")
            emit("completed", universe_id, title, "no new badge observed")
            output(f"completed {universe_id}")
        else:
            reason = badge_check_reason
            state["last_universe"] = universe_id
            set_visit_status(universe_id, "badge_check_inconclusive", reason=reason)
            emit("inconclusive", universe_id, title, reason)
            output(f"inconclusive {universe_id}: {reason}")

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
    parser.add_argument(
        "--lock-file",
        type=Path,
        default=Path(".badge-route-worker.lock"),
        help="worker lock file used to prevent duplicate route processes",
    )
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
    route_lock = RouteLock(args.lock_file)
    try:
        path_map = {
            "games": args.games,
            "state": args.state,
            "badge output": args.badge_output,
            "lock": args.lock_file,
        }
        if args.status_file is not None:
            path_map["status"] = args.status_file
        if args.control_file is not None:
            path_map["control"] = args.control_file
        validate_paths(path_map)
        if (
            not math.isfinite(args.seconds)
            or not math.isfinite(args.poll_seconds)
            or not math.isfinite(args.startup_seconds)
        ):
            raise RouteError("timer values must be finite")
        route_lock.acquire()
        universe_ids = parse_universe_ids(args.games)
        state = load_state(args.state, expected_user_id=args.user_id)
        positions = {universe_id: index + 1 for index, universe_id in enumerate(universe_ids)}
        api_cooldown = RequestCooldown()

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
                    "inconclusive": len(state.get("inconclusive_universes", {})),
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
            return get_json(url, sleep=controlled_sleep, cooldown=api_cooldown)

        def controlled_badge_request(url: str, remaining: float) -> Any:
            # Badge polling must not hold a destination open behind the route
            # timer when Roblox is slow or rate-limits the ownership endpoint.
            timeout = max(0.1, min(3.0, remaining))
            return get_json(
                url,
                timeout=timeout,
                retries=1,
                sleep=controlled_sleep,
                cooldown=api_cooldown,
                deadline=time.monotonic() + remaining,
            )

        def controlled_badge_request_default(url: str) -> Any:
            return controlled_badge_request(url, 3.0)

        source_ids = set(universe_ids)
        state_badges = [str(item) for item in state.get("badge_universes", []) if str(item) in source_ids]
        state["badge_universes"] = list(dict.fromkeys(state_badges))
        write_universe_ids(args.badge_output, [item for item in universe_ids if item in set(state_badges)])
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
            badge_request_json=controlled_badge_request_default,
            badge_request_deadline_json=controlled_badge_request,
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
    finally:
        route_lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
