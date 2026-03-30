from __future__ import annotations

import copy
import unittest

try:
    import test_monkey as legacy_monkey
    import test_monkey_reopt as legacy_reopt
except ModuleNotFoundError:
    import tests.test_monkey as legacy_monkey
    import tests.test_monkey_reopt as legacy_reopt


class TestLegacyMonkeyDiscover(unittest.TestCase):
    def _first_solved_monkey_trial(self) -> tuple[int, dict, dict]:
        legacy_monkey.random.seed(42)
        for trial in range(1, 11):
            scenario = legacy_monkey.random_scenario(trial)
            result = legacy_monkey.generate_schedule(copy.deepcopy(scenario))
            if not result.get("table"):
                continue
            issues = legacy_monkey.check_result(
                trial,
                f"discover trial {trial}",
                result,
                scenario,
            )
            if not issues:
                return trial, scenario, result
        self.fail(
            "legacy monkey scenario did not produce a clean solvable case in first 10 trials"
        )

    def _first_solved_reopt_trial(self) -> tuple[int, dict, dict, tuple[int, int, list[int]], dict]:
        legacy_reopt.random.seed(2026)
        for trial in range(1, 11):
            scenario = legacy_reopt.random_reopt_scenario(trial)
            original = legacy_reopt.generate_schedule(copy.deepcopy(scenario))
            if not original.get("table"):
                continue
            params = legacy_reopt.random_reopt_params(scenario, original)
            result = legacy_reopt.reschedule_after_cancellation(
                original_input=scenario,
                original_result=original,
                reopt_start_slot=params[0],
                reopt_end_slot=params[1],
                cancelled_slots=params[2],
            )
            if not result.get("table"):
                continue
            issues = []
            used_input = result.get("used_input", scenario)
            issues.extend(
                legacy_monkey.check_result(
                    trial,
                    f"discover reopt trial {trial}",
                    result,
                    used_input,
                )
            )
            issues.extend(
                legacy_reopt.verify_fixed_slots(
                    original,
                    result,
                    params[0],
                    params[1],
                    set(params[2]),
                )
            )
            issues.extend(legacy_reopt.verify_cancels(result, set(params[2])))
            if not issues:
                return trial, scenario, original, params, result
        self.fail(
            "legacy reopt monkey scenario did not produce a clean solvable case in first 10 trials"
        )

    def test_legacy_monkey_random_scenario_runs_under_discover(self) -> None:
        trial, scenario, result = self._first_solved_monkey_trial()

        issues = legacy_monkey.check_result(
            trial,
            f"discover trial {trial}",
            result,
            scenario,
        )

        self.assertEqual([], issues)

    def test_legacy_reopt_random_scenario_runs_under_discover(self) -> None:
        trial, scenario, original, params, result = self._first_solved_reopt_trial()

        issues = []
        used_input = result.get("used_input", scenario)
        issues.extend(
            legacy_monkey.check_result(
                trial,
                f"discover reopt trial {trial}",
                result,
                used_input,
            )
        )
        issues.extend(
            legacy_reopt.verify_fixed_slots(
                original,
                result,
                params[0],
                params[1],
                set(params[2]),
            )
        )
        issues.extend(legacy_reopt.verify_cancels(result, set(params[2])))

        self.assertEqual([], issues)


if __name__ == "__main__":
    unittest.main()
