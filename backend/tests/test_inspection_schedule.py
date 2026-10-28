from datetime import datetime
import unittest


class InspectionScheduleTest(unittest.TestCase):
    def test_daily_next_run_advances_after_current_run(self):
        from app.services.inspection.policy_service import compute_next_run_time

        next_run = compute_next_run_time(
            "daily:02:00",
            "2026-07-15 02:05:00",
            now=datetime(2026, 7, 15, 3, 0),
        )

        self.assertEqual(next_run, datetime(2026, 7, 16, 2, 0))

    def test_weekly_next_run_uses_the_next_selected_weekday(self):
        from app.services.inspection.policy_service import compute_next_run_time

        next_run = compute_next_run_time(
            "weekly:1,3:02:00",
            "2026-07-13 02:05:00",
            now=datetime(2026, 7, 13, 3, 0),
        )

        self.assertEqual(next_run, datetime(2026, 7, 15, 2, 0))

    def test_monthly_next_run_uses_the_next_selected_day(self):
        from app.services.inspection.policy_service import compute_next_run_time

        next_run = compute_next_run_time(
            "monthly:1,15:02:00",
            "2026-07-01 02:05:00",
            now=datetime(2026, 7, 1, 3, 0),
        )

        self.assertEqual(next_run, datetime(2026, 7, 15, 2, 0))

    def test_quarterly_next_run_uses_the_next_selected_day(self):
        from app.services.inspection.policy_service import compute_next_run_time

        next_run = compute_next_run_time(
            "quarterly:2:1,15:02:00",
            "2026-08-01 02:05:00",
            now=datetime(2026, 8, 1, 3, 0),
        )

        self.assertEqual(next_run, datetime(2026, 8, 15, 2, 0))


if __name__ == "__main__":
    unittest.main()
