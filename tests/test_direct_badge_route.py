import json
import tempfile
import unittest
from pathlib import Path

import direct_badge_route as route


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class DirectBadgeRouteTests(unittest.TestCase):
    def test_parse_ids_deduplicates_and_accepts_comments(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "games.txt"
            path.write_text("1\n 0002 # comment\n1\n\n", encoding="utf-8")
            self.assertEqual(route.parse_universe_ids(path), ["1", "2"])

    def test_parse_ids_rejects_bad_line(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "games.txt"
            path.write_text("1\nnot-an-id\n", encoding="utf-8")
            with self.assertRaises(route.RouteError):
                route.parse_universe_ids(path)

    def test_empty_badge_output_and_normalized_write(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "game-badges.txt"
            path.write_text("", encoding="utf-8")
            self.assertEqual(route.parse_universe_ids(path, allow_empty=True), [])
            route.write_universe_ids(path, ["3", "3", "9"])
            self.assertEqual(path.read_text(encoding="utf-8"), "3\n9\n")
            self.assertEqual(route.parse_universe_ids(path), ["3", "9"])

    def test_resolve_destinations_keeps_api_metadata(self):
        calls = []
        batches = []

        def request_json(url):
            calls.append(url)
            return {
                "data": [
                    {"id": 1, "rootPlaceId": 101, "name": "One", "creator": {"name": "A"}},
                    {"id": 2, "rootPlaceId": 202, "name": "Two"},
                ]
            }

        destinations = route.resolve_destinations(
            ["1", "2"],
            request_json=request_json,
            before_batch=lambda: batches.append("before"),
            on_batch=lambda current, total, found: batches.append((current, total, found)),
        )
        self.assertEqual(destinations["1"]["place_id"], 101)
        self.assertEqual(destinations["2"]["name"], "Two")
        self.assertIn("universeIds=1%2C2", calls[0])
        self.assertEqual(batches, ["before", (1, 1, 2)])

    def test_badge_pagination_and_ownership_transition(self):
        clock = FakeClock()
        ownership_calls = 0

        def request_json(url):
            nonlocal ownership_calls
            if "badges.roblox.com" in url:
                if "cursor=" not in url:
                    return {"data": [{"id": 11}], "nextPageCursor": "next"}
                return {"data": [{"id": 12}], "nextPageCursor": None}
            ownership_calls += 1
            return ownership_calls > 1

        badge_ids = route.list_badge_ids("99", request_json=request_json)
        self.assertEqual(badge_ids, ["11", "12"])
        checker = route.BadgeChecker(
            7,
            request_json=request_json,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            min_interval=0,
        )
        baseline = checker.snapshot(["11"])
        self.assertEqual(baseline, set())
        self.assertEqual(checker.find_new(["11"], baseline), "11")

    def test_run_route_launches_and_saves_completion(self):
        clock = FakeClock()
        launched = []
        saved = []
        state = route.new_state()
        destinations = {"1": {"place_id": 101, "name": "One"}}

        route.run_route(
            ["1"],
            state=state,
            destinations=destinations,
            user_id=None,
            seconds=4,
            poll_seconds=1,
            startup_seconds=2,
            launch=True,
            badge_check=False,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            launch_fn=launched.append,
            save_progress=lambda current: saved.append(json.loads(json.dumps(current))),
            output=lambda _: None,
        )
        self.assertEqual(launched, ["roblox://experiences/start?placeId=101"])
        self.assertEqual(state["completed_universes"], ["1"])
        self.assertEqual(saved[-1]["last_universe"], "1")
        self.assertEqual(clock.value, 6)

    def test_new_badge_records_universe_in_output(self):
        clock = FakeClock()
        calls = 0
        state = route.new_state()
        saved_badges = []

        def request_json(url):
            nonlocal calls
            if "badges.roblox.com" in url:
                return {"data": [{"id": 11}], "nextPageCursor": None}
            calls += 1
            return calls > 1

        route.run_route(
            ["1"],
            state=state,
            destinations={"1": {"place_id": 101, "name": "One"}},
            user_id=7,
            seconds=4,
            poll_seconds=1,
            startup_seconds=0,
            launch=True,
            request_json=request_json,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            launch_fn=lambda _: None,
            save_badges=lambda current: saved_badges.append(list(current["badge_universes"])),
            output=lambda _: None,
        )
        self.assertEqual(state["badge_universes"], ["1"])
        self.assertEqual(saved_badges, [["1"]])

    def test_state_round_trip_is_atomic_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            state = route.new_state()
            state["completed_universes"].append("5")
            state["failed_universes"]["9"] = "missing"
            state["badge_universes"].append("5")
            state["last_universe"] = "5"
            route.save_state(path, state)
            loaded = route.load_state(path)
            self.assertEqual(loaded["completed_universes"], ["5"])
            self.assertEqual(loaded["failed_universes"], {"9": "missing"})
            self.assertEqual(loaded["badge_universes"], ["5"])
            self.assertFalse(path.with_name("state.json.tmp").exists())

    def test_pause_resume_extends_the_timer_and_reports_status(self):
        with tempfile.TemporaryDirectory() as directory:
            control = Path(directory) / "control.json"
            control.write_text('{"command":"pause"}', encoding="utf-8")
            clock = FakeClock()
            state = route.new_state()
            statuses = []
            sleeps = 0

            def sleep(seconds):
                nonlocal sleeps
                sleeps += 1
                clock.sleep(seconds)
                if sleeps == 3:
                    control.write_text('{"command":"resume"}', encoding="utf-8")

            route.run_route(
                ["1"],
                state=state,
                destinations={"1": {"place_id": 101, "name": "One"}},
                user_id=None,
                seconds=2,
                poll_seconds=1,
                startup_seconds=0,
                launch=True,
                badge_check=False,
                sleep=sleep,
                monotonic=clock.monotonic,
                launch_fn=lambda _: None,
                control_file=control,
                status_update=lambda status, *_: statuses.append(status),
                output=lambda _: None,
            )
            self.assertEqual(state["completed_universes"], ["1"])
            self.assertIn("paused", statuses)
            self.assertIn("running", statuses)

    def test_stop_command_stops_before_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            control = Path(directory) / "control.json"
            control.write_text('{"command":"stop"}', encoding="utf-8")
            with self.assertRaises(route.RouteStopped):
                route.run_route(
                    ["1"],
                    state=route.new_state(),
                    destinations={"1": {"place_id": 101, "name": "One"}},
                    user_id=None,
                    launch=True,
                    badge_check=False,
                    control_file=control,
                    launch_fn=lambda _: self.fail("stop command launched a place"),
                    output=lambda _: None,
                )


if __name__ == "__main__":
    unittest.main()
