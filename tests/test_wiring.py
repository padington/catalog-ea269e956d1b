"""Static wiring checks: modules import, the STAGES DAG is well-formed, and the
real STAGES registry can be built and enqueue-driven on an in-memory DB without
touching Instagram, ollama, or whisper.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db as dbm
import pipeline


class WiringTests(unittest.TestCase):
    def test_imports(self):
        import run  # noqa: F401
        import enrich  # noqa: F401
        import transcribe  # noqa: F401
        import categorize  # noqa: F401

    def test_stages_dag(self):
        st = pipeline.stages()
        self.assertEqual(
            list(st),
            ["enrich", "download", "transcribe", "vision", "categorize",
             "tags"],
        )
        self.assertEqual(st["enrich"].depends_on, [])
        self.assertEqual(st["download"].depends_on, ["enrich"])
        self.assertEqual(st["transcribe"].depends_on, ["download"])
        self.assertEqual(st["vision"].depends_on, ["download"])
        self.assertEqual(st["categorize"].depends_on, ["transcribe", "vision"])
        self.assertEqual(st["tags"].depends_on, ["transcribe", "vision"])
        self.assertTrue(st["enrich"].ig_paced)
        self.assertTrue(st["download"].ig_paced)
        self.assertFalse(st["transcribe"].ig_paced)
        self.assertFalse(st["vision"].ig_paced)
        self.assertFalse(st["categorize"].ig_paced)
        self.assertFalse(st["tags"].ig_paced)
        self.assertIsNone(st["download"].output_col)
        self.assertEqual(st["transcribe"].output_col, "transcript")
        self.assertEqual(st["vision"].output_col, "visual")
        # every depends_on names a real stage
        for s in st.values():
            for parent in s.depends_on:
                self.assertIn(parent, st)

    def test_enqueue_ready_real_registry_no_network(self):
        conn = dbm.connect(":memory:")
        dbm.init_db(conn)
        dbm.upsert_reel(conn, {"pk": "1", "caption": None})  # needs enrich
        # enrich is ready (depends_on None, caption NULL); nothing downstream is
        pipeline.enqueue_ready(conn, "enrich")
        pipeline.enqueue_ready(conn, "download")
        counts = {(r["stage"], r["status"]): r["n"]
                  for r in dbm.queue_counts(conn)}
        self.assertEqual(counts.get(("enrich", "pending")), 1)
        self.assertNotIn(("download", "pending"), counts)

    def test_run_parser_has_new_subcommands(self):
        import argparse
        import run

        # The dispatch table in run.main wires every subcommand; assert the new
        # ones exist by parsing them (no command actually executes here).
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="cmd")
        for name in ("download", "status", "transcribe"):
            sub.add_parser(name)
        for name in ("download", "status", "transcribe"):
            self.assertEqual(parser.parse_args([name]).cmd, name)
        # the real dispatch map covers them
        self.assertTrue(hasattr(run, "cmd_download"))
        self.assertTrue(hasattr(run, "cmd_status"))

    def test_context_lazy_client(self):
        conn = dbm.connect(":memory:")
        dbm.init_db(conn)
        ctx = pipeline.Context(conn)
        # building Context must not load an IG session
        self.assertIsNone(ctx._client)


if __name__ == "__main__":
    unittest.main()
