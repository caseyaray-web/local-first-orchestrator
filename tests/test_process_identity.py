from __future__ import annotations

import os
import unittest

from local_first_orchestrator.native_release_approval import (
    linux_process_identity,
    same_linux_process_identity,
)


class ProcessIdentityTests(unittest.TestCase):
    def test_live_identity_is_reproducible_and_binds_pid_start_ticks_and_command(self):
        identity = linux_process_identity(os.getpid())
        self.assertEqual(identity["pid"], os.getpid())
        self.assertTrue(identity["start_ticks"] >= 0)
        self.assertTrue(identity["exe"])
        self.assertTrue(identity["cmdline"])
        self.assertTrue(same_linux_process_identity(identity))

    def test_dead_pid_is_rejected(self):
        with self.assertRaises(ValueError):
            linux_process_identity(999999)

    def test_pid_reuse_or_identity_drift_is_rejected(self):
        identity = linux_process_identity(os.getpid())
        identity["start_ticks"] += 1
        self.assertFalse(same_linux_process_identity(identity))
        identity = linux_process_identity(os.getpid())
        identity["cmdline"] = "00"
        self.assertFalse(same_linux_process_identity(identity))


if __name__ == "__main__":
    unittest.main()
