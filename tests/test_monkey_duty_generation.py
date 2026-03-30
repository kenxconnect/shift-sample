from __future__ import annotations

import unittest

import tests.test_monkey as monkey
import tests.test_monkey_reopt as monkey_reopt


def _non_empty_duties(scenario: dict) -> list[str]:
    return [
        name
        for name in scenario.get("duties", {}).values()
        if isinstance(name, str) and name.strip()
    ]


class MonkeyDutyGenerationTests(unittest.TestCase):
    def test_random_scenario_uses_unique_non_lunch_duties(self) -> None:
        monkey.random.seed(42)
        for trial in range(1, 21):
            scenario = monkey.random_scenario(
                trial,
                use_random_constraints=trial % 2 == 0,
                use_random_staff=trial % 3 == 0,
            )
            duties = _non_empty_duties(scenario)
            self.assertEqual(
                len(duties),
                len(set(duties)),
                f"duplicate duties found in monkey scenario trial {trial}: {scenario['duties']}",
            )

    def test_random_reopt_scenario_uses_unique_non_lunch_duties(self) -> None:
        monkey_reopt.random.seed(2026)
        for trial in range(1, 21):
            scenario = monkey_reopt.random_reopt_scenario(
                trial,
                use_random_constraints=trial % 2 == 0,
                use_random_staff=trial % 3 == 0,
            )
            duties = _non_empty_duties(scenario)
            self.assertEqual(
                len(duties),
                len(set(duties)),
                f"duplicate duties found in reopt monkey scenario trial {trial}: {scenario['duties']}",
            )


if __name__ == "__main__":
    unittest.main()
