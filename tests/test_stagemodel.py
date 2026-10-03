"""Per-hour estimates for unplayed stages on synthetic stages: no game, no save."""
import unittest

from tbh.analysis import stagemodel

A, B, GOLD_RATIO, XP_RATIO = 10.0, 0.001, 0.2, 0.5


def stage(key, waves, monsters, hp, gold_clear, exp_clear=1000.0, dmg=1.0):
    return {'stage': key, 'waves': waves, 'monsters_per_wave': monsters, 'monster_hp_mult': hp,
            'monster_dmg_mult': dmg, 'gold_per_clear': gold_clear, 'exp_per_clear': exp_clear}


def played(t, runs=6):
    """Evidence that follows the model exactly."""
    seconds = t['waves'] * (A + B * t['monsters_per_wave'] * t['monster_hp_mult'])
    return {'stage': t['stage'], 'runs': runs, 'xp_runs': runs, 'median_duration_s': seconds,
            'minutes': runs * seconds / 60,
            'gold_h': t['gold_per_clear'] * GOLD_RATIO * 3600 / seconds,
            'xp_h': t['exp_per_clear'] * XP_RATIO * 3600 / seconds}


THEORY = [stage(1, 10, 5, 10, 1000), stage(2, 14, 8, 80, 5000), stage(3, 18, 10, 250, 20000),
          stage(4, 16, 9, 150, 9000, dmg=2.0)]


class ModelTests(unittest.TestCase):
    def test_recovers_an_exact_model_with_no_error(self):
        model = stagemodel.fit([played(t) for t in THEORY], THEORY)
        self.assertAlmostEqual(model['a'], A, places=6)
        self.assertAlmostEqual(model['b'], B, places=9)
        self.assertAlmostEqual(model['gold_ratio'], GOLD_RATIO)
        self.assertAlmostEqual(model['error']['gold_h'], 0, places=6)
        new = stage(9, 20, 12, 400, 50000)
        est = stagemodel.estimate(model, new)
        seconds = 20 * (A + B * 12 * 400)
        self.assertAlmostEqual(est['duration_s'], seconds)
        self.assertAlmostEqual(est['gold_h'], 50000 * GOLD_RATIO * 3600 / seconds)

    def test_needs_enough_played_stages_with_runs(self):
        self.assertIsNone(stagemodel.fit([played(t) for t in THEORY[:2]], THEORY))
        few_runs = [played(t, runs=2) for t in THEORY]
        self.assertIsNone(stagemodel.fit(few_runs, THEORY))

    def test_faster_unplayed_stage_ranks_above_richer_but_slower(self):
        model = stagemodel.fit([played(t) for t in THEORY], THEORY)
        fast = stage(10, 6, 1, 1, 100)    # ~1 min for 100 gold per clear
        slow = stage(11, 24, 1, 1, 350)   # ~4 min for 350 gold per clear
        self.assertGreater(stagemodel.estimate(model, fast)['gold_h'], stagemodel.estimate(model, slow)['gold_h'])

    def test_range_follows_measured_error_and_flags_harder_monsters(self):
        noisy = [played(t) for t in THEORY]
        noisy[0]['median_duration_s'] *= 1.5
        model = stagemodel.fit(noisy, THEORY)
        self.assertGreater(model['error']['gold_h'], 0)
        est = stagemodel.estimate(model, stage(12, 15, 9, 200, 10000, dmg=3.0))
        self.assertLess(est['gold_low'], est['gold_h'])
        self.assertGreater(est['gold_high'], est['gold_h'])
        self.assertAlmostEqual(est['dmg_vs_observed'], 1.5)


if __name__ == '__main__':
    unittest.main()
