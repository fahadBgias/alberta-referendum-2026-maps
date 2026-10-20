"""Tests for the data scripts. Run from the repo root: python -m unittest discover tests"""
from __future__ import annotations

import csv
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import build_geometry  # noqa: E402
import fetch_results  # noqa: E402
from mock_feed import MockFeed  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
SNAPSHOT = json.loads((FIXTURES / "feed_snapshot.json").read_text())


def run(feed: MockFeed, data_dir: Path) -> tuple[int, str]:
    buf = io.StringIO()
    with mock.patch.object(fetch_results.time, "sleep"), redirect_stdout(buf):
        code = fetch_results.main(["--base-url", feed.url, "--data-dir", str(data_dir)])
    return code, buf.getvalue()


class GeometryTests(unittest.TestCase):
    def test_reproduces_published_shapes(self):
        sample = json.loads((FIXTURES / "geometry_sample.json").read_text())
        published = json.loads((ROOT / "data" / "geometry.json").read_text())["f"]
        E, C = sample["E"], sample["C"]
        self.assertEqual(build_geometry.to_path(sample["ed1"], E[0], E[3], 2000), published["1"][0])
        self.assertEqual(build_geometry.to_path(sample["ed1"], C[0], C[3], 100), published["1"][1])
        # Grande Prairie-Wapiti has a hole (the city of Grande Prairie): two rings in one path
        self.assertEqual(build_geometry.to_path(sample["ed64"], E[0], E[3], 2000), published["64"][0])

    def test_rounds_half_up_like_javascript(self):
        self.assertEqual(build_geometry.js_round(0.5), 1)
        self.assertEqual(build_geometry.js_round(2.5), 3)
        self.assertEqual(build_geometry.js_round(-0.5), 0)


class FetchTests(unittest.TestCase):
    def check_output(self, data_dir: Path):
        out = json.loads((data_dir / "results.json").read_text())
        for n in range(1, 11):
            self.assertEqual(out["questions"][str(n)]["votes"], SNAPSHOT["questions"][str(n)], f"Q{n}")
        self.assertEqual(out["updated"], "2026-10-20T18:51:21Z")
        self.assertEqual(out["questions"]["10"]["a"], "Remain")
        self.assertEqual(out["warnings"], [])
        # Calgary-Varsity Q1 and Grande Prairie-Wapiti Q4-Q7 exceed their Q10 ballots in this snapshot
        self.assertIn({"q": 1, "ed": 25, "total": 48256, "ballots": 20288}, out["flags"])
        with open(data_dir / "results.csv", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 870)
        varsity_q1 = next(r for r in rows if r["division_number"] == "25" and r["question"] == "1")
        self.assertEqual(varsity_q1["data_flag"], "votes exceed Q10 ballots")
        return out

    def test_round_trip(self):
        with MockFeed(SNAPSHOT) as feed, tempfile.TemporaryDirectory() as tmp:
            code, _ = run(feed, Path(tmp))
            self.assertEqual(code, 0)
            self.check_output(Path(tmp))
            self.assertNotIn("/8400", feed.requests, "should not need the results page when the feed answers")

    def test_reads_version_from_page_after_a_version_bump(self):
        with MockFeed(SNAPSHOT, require_version=True) as feed, tempfile.TemporaryDirectory() as tmp:
            code, log = run(feed, Path(tmp))
            self.assertEqual(code, 0, log)
            self.assertIn("/8400", feed.requests)
            self.assertIn("version=3.3.1", feed.requests[-1])
            self.check_output(Path(tmp))

    def test_unchanged_count_writes_nothing(self):
        with MockFeed(SNAPSHOT) as feed, tempfile.TemporaryDirectory() as tmp:
            run(feed, Path(tmp))
            before = (Path(tmp) / "history.csv").read_text()
            code, log = run(feed, Path(tmp))
            self.assertEqual(code, 0)
            self.assertIn("No change", log)
            self.assertEqual((Path(tmp) / "history.csv").read_text(), before)

    def test_broken_feed_leaves_existing_files_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            with MockFeed(SNAPSHOT) as good:
                run(good, Path(tmp))
            before = (Path(tmp) / "results.json").read_text()
            with MockFeed(SNAPSHOT, broken=True) as bad:
                code, log = run(bad, Path(tmp))
            self.assertEqual(code, 1)
            self.assertIn("::error::", log)
            self.assertEqual((Path(tmp) / "results.json").read_text(), before)


if __name__ == "__main__":
    unittest.main()
