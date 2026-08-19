"""Unit tests for arb_core. Run with: python -m unittest test_arb_core -v"""

import unittest
from datetime import datetime, timedelta, timezone

import arb_core as core


NOW = datetime(2026, 8, 17, 12, 0, 0, tzinfo=timezone.utc)


class TestOddsArithmetic(unittest.TestCase):
    def test_american_conversion(self):
        self.assertAlmostEqual(core.american_to_decimal(150), 2.5)
        self.assertAlmostEqual(core.american_to_decimal(-200), 1.5)
        with self.assertRaises(ValueError):
            core.american_to_decimal(0)

    def test_commission_applies_to_profit_only(self):
        self.assertAlmostEqual(core.effective_price(3.0, 0.05), 2.90)
        self.assertAlmostEqual(core.effective_price(2.0, 0.0), 2.0)
        with self.assertRaises(ValueError):
            core.effective_price(1.0, 0.0)
        with self.assertRaises(ValueError):
            core.effective_price(2.0, 1.0)

    def test_book_sum_and_roi(self):
        s = core.book_sum([2.1, 2.1])
        self.assertAlmostEqual(s, 2 / 2.1)
        # roi is 1/S - 1, which is 5.0% here, NOT the 4.76% overround.
        self.assertAlmostEqual(core.roi_from_book_sum(s), 0.05)
        self.assertAlmostEqual(core.roi_from_book_sum(1.05), 1 / 1.05 - 1)

    def test_book_sum_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            core.book_sum([2.0])
        with self.assertRaises(ValueError):
            core.book_sum([2.0, 1.0])


class TestAllocation(unittest.TestCase):
    def test_even_two_way_arb(self):
        alloc = core.allocate_stakes([2.1, 2.1], 100.0, 0.01)
        self.assertEqual(alloc.stakes, [50.0, 50.0])
        self.assertAlmostEqual(alloc.worst_return, 105.0)
        self.assertAlmostEqual(alloc.profit, 5.0)
        self.assertAlmostEqual(alloc.roi, 0.05)

    def test_stakes_always_sum_to_budget(self):
        for prices in ([2.9, 3.6, 3.1], [1.83, 2.28], [4.1, 2.6, 2.9], [1.5, 3.9, 9.0]):
            for total, inc in ((100.0, 0.01), (250.0, 1.0), (37.5, 0.5)):
                alloc = core.allocate_stakes(prices, total, inc)
                self.assertAlmostEqual(alloc.total_staked, total, places=6,
                                       msg=f"{prices} {total} {inc}")

    def test_returns_are_balanced_and_profit_is_worst_case(self):
        alloc = core.allocate_stakes([2.9, 3.6, 3.1], 100.0, 0.01)
        spread = alloc.best_return - alloc.worst_return
        self.assertLess(spread, 0.10)
        self.assertAlmostEqual(alloc.profit, alloc.worst_return - alloc.total_staked, places=9)
        self.assertGreater(alloc.profit, 0)

    def test_coarse_rounding_never_overstates_profit(self):
        # Whole-pound stakes on a thin edge: reported profit must be the real
        # post-rounding worst case, which can be worse than the theory.
        prices = [2.02, 2.03]
        theory = core.roi_from_book_sum(core.book_sum(prices))
        alloc = core.allocate_stakes(prices, 25.0, 5.0)
        self.assertAlmostEqual(alloc.total_staked, 25.0)
        self.assertLessEqual(alloc.roi, theory + 1e-9)
        self.assertAlmostEqual(alloc.profit, min(alloc.returns) - alloc.total_staked, places=9)

    def test_allocation_is_max_min_optimal(self):
        """Exhaustive check: no other split of the same budget pays more in
        its worst case. Covers the coarse-increment cases where naive
        rounding goes wrong."""
        import itertools
        import random

        random.seed(20260817)
        for _ in range(400):
            n = random.choice([2, 2, 3, 4])
            prices = [round(random.uniform(1.05, 20.0), 2) for _ in range(n)]
            increment = random.choice([1.0, 0.5, 5.0])
            units = random.randint(n, 11)
            alloc = core.allocate_stakes(prices, units * increment, increment)
            best = max(
                min(u * increment * p for u, p in zip(combo, prices))
                for combo in itertools.product(range(units + 1), repeat=n)
                if sum(combo) == units
            )
            self.assertAlmostEqual(alloc.worst_return, best, places=9,
                                   msg=f"{prices} units={units} inc={increment}")

    def test_every_leg_is_covered(self):
        alloc = core.allocate_stakes([1.9, 8.5, 21.0], 60.0, 1.0)
        self.assertTrue(all(s > 0 for s in alloc.stakes))

    def test_rejects_impossible_inputs(self):
        with self.assertRaises(ValueError):
            core.allocate_stakes([2.0, 2.0], 0.0, 0.01)
        with self.assertRaises(ValueError):
            core.allocate_stakes([2.0, 2.0], 100.0, -1)
        with self.assertRaises(ValueError):
            core.allocate_stakes([2.0], 100.0, 0.01)
        with self.assertRaises(ValueError):
            core.allocate_stakes([2.0, 2.0, 2.0], 2.0, 1.0)


class TestParsing(unittest.TestCase):
    def setUp(self):
        self.groups = core.parse_events(core.demo_payload(NOW))

    def test_groups_are_keyed_by_event_market_and_line(self):
        keys = {(g.event_id, g.market_key, g.line) for g in self.groups}
        self.assertIn(("demo-soccer-1", "h2h", None), keys)
        self.assertIn(("demo-basket-1", "totals", 224.5), keys)
        self.assertIn(("demo-basket-1", "totals", 226.5), keys)

    def test_totals_lines_are_not_merged(self):
        lines = [g for g in self.groups if g.event_id == "demo-basket-1"]
        self.assertEqual(len(lines), 2)

    def test_spread_sides_share_one_line_via_home_handicap(self):
        spreads = [g for g in self.groups if g.market_key == "spreads"]
        self.assertEqual(len(spreads), 1)
        self.assertEqual(spreads[0].line, -2.5)
        self.assertEqual(len(spreads[0].quotes), 4)

    def test_market_filter(self):
        only_h2h = core.parse_events(core.demo_payload(NOW), markets_wanted=["h2h"])
        self.assertTrue(all(g.market_key == "h2h" for g in only_h2h))

    def test_bad_records_are_skipped_not_fatal(self):
        payload = [{
            "id": "x", "sport_key": "s", "sport_title": "S",
            "commence_time": "not-a-date", "home_team": "A", "away_team": "B",
            "bookmakers": [{"key": "k", "title": "K", "markets": [{"key": "h2h", "outcomes": [
                {"name": "A", "price": None},
                {"name": "B", "price": "1.0"},
                {"name": "Draw", "price": "2.5"},
            ]}]}],
        }]
        groups = core.parse_events(payload)
        self.assertEqual(len(groups), 1)
        self.assertEqual([q.outcome for q in groups[0].quotes], ["Draw"])
        self.assertIsNone(groups[0].commence_time)


class TestDetection(unittest.TestCase):
    def setUp(self):
        self.groups = core.parse_events(core.demo_payload(NOW))

    def find(self, **kwargs):
        kwargs.setdefault("now", NOW)
        return core.find_opportunities(self.groups, **kwargs)

    def test_finds_the_planted_arbs(self):
        found = self.find()
        markets = {(o.event_id, o.market_key, o.line) for o in found}
        self.assertIn(("demo-soccer-1", "h2h", None), markets)
        self.assertIn(("demo-basket-1", "totals", 224.5), markets)
        self.assertIn(("demo-nfl-1", "spreads", -2.5), markets)

    def test_ignores_the_no_arb_market(self):
        found = self.find()
        self.assertNotIn("demo-tennis-1", {o.event_id for o in found})

    def test_two_way_book_in_a_three_way_market_is_excluded(self):
        found = self.find()
        soccer = next(o for o in found if o.event_id == "demo-soccer-1")
        self.assertEqual(len(soccer.legs), 3)
        self.assertNotIn("book_2way", {leg.bookmaker_key for leg in soccer.legs})

    def test_disabling_coverage_check_lets_the_trap_through(self):
        """The guard is load-bearing: without it the two-way price wins a leg
        and invents an edge that a draw would wipe out."""
        loose = self.find(require_full_coverage=False)
        soccer = next(o for o in loose if o.event_id == "demo-soccer-1")
        self.assertIn("book_2way", {leg.bookmaker_key for leg in soccer.legs})
        strict = next(o for o in self.find() if o.event_id == "demo-soccer-1")
        self.assertGreater(loose[0].roi if loose[0].event_id == "demo-soccer-1"
                           else soccer.roi, strict.roi)

    def test_soccer_arb_maths(self):
        opp = next(o for o in self.find() if o.event_id == "demo-soccer-1")
        picks = {leg.outcome: leg for leg in opp.legs}
        self.assertEqual(picks["Arsenal"].price, 2.25)
        self.assertEqual(picks["Chelsea"].price, 3.85)
        self.assertEqual(picks["Draw"].price, 3.60)
        expected = core.book_sum([2.25, 3.85, 3.60])
        self.assertAlmostEqual(opp.book_sum, expected)
        self.assertAlmostEqual(opp.theoretical_roi, 1 / expected - 1)
        self.assertAlmostEqual(sum(leg.stake for leg in opp.legs), opp.total_stake, places=6)
        self.assertGreater(opp.guaranteed_profit, 0)
        for leg in opp.legs:
            self.assertAlmostEqual(leg.payout, leg.stake * leg.net_price, places=6)
            self.assertGreaterEqual(leg.payout, opp.total_stake + opp.guaranteed_profit - 1e-6)

    def test_min_roi_filter(self):
        self.assertTrue(self.find(min_roi=0.0))
        self.assertEqual(self.find(min_roi=0.95), [])

    def test_results_are_sorted_best_first(self):
        rois = [o.roi for o in self.find()]
        self.assertEqual(rois, sorted(rois, reverse=True))

    def test_commission_erodes_the_edge(self):
        base = next(o for o in self.find() if o.event_id == "demo-soccer-1")
        taxed = core.find_opportunities(
            self.groups, commissions={"book_b": 0.005, "BOOK_C": 0.005}, now=NOW
        )
        after = next(o for o in taxed if o.event_id == "demo-soccer-1")
        self.assertLess(after.roi, base.roi)
        leg = next(l for l in after.legs if l.bookmaker_key == "book_c")
        self.assertAlmostEqual(leg.net_price, core.effective_price(leg.price, 0.005))

    def test_demo_edges_are_plausible(self):
        for opp in self.find():
            self.assertLess(opp.roi, 0.05, msg=opp.event_name)

    def test_commission_can_kill_an_arb_entirely(self):
        # Commission is charged on winnings, not stake, so 5% at odds near 2.0
        # costs about 2.5% of stake -- enough to wipe out the 1.8% soccer edge
        # while the fatter totals edge survives.
        taxed = core.find_opportunities(
            self.groups,
            commissions={"book_a": 0.05, "book_b": 0.05, "book_c": 0.05},
            now=NOW,
        )
        self.assertNotIn("demo-soccer-1", {o.event_id for o in taxed})
        self.assertIn("demo-basket-1", {o.event_id for o in taxed})

        # Nothing survives a punitive rate.
        self.assertEqual(
            core.find_opportunities(
                self.groups,
                commissions={"book_a": 0.5, "book_b": 0.5, "book_c": 0.5},
                now=NOW,
            ),
            [],
        )

    def test_stale_prices_are_dropped(self):
        fresh = self.find(max_age_minutes=60)
        self.assertTrue(fresh)
        stale = core.find_opportunities(
            self.groups, max_age_minutes=60, now=NOW + timedelta(hours=3)
        )
        self.assertEqual(stale, [])

    def test_single_bookmaker_arb_is_suppressed_by_default(self):
        payload = [{
            "id": "e1", "sport_key": "s", "sport_title": "S",
            "commence_time": "2026-08-18T12:00:00Z",
            "home_team": "A", "away_team": "B",
            "bookmakers": [{
                "key": "solo", "title": "Solo", "last_update": "2026-08-17T11:59:00Z",
                "markets": [{"key": "h2h", "last_update": "2026-08-17T11:59:00Z", "outcomes": [
                    {"name": "A", "price": 2.20}, {"name": "B", "price": 2.20},
                ]}],
            }],
        }]
        groups = core.parse_events(payload)
        self.assertEqual(core.find_opportunities(groups, now=NOW), [])
        loose = core.find_opportunities(groups, now=NOW, require_distinct_books=False)
        self.assertEqual(len(loose), 1)

    def test_warnings_flag_started_events_and_fat_edges(self):
        payload = [{
            "id": "e2", "sport_key": "s", "sport_title": "S",
            "commence_time": "2026-08-17T11:00:00Z",
            "home_team": "A", "away_team": "B",
            "bookmakers": [
                {"key": "b1", "title": "B1", "last_update": "2026-08-17T11:59:00Z",
                 "markets": [{"key": "h2h", "last_update": "2026-08-17T11:59:00Z",
                              "outcomes": [{"name": "A", "price": 5.0}, {"name": "B", "price": 1.5}]}]},
                {"key": "b2", "title": "B2", "last_update": "2026-08-17T11:59:00Z",
                 "markets": [{"key": "h2h", "last_update": "2026-08-17T11:59:00Z",
                              "outcomes": [{"name": "A", "price": 1.6}, {"name": "B", "price": 4.0}]}]},
            ],
        }]
        opp = core.find_opportunities(core.parse_events(payload), now=NOW)[0]
        joined = " ".join(opp.warnings)
        self.assertIn("already started", joined)
        self.assertIn("Unusually large edge", joined)

    def test_empty_input_is_fine(self):
        self.assertEqual(core.find_opportunities([]), [])
        self.assertEqual(core.parse_events([]), [])
        self.assertEqual(core.parse_events(None), [])


class TestClientGuards(unittest.TestCase):
    def test_key_is_required(self):
        with self.assertRaises(core.OddsFeedError):
            core.OddsAPIClient("")
        with self.assertRaises(core.OddsFeedError):
            core.OddsAPIClient("   ")

    def test_parameters_are_validated_before_any_network_call(self):
        client = core.OddsAPIClient("dummy")
        with self.assertRaises(core.OddsFeedError):
            client.fetch_odds("soccer_epl", [], ["h2h"])
        with self.assertRaises(core.OddsFeedError):
            client.fetch_odds("soccer_epl", ["uk"], [])


class TestExport(unittest.TestCase):
    def test_csv_has_one_row_per_leg(self):
        import csv as _csv
        import tempfile
        import os

        opps = core.find_opportunities(core.parse_events(core.demo_payload(NOW)), now=NOW)
        path = os.path.join(tempfile.mkdtemp(), "out.csv")
        core.opportunities_to_csv(opps, path)
        with open(path, encoding="utf-8") as handle:
            rows = list(_csv.reader(handle))
        self.assertEqual(len(rows) - 1, sum(len(o.legs) for o in opps))


if __name__ == "__main__":
    unittest.main(verbosity=2)
