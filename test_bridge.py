#!/usr/bin/env python3
"""Tests for fuldc-arr-bridge. Stdlib unittest, no network.

Run: python -m unittest -v test_bridge

The FulDC++ API calls are faked at the _call boundary, so these assert the
exact request bodies we send. Several of these encode behaviour that the API
requires but does not enforce — a missing use_params, for instance, produces a
working-looking AutoSearch item that silently never matches anything.
"""

from __future__ import annotations

import unittest

import core
import ranker
from fuldc_client import PRIO_HIGH, PRIO_LOW, FulDCClient, FulDCError


class FakeClient(FulDCClient):
    """FulDCClient with the transport replaced by a scripted response table."""

    def __init__(self, responses=None):
        super().__init__("http://localhost:5600", "u", "p")
        self.calls: list[tuple[str, str, dict | None]] = []
        self.responses = responses or {}

    def _call(self, method, path, body=None):
        self.calls.append((method, path, body))
        key = (method, path)
        if key in self.responses:
            r = self.responses[key]
            return r.pop(0) if isinstance(r, list) else r
        if "/results/" in path or path.endswith("/items"):
            return 200, []          # list-shaped endpoints
        return 200, {"id": 1}

    def body_for(self, method, path) -> dict:
        for m, p, b in self.calls:
            if m == method and p == path:
                return b or {}
        raise AssertionError(f"no {method} {path} in {[(m, p) for m, p, _ in self.calls]}")


class TestAutoSearchIncrementation(unittest.TestCase):
    """AutoSearch.cpp:207 returns early from formatParams when useParams is
    false, so %[inc] is searched for literally. The monitor looks fine in the
    UI and never matches."""

    def test_monitor_enables_use_params(self):
        c = FakeClient()
        core.monitor_tv_season(c, "Severance", 2, dc_root="S:\\dc", log=lambda m: None)
        body = c.body_for("POST", "/auto_search/items")
        self.assertTrue(body["use_params"], "%[inc] never expands without use_params")
        self.assertEqual(body["cur_number"], 1)
        self.assertEqual(body["max_number"], 0)      # 0 = no upper bound
        self.assertEqual(body["number_length"], 2)   # E01, not E1
        self.assertIn("%[inc]", body["search_string"])

    def test_monitor_can_start_mid_season(self):
        c = FakeClient()
        core.monitor_tv_season(c, "Severance", 2, first_episode=4, log=lambda m: None)
        self.assertEqual(c.body_for("POST", "/auto_search/items")["cur_number"], 4)

    def test_monitor_never_expires(self):
        c = FakeClient()
        core.monitor_tv_season(c, "Severance", 2, log=lambda m: None)
        body = c.body_for("POST", "/auto_search/items")
        self.assertNotIn("expire_time", body, "an ongoing show has no end date")
        self.assertFalse(body["remove_after_hit"])


class TestAutoSearchHygiene(unittest.TestCase):
    def test_always_checks_queue_and_share(self):
        c = FakeClient()
        c.create_autosearch("Dune 2021")
        body = c.body_for("POST", "/auto_search/items")
        self.assertTrue(body["check_already_queued"])
        self.assertTrue(body["check_already_shared"])

    def test_one_shot_items_expire(self):
        c = FakeClient()
        core.hybrid_grab(c, "Dune", 2021, wait=0, log=lambda m: None)
        body = c.body_for("POST", "/auto_search/items")
        self.assertIn("expire_time", body,
                      "an abandoned request would otherwise search hubs forever")

    def test_duplicate_updates_instead_of_raising(self):
        """POST 409s on a duplicate search_string (AutoSearchApi.cpp:322-325).
        Re-requesting a title must revive the existing item, not blow up."""
        c = FakeClient({
            ("POST", "/auto_search/items"): (409, {"message": "Duplicate"}),
            ("GET", "/auto_search/items"): (200, [{"id": 77, "search_string": "Dune 2021"}]),
            ("PATCH", "/auto_search/items/77"): (200, {"id": 77}),
        })
        item = c.create_autosearch("Dune 2021", target_directory="S:\\dc\\movies")
        self.assertEqual(item["id"], 77)
        patch = c.body_for("PATCH", "/auto_search/items/77")
        self.assertTrue(patch["enabled"], "a spent item must be re-enabled")
        self.assertNotIn("search_string", patch, "the match key is not patchable")

    def test_duplicate_with_no_match_still_raises(self):
        c = FakeClient({
            ("POST", "/auto_search/items"): (409, {"message": "Too short"}),
            ("GET", "/auto_search/items"): (200, []),
        })
        with self.assertRaises(FulDCError) as ctx:
            c.create_autosearch("x")
        self.assertEqual(ctx.exception.status, 409)


class TestSearchPriority(unittest.TestCase):
    """FileSearchParser.cpp:34-37 defaults an absent priority to LOW, which is
    the 15s per-hub interval and the first class shed by the 503 overflow guard
    at SearchEntity.cpp:184."""

    def test_priority_is_sent_and_is_top_level(self):
        c = FakeClient({("GET", "/search/1/results/0/200"): (200, [])})
        c.search("Dune 2021", wait=0)
        body = c.body_for("POST", "/search/1/hub_search")
        self.assertEqual(body["priority"], PRIO_HIGH)
        self.assertNotIn("priority", body["query"], "priority is a sibling of query")

    def test_background_callers_can_opt_down(self):
        c = FakeClient({("GET", "/search/1/results/0/200"): (200, [])})
        c.search("Dune", wait=0, priority=PRIO_LOW)
        self.assertEqual(c.body_for("POST", "/search/1/hub_search")["priority"], PRIO_LOW)

    def test_overflow_status_is_preserved(self):
        """503 must be distinguishable so callers can back off rather than
        treating it as a hard failure."""
        c = FakeClient({("POST", "/search/1/hub_search"): (503, {"message": "overflow"})})
        with self.assertRaises(FulDCError) as ctx:
            c.search("Dune", wait=0)
        self.assertEqual(ctx.exception.status, 503)


class TestTargetPathSafety(unittest.TestCase):
    """The show name arrives from the Seerr webhook, i.e. off the network."""

    def test_traversal_is_neutralised(self):
        for bad in [r"..\..\Users\Public", r"C:\Windows\Temp", "..", "  .. . ",
                    "Sev/er:ance", "con.txt"]:
            got = core.resolve_target("series", bad, None, r"S:\dc", None, 1)
            self.assertTrue(got.startswith("S:\\dc\\series\\"), got)
            self.assertNotIn("..", got)

    def test_normal_names_survive(self):
        got = core.resolve_target("series", "The Expanse", None, r"S:\dc", None, 3)
        self.assertEqual(got, "S:\\dc\\series\\The Expanse\\S03\\")


class TestRanker(unittest.TestCase):
    def _res(self, path, size, users=2):
        return {"path": path, "size": size, "users": {"count": users},
                "slots": {"free": 4}, "type": {"id": "directory"}}

    def test_quality_in_subfolder_is_matched(self):
        """parse_release_folder skips the quality segment, so require_quality
        must look at the whole path or it filters out everything."""
        res = [self._res("/-x264-Kids/Dune.2021.BluRay.x264-GRP/1080p/", 9 * 1024**3),
               self._res("/share/Dune.2021.CAM.XviD/480p/", 1 * 1024**3)]
        cands = ranker.rank(res, "Dune", 2021, ranker.Prefs(require_quality=["1080p"]))
        self.assertEqual(len(cands), 1)
        self.assertIn("1080p", cands[0].reasons)

    def test_episode_escapes_the_movie_size_floor(self):
        ep = self._res("/tv/Severance.S02E03.1080p.WEB.x265/", 400 * 1024**2)
        c = ranker.score_result(ep, "Severance", None, ranker.Prefs(), kind="series")
        self.assertNotIn("too-small", c.reasons)

    def test_movie_still_has_a_size_floor(self):
        mv = self._res("/m/Dune.2021.1080p/", 400 * 1024**2)
        c = ranker.score_result(mv, "Dune", 2021, ranker.Prefs())
        self.assertIn("too-small", c.reasons)

    def test_hub_root_folder_does_not_poison_bad_source(self):
        r = self._res("/-TS-Releases/Dune.2021.1080p.BluRay/1080p/", 9 * 1024**3)
        self.assertNotIn("BAD-source",
                         ranker.score_result(r, "Dune", 2021, ranker.Prefs()).reasons)

    def test_real_cam_is_still_rejected(self):
        r = self._res("/m/Dune.2021.CAM.x264/", 2 * 1024**3)
        self.assertIn("BAD-source",
                      ranker.score_result(r, "Dune", 2021, ranker.Prefs()).reasons)

    def test_season_pack_beats_single_episode(self):
        res = [self._res("/tv/Severance.S02.COMPLETE.1080p/", 20 * 1024**3),
               self._res("/tv/Severance.S02E01.1080p/", 2 * 1024**3)]
        cands = ranker.rank(res, "Severance", None, ranker.Prefs(), kind="series")
        self.assertIn("S02.COMPLETE", cands[0].release)


class TestSceneTitle(unittest.TestCase):
    """DC releases are dotted, punctuation-stripped. A search token like 'Rings:'
    (colon attached) matches nothing in a dotted filename, so the search strings
    must be scene-formatted."""

    def test_colon_and_spaces_become_dots(self):
        self.assertEqual(
            ranker.scene_title("Lord of the Rings: The Rings of Power"),
            "Lord.of.the.Rings.The.Rings.of.Power")

    def test_apostrophe_dropped_hyphen_kept(self):
        self.assertEqual(ranker.scene_title("Marvel's Agatha All Along"),
                         "Marvels.Agatha.All.Along")
        self.assertEqual(ranker.scene_title("Spider-Man: Brand New Day"),
                         "Spider-Man.Brand.New.Day")

    def test_monitor_matcher_has_no_punctuation(self):
        c = FakeClient()
        core.monitor_tv_season(c, "Lord of the Rings: The Rings of Power", 3,
                               log=lambda m: None)
        ss = c.body_for("POST", "/auto_search/items")["search_string"]
        self.assertNotIn(":", ss)
        self.assertIn("Rings.of.Power", ss)


class TestYearFolder(unittest.TestCase):
    def test_series_folder_gets_year(self):
        got = core.resolve_target("series", "Shameless", None, r"S:\dc", None, 3,
                                  year=2011)
        self.assertEqual(got, "S:\\dc\\series\\Shameless (2011)\\S03\\")

    def test_no_year_is_unchanged(self):
        got = core.resolve_target("series", "Silo", None, r"S:\dc", None, 2)
        self.assertEqual(got, "S:\\dc\\series\\Silo\\S02\\")


class TestSeasonPackMatcher(unittest.TestCase):
    """An ended-show season AutoSearch must match a PACK, not a single episode
    (partial matching treats S03 as a substring of S03E02)."""

    def test_ended_season_uses_pack_regex(self):
        import re as _re
        c = FakeClient()
        core.hybrid_grab(c, "Shameless", None, kind="series", season=3,
                         prefs=ranker.Prefs(require_quality=["1080p"]),
                         wait=0, log=lambda m: None)
        body = c.body_for("POST", "/auto_search/items")
        self.assertEqual(body["matcher_type"], "regex")
        rx = _re.compile(body["matcher_string"])
        self.assertTrue(rx.search("Shameless.US.S03.1080p.BluRay.x264-ROVERS"))
        self.assertFalse(rx.search("Shameless.US.S03E02.1080p.BluRay.x264-ROVERS"),
                         "must not match a single episode")
        self.assertFalse(rx.search("Shameless.US.S03.720p.BluRay"),
                         "must not match the wrong quality")


if __name__ == "__main__":
    unittest.main()
