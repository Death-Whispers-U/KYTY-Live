import unittest

from kyty_live.snapshots import AutoSnapshots, snapshot_name


class SnapshotNameTest(unittest.TestCase):
    def test_manual_name_is_unchanged(self):
        self.assertEqual(snapshot_name(12.34), "snap-00012.3s.png")

    def test_reason_is_appended(self):
        self.assertEqual(snapshot_name(5, "auto"), "snap-00005.0s-auto.png")
        self.assertEqual(snapshot_name(41.06, "device_lost"), "snap-00041.1s-device_lost.png")


class AutoSnapshotsTest(unittest.TestCase):
    def test_off_never_due(self):
        a = AutoSnapshots(every=0, keep=5)
        self.assertFalse(a.due(100))

    def test_due_every_interval(self):
        a = AutoSnapshots(every=5, keep=5)
        self.assertFalse(a.due(4.9))
        self.assertTrue(a.due(5.0))
        a.taken(5.0, "snap-00005.0s-auto.png")
        self.assertFalse(a.due(9.9))
        self.assertTrue(a.due(10.0))

    def test_a_late_capture_does_not_cause_a_burst(self):
        a = AutoSnapshots(every=5, keep=5)
        a.taken(23.0, "snap-00023.0s-auto.png")  # the window only appeared late
        self.assertFalse(a.due(25.0))
        self.assertTrue(a.due(28.0))

    def test_keeps_only_the_newest_periodic_snapshots(self):
        a = AutoSnapshots(every=5, keep=2)
        self.assertEqual(a.taken(5, "a.png"), [])
        self.assertEqual(a.taken(10, "b.png"), [])
        self.assertEqual(a.taken(15, "c.png"), ["a.png"])
        self.assertEqual(a.taken(20, "d.png"), ["b.png"])

    def test_event_snapshots_are_limited_per_run(self):
        a = AutoSnapshots(every=5, keep=2, max_events=2)
        self.assertTrue(a.claim_event())
        self.assertTrue(a.claim_event())
        self.assertFalse(a.claim_event())

    def test_triggers(self):
        self.assertTrue(AutoSnapshots.triggers({"kind": "device_lost"}))
        self.assertTrue(AutoSnapshots.triggers({"kind": "fatal"}))
        self.assertTrue(AutoSnapshots.triggers({"kind": "host_exception"}))
        self.assertFalse(AutoSnapshots.triggers({"kind": "device_fault"}))  # follows device_lost
        self.assertFalse(AutoSnapshots.triggers({"kind": "missing_import"}))

    def test_rejects_bad_settings(self):
        with self.assertRaises(ValueError):
            AutoSnapshots(every=-1, keep=5)
        with self.assertRaises(ValueError):
            AutoSnapshots(every=0.5, keep=5)
        with self.assertRaises(ValueError):
            AutoSnapshots(every=5, keep=0)


if __name__ == "__main__":
    unittest.main()
