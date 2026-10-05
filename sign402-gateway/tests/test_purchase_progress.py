"""The stage of a purchase the page waits on: marked where it is reached, seen only by its account."""
import unittest
from unittest.mock import patch

from sign402_gateway import purchase_progress as pp

ATTEMPT = "a" * 32
ACCOUNT = "solana:owner"


class ProgressTests(unittest.TestCase):
    def setUp(self):
        pp._stages.clear()

    def test_stages_are_marked_inside_the_attempt_only(self):
        pp.mark("paying")  # outside any attempt: nowhere to go
        self.assertEqual(pp._stages, {})
        with pp.tracking(ACCOUNT, ATTEMPT):
            self.assertEqual(pp.stage_of(ACCOUNT, ATTEMPT), "ordering")
            pp.mark("paying")
            self.assertEqual(pp.stage_of(ACCOUNT, ATTEMPT), "paying")
            pp.mark("delivered-by-magic")  # not a stage
            self.assertEqual(pp.stage_of(ACCOUNT, ATTEMPT), "paying")
        pp.mark("paid")  # the attempt is over: no more marks
        self.assertEqual(pp.stage_of(ACCOUNT, ATTEMPT), "paying")

    def test_only_its_own_account_sees_an_attempt_and_odd_ids_see_nothing(self):
        with pp.tracking(ACCOUNT, ATTEMPT):
            pp.mark("paid")
        self.assertEqual(pp.stage_of("solana:someone-else", ATTEMPT), "working")
        for odd in ("", None, "A" * 32, "a" * 31, "../" + "a" * 29):
            self.assertEqual(pp.stage_of(ACCOUNT, odd), "working")
            with pp.tracking(ACCOUNT, odd):
                pp.mark("paid")
        self.assertEqual(len(pp._stages), 1)

    def test_old_attempts_are_forgotten(self):
        with patch.object(pp.time, "time", return_value=1000.0), pp.tracking(ACCOUNT, ATTEMPT):
            pp.mark("paid")
        with patch.object(pp.time, "time", return_value=1000.0 + pp.KEEP_SECONDS + 1):
            self.assertEqual(pp.stage_of(ACCOUNT, ATTEMPT), "working")
            with pp.tracking(ACCOUNT, "b" * 32):
                pass
        self.assertNotIn((ACCOUNT, ATTEMPT), pp._stages)


if __name__ == "__main__":
    unittest.main()
