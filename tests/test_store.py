import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from listener.store import Store  # noqa: E402


def sig(i, score):
    return {"source": "reddit", "kind": "pain", "external_id": str(i), "title": f"t{i}", "text": "x",
            "score": score, "persona": "saas", "matched": [], "query": "q"}


class StoreQueryTests(unittest.TestCase):
    def setUp(self):
        self.st = Store(os.path.join(tempfile.mkdtemp(), "s.db"))
        self.st.upsert_signals([sig(1, 80), sig(2, 10)])
        # Second batch, as if found by a later run.
        self.run_started = int(time.time()) + 5
        self.st.db.execute("UPDATE signals SET first_seen = first_seen - 100")
        self.st.add_run(self.run_started, self.run_started + 1, 2, 10, 0, 1, [])
        self.st.upsert_signals([sig(3, 50), sig(4, 30)])
        self.st.db.execute("UPDATE signals SET first_seen = ? WHERE external_id IN ('3','4')", (self.run_started + 1,))
        self.st.db.commit()

    def test_sort_by_score_and_recent(self):
        self.assertEqual([r["external_id"] for r in self.st.list_signals()], ["1", "3", "4", "2"])
        self.assertEqual([r["external_id"] for r in self.st.list_signals(sort="recent")], ["3", "4", "1", "2"])

    def test_since_and_count(self):
        last = self.st.last_run()
        self.assertEqual(last, {"started": self.run_started, "new": 2})
        rows = self.st.list_signals(since=last["started"])
        self.assertEqual(sorted(r["external_id"] for r in rows), ["3", "4"])
        self.assertEqual(self.st.count_signals(), 4)
        self.assertEqual(self.st.count_signals(min_score=20), 3)
        self.assertEqual(self.st.count_signals(since=last["started"], min_score=40), 1)

    def test_count_by_status_ignores_status_filter(self):
        ids = {r["external_id"]: r["id"] for r in self.st.list_signals()}
        self.st.update_signal(ids["1"], status="pitched")
        self.st.update_signal(ids["3"], status="won")
        counts = self.st.count_by_status(status="new", min_score=20)
        self.assertEqual(counts, {"new": 1, "pitched": 1, "talking": 0, "won": 1, "lost": 0})

    def test_paging(self):
        page1 = self.st.list_signals(limit=2, offset=0)
        page2 = self.st.list_signals(limit=2, offset=2)
        self.assertEqual([r["external_id"] for r in page1 + page2], ["1", "3", "4", "2"])
        self.assertEqual(self.st.stats()["last_run"]["new"], 2)


def usig(i, source, url, **kw):
    s = {"source": source, "kind": "pain", "external_id": f"{source}-{i}", "url": url,
         "title": f"t{i}", "text": "x", "score": 10, "persona": "saas", "matched": [], "query": "q"}
    s.update(kw)
    return s


class StoreUrlMergeTests(unittest.TestCase):
    """The same post arriving via a feed and via an import is one lead."""

    def setUp(self):
        self.st = Store(os.path.join(tempfile.mkdtemp(), "s.db"))

    def test_same_url_different_source_merges(self):
        url = "https://www.reddit.com/r/DesignJobs/comments/abc123/some_post/"
        self.assertEqual(self.st.upsert_signals([usig(1, "rss", url)]), 1)
        # Same post, other source, no trailing slash, tracking param attached.
        twin = usig(2, "import", "https://www.reddit.com/r/DesignJobs/comments/abc123/some_post?utm_source=feed")
        self.assertEqual(self.st.upsert_signals([twin]), 0)
        self.assertEqual(self.st.count_signals(), 1)

    def test_different_urls_do_not_merge(self):
        self.st.upsert_signals([usig(1, "rss", "https://example.com/posts/1")])
        added = self.st.upsert_signals([usig(2, "import", "https://example.com/posts/2")])
        self.assertEqual(added, 1)
        self.assertEqual(self.st.count_signals(), 2)

    def test_merge_preserves_status_and_keeps_richer_fields(self):
        url = "https://example.com/jobs/1"
        self.st.upsert_signals([usig(1, "rss", url, text="short")])
        row = self.st.list_signals()[0]
        self.st.update_signal(row["id"], status="pitched")
        added = self.st.upsert_signals([usig(2, "import", url, text="a much richer body",
                                             author="acme", score=40, matched=["hiring"])])
        self.assertEqual(added, 0)
        merged = self.st.list_signals()[0]
        self.assertEqual(merged["status"], "pitched")  # never downgraded by a merge
        self.assertEqual(merged["text"], "a much richer body")
        self.assertEqual(merged["author"], "acme")  # gap filled from the twin
        self.assertEqual(merged["score"], 40)
        self.assertEqual(merged["matched"], ["hiring"])


if __name__ == "__main__":
    unittest.main()
