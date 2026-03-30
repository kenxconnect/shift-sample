"""週間シナリオテスト: 2026年3月16日〜23日の実運用条件で generate_schedule を検証する。"""

from __future__ import annotations

import copy
import unittest

from scheduler import generate_schedule
from weekly_scenarios import check_weekly_result, get_weekly_scenario


class TestWeeklyScenarios(unittest.TestCase):
    """2026年3月16日〜23日の7日間シナリオテスト。"""

    def _run_and_assert(self, scenario_name: str) -> dict:
        scenario = get_weekly_scenario(scenario_name)
        input_data = copy.deepcopy(scenario["input_data"])
        result = generate_schedule(input_data)
        self.assertTrue(
            result.get("table"),
            f"{scenario['label']}: ソルバーが解を返せなかった",
        )
        issues = check_weekly_result(result, input_data)
        self.assertEqual(
            issues,
            [],
            f"{scenario['label']}: ハード制約違反:\n" + "\n".join(issues),
        )
        return result

    def test_march_16(self) -> None:
        self._run_and_assert("march_16")

    def test_march_17(self) -> None:
        self._run_and_assert("march_17")

    def test_march_18(self) -> None:
        self._run_and_assert("march_18")

    def test_march_19(self) -> None:
        self._run_and_assert("march_19")

    def test_march_20(self) -> None:
        self._run_and_assert("march_20")

    def test_march_22(self) -> None:
        self._run_and_assert("march_22")

    def test_march_23(self) -> None:
        self._run_and_assert("march_23")


if __name__ == "__main__":
    unittest.main()
