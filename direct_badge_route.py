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
USER_AGENT = "roblox-badge-farm-direct/1.0"


class RouteError(RuntimeError):
    """An expected route or API error."""


def parse_universe_ids(path: Path) -> list[str]:
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
    if not result:
        raise RouteError(f"{path} does not contain any universe IDs")
    return result


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
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
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
) -> dict[str, dict[str, Any]]:
    """Resolve universe IDs to root places and return the API metadata."""

    destinations: dict[str, dict[str, Any]] = {}
    for batch in _batches(universe_ids, batch_size):
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
    return destinations


def list_badge_ids(
    universe_id: str,
    *,
    request_json: Callable[[str], Any] = get_json,
) -> list[str]:
    """List all badge IDs belonging to a universe."""

    result: list[str] = []
    seen: set[str] = set()
    cursor: str | None = None
    seen_cursors: set[str] = set()
    while True:
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

    def _is_owned(self, badge_id: str) -> bool:
        now = self.monotonic()
        if self._last_request is not None:
            delay = self.min_interval - (now - self._last_request)
            if delay > 0:
                self.sleep(delay)
        url = OWNERSHIP_API.format(user_id=self.user_id, badge_id=badge_id)
        payload = self.request_json(url)
        self._last_request = self.monotonic()
        owned = _owned_value(payload)
        if owned:
            self._known_owned.add(str(badge_id))
        return owned

    def snapshot(self, badge_ids: Iterable[str]) -> set[str]:
        """Return the badges already owned before entering a destination."""

        baseline: set[str] = set()
        for badge_id in badge_ids:
            if self._is_owned(str(badge_id)):
                baseline.add(str(badge_id))
        return baseline

    def find_new(self, badge_ids: Iterable[str], baseline: set[str]) -> str | None:
        """Return the first badge that changed from unowned to owned."""

        for badge_id in badge_ids:
            badge_id = str(badge_id)
            if badge_id in baseline or badge_id in self._known_owned:
                continue
            if self._is_owned(badge_id):
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
    return {"version": 1, "completed_universes": [], "failed_universes": {}, "last_universe": None}


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
    if not isinstance(completed, list) or not isinstance(failed, dict):
        raise RouteError(f"state file {path} has an invalid shape")
    normalized = new_state()
    normalized["completed_universes"] = list(dict.fromkeys(str(item) for item in completed if str(item).isdigit()))
    normalized["failed_universes"] = {str(key): str(value) for key, value in failed.items()}
    last = data.get("last_universe")
    normalized["last_universe"] = str(last) if last is not None else None
    return normalized


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    payload = {
        "version": 1,
        "completed_universes": list(dict.fromkeys(str(item) for item in state.get("completed_universes", []))),
        "failed_universes": {str(key): str(value) for key, value in state.get("failed_universes", {}).items()},
        "last_universe": state.get("last_universe"),
    }
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


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
) -> dict[str, Any]:
    """Process the route once and return the updated state."""

    if launch and badge_check and user_id is None:
        raise RouteError("--user-id is required with --launch unless --no-badge-check is used")
    if seconds < 0 or poll_seconds <= 0 or startup_seconds < 0:
        raise RouteError("seconds and startup settings must be non-negative; poll-seconds must be positive")

    checker = (
        BadgeChecker(user_id, request_json=request_json, sleep=sleep, monotonic=monotonic)
        if launch and badge_check and user_id is not None
        else None
    )
    completed = set(str(item) for item in state.get("completed_universes", []))

    for universe_id in universe_ids:
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

        badge_ids: list[str] = []
        baseline: set[str] = set()
        badge_poll_enabled = checker is not None
        if checker is not None:
            try:
                badge_ids = list_badge_ids(universe_id, request_json=request_json)
                if badge_ids:
                    baseline = checker.snapshot(badge_ids)
            except RouteError as exc:
                output(f"badge check unavailable for {universe_id}: {exc}; using the timer")
                badge_poll_enabled = False

        uri = build_launch_uri(destination["place_id"])
        title = destination.get("name") or universe_id
        output(f"{universe_id} -> {title} (place {destination['place_id']})")
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

        if startup_seconds:
            sleep(startup_seconds)
        deadline = monotonic() + seconds
        awarded: str | None = None
        while monotonic() < deadline:
            if badge_poll_enabled and checker is not None and badge_ids:
                try:
                    awarded = checker.find_new(badge_ids, baseline)
                except RouteError as exc:
                    output(f"badge check unavailable in {universe_id}: {exc}; using the timer")
                    badge_poll_enabled = False
                if awarded:
                    output(f"badge {awarded} detected in {universe_id}; continuing")
                    break
            remaining = deadline - monotonic()
            if remaining > 0:
                sleep(min(poll_seconds, remaining))
        if not awarded:
            output(f"{seconds:g}-second window elapsed for {universe_id}; continuing")
        _mark_completed(state, universe_id)
        completed.add(universe_id)
        if save_progress is not None:
            save_progress(state)
        output(f"completed {universe_id}")

    return state


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--games", type=Path, default=Path("games.txt"), help="universe IDs, one per line")
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
    try:
        universe_ids = parse_universe_ids(args.games)
        state = load_state(args.state)
        completed = set(str(item) for item in state.get("completed_universes", []))
        pending = [item for item in universe_ids if item not in completed]
        if args.limit is not None:
            if args.limit <= 0:
                raise RouteError("--limit must be positive")
            pending = pending[: args.limit]
        if not pending:
            print("no uncompleted destinations")
            return 0
        destinations = resolve_destinations(pending)
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
            save_progress=lambda current: save_state(args.state, current),
        )
        save_state(args.state, state)
        return 0
    except (OSError, RouteError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
