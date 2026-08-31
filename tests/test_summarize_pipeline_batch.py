from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "summarize_pipeline.py"
SPEC = importlib.util.spec_from_file_location("summarize_pipeline", SCRIPT)
assert SPEC and SPEC.loader
pipeline = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pipeline)


class BatchUploadDateTest(unittest.TestCase):
    def test_probe_persists_and_emits_upload_date_for_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            scratchpad = work_dir / ".scratchpad" / "video_summary"
            pipeline.save_batch(
                scratchpad,
                {
                    "batch_id": "test-batch",
                    "work_dir": str(work_dir),
                    "scratchpad": str(scratchpad),
                    "created_at": 0,
                    "whisper_active": None,
                    "items": [
                        {
                            "url": "https://www.youtube.com/watch?v=sG5aB79TE44",
                            "platform": "youtube",
                            "video_id": "sG5aB79TE44",
                            "title": "",
                            "upload_date": "",
                            "probe_status": "pending",
                            "summary_status": "pending",
                            "ready_for_summary": False,
                        }
                    ],
                },
            )
            probe_payload = {
                "status": "ok",
                "platform": "youtube",
                "video_id": "sG5aB79TE44",
                "title": "How Supabase Became One Of The Fastest Growing DevTool Companies In The World",
                "uploader": "Y Combinator",
                "upload_date": "20260723",
                "duration_string": "31:21",
                "language": "en-US",
                "subtitle_type": "auto",
                "transcript_file": str(scratchpad / "sG5aB79TE44.txt"),
                "webpage_url": "https://www.youtube.com/watch?v=sG5aB79TE44",
            }
            args = argparse.Namespace(
                dir=str(work_dir),
                scratchpad=str(scratchpad),
                force=False,
            )

            with (
                mock.patch.object(pipeline, "lookup", return_value=None),
                mock.patch.object(pipeline, "run_json_script", return_value=(probe_payload, 0)),
                contextlib.redirect_stdout(io.StringIO()),
                self.assertRaises(SystemExit) as exit_context,
            ):
                pipeline.cmd_batch_probe(args)

            self.assertEqual(exit_context.exception.code, 0)
            batch = pipeline.load_batch(scratchpad)
            self.assertEqual(batch["items"][0]["upload_date"], "20260723")

            status = pipeline._batch_status_payload(batch)
            self.assertEqual(status["summarize_ready"][0]["upload_date"], "20260723")
            summarize_action = next(a for a in status["actions"] if a["type"] == "summarize")
            self.assertEqual(summarize_action["upload_date"], "20260723")
            self.assertEqual(status["items"][0]["upload_date"], "20260723")


if __name__ == "__main__":
    unittest.main()
