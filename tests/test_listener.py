import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from listener import sources  # noqa: E402
from listener.config import Config  # noqa: E402
from listener.scoring import score_signal  # noqa: E402
from listener.store import Store  # noqa: E402

# Shapes copied from https://treg.to/catalog/endpoints/<id> example_response
REDDIT = {"success": True, "posts": [{
    "id": "1v84bpg", "author": "Ask2026_", "subreddit": "SaaS",
    "title": "Organic traffic dropped 40% since AI Overviews - any recommendations?",
    "selftext": "We are a B2B SaaS, ChatGPT doesn't mention us at all. Looking for an agency.",
    "score": 36, "num_comments": 55, "created_utc": 1785167578,
    "permalink": "/r/SaaS/comments/1v84bpg/x/"}], "after": "abc"}
LI_JOBS = {"output": {"data": {"items": [{
    "company": "Acme", "companyUrl": "https://www.linkedin.com/company/acme", "createdUtc": 1789171200,
    "id": "4419969671", "location": "Austin, TX", "title": "Head of Growth",
    "url": "https://www.linkedin.com/jobs/view/4419969671"}]}, "found": True}, "costUsd": 0.0005}
LI_POSTS = {"output": {"data": {"nextCursor": None, "posts": [{
    "authorName": "Jake", "authorUrl": "https://www.linkedin.com/in/jake", "commentCount": 2,
    "createdUtc": 1789483497, "id": "7505637791545020416", "reactionCount": 13,
    "text": "Our shopify store is struggling with AI search", "url": "https://www.linkedin.com/posts/x"}]}}}
X_ROUTED_A = {"output": {"posts": [{"id_str": "111", "full_text": "How do I show up in ChatGPT?",
                                     "user": {"screen_name": "founder1"}, "favorite_count": 20,
                                     "created_at": "Wed Oct 08 12:00:00 +0000 2026"}]},
              "_treg": {"served_by": "tikhub"}}
X_ROUTED_B = {"output": {"posts": [{"id": "222", "text": "giveaway crypto airdrop",
                                     "author": {"username": "spam"}, "createdAt": "2026-10-08T12:00:00Z"}]}}
X_ANYAPI = {"output": {"posts": [{"id": "333", "text": "Need help: ChatGPT doesn't mention us",
                                   "authorUsername": "Joshua_WD", "createdUtc": 1791603471, "likeCount": 2,
                                   "replyCount": 2, "url": "https://x.com/i/web/status/333"}]}}
HN = {"hits": [{"objectID": "42", "story_title": "Ask HN: AI Overviews killed our traffic",
                "comment_text": "<p>Same here &amp; we&#x27;re a startup</p>", "author": "pg",
                "created_at_i": 1789000000, "points": 10}]}
FUNDING = {"ok": True, "data": [{"company_name": "Example Robotics", "amount": 12000000, "currency": "USD",
                                 "round": "Series A", "announced_date": "2026-09-01", "region": "US"}]}


class ParserTests(unittest.TestCase):
    def test_reddit(self):
        [s] = sources.parse_reddit(REDDIT, "q")
        self.assertEqual(s["external_id"], "1v84bpg")
        self.assertEqual(s["url"], "https://www.reddit.com/r/SaaS/comments/1v84bpg/x/")
        self.assertEqual(s["community"], "r/SaaS")
        self.assertEqual(s["engagement"], 91)

    def test_linkedin(self):
        [j] = sources.parse_linkedin_jobs(LI_JOBS, "Head of Growth")
        self.assertEqual((j["kind"], j["author"]), ("hiring", "Acme"))
        [p] = sources.parse_linkedin_posts(LI_POSTS, "q")
        self.assertEqual((p["author"], p["engagement"]), ("Jake", 15))

    def test_x_multiple_provider_shapes(self):
        [a] = sources.parse_x(X_ROUTED_A, "q")
        self.assertEqual(a["url"], "https://x.com/founder1/status/111")
        self.assertIsNotNone(a["created_utc"])
        [b] = sources.parse_x(X_ROUTED_B, "q")
        self.assertEqual((b["author"], b["external_id"]), ("spam", "222"))
        [c] = sources.parse_x(X_ANYAPI, "q")
        self.assertEqual((c["author"], c["url"], c["engagement"]), ("Joshua_WD", "https://x.com/Joshua_WD/status/333", 4))
        self.assertEqual(c["created_utc"], 1791603471)

    def test_hn_and_funding(self):
        [h] = sources.parse_hn(HN, "q")
        self.assertEqual(h["text"], "Same here & we're a startup")
        self.assertEqual(h["title"], "")
        self.assertEqual(h["community"], "HN comment · Ask HN: AI Overviews killed our traffic")
        [f] = sources.parse_funding(FUNDING)
        self.assertIn("Series A", f["title"])
        self.assertEqual(f["kind"], "funding")

    def test_empty_and_garbage(self):
        self.assertEqual(sources.parse_reddit({"posts": None}, "q"), [])
        self.assertEqual(sources.parse_x({"output": {"posts": [None, {"id": "1"}]}}, "q"), [])


class ScoringTests(unittest.TestCase):
    def test_pain_beats_spam(self):
        pain = sources.parse_reddit(REDDIT, "q")[0]
        spam = sources.parse_x(X_ROUTED_B, "q")[0]
        sp, persona, matched = score_signal(pain)
        ss, _, _ = score_signal(spam)
        self.assertGreater(sp, 50)
        self.assertEqual(persona, "saas")
        self.assertIn("doesn't mention", matched)
        self.assertEqual(ss, 0)

    def test_dtc_persona(self):
        _, persona, _ = score_signal(sources.parse_linkedin_posts(LI_POSTS, "q")[0])
        self.assertEqual(persona, "dtc")


class StoreConfigTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_dedupe_and_status(self):
        st = Store(os.path.join(self.dir, "t.db"))
        sig = sources.parse_reddit(REDDIT, "q")
        self.assertEqual(st.upsert_signals(sig), 1)
        self.assertEqual(st.upsert_signals(sig), 0)
        row = st.list_signals()[0]
        st.update_signal(row["id"], status="pitched", note="good")
        self.assertEqual(st.list_signals(status="pitched")[0]["note"], "good")
        with self.assertRaises(ValueError):
            st.update_signal(row["id"], status="bogus")
        self.assertIn("Organic traffic", st.export("csv"))

    def test_token_masking(self):
        os.environ.pop("TREG_TOKEN", None)
        cfg = Config(os.path.join(self.dir, "config.json"))
        cfg.update({"treg_token": "FAKE-TEST-TOKEN-not-a-real-key-0000"})
        masked = cfg.public()["treg_token"]
        self.assertNotIn("not-a-real-key", masked)
        cfg.update({"treg_token": masked, "lookback_days": 3})  # masked echo must not overwrite
        self.assertEqual(cfg.token(), "FAKE-TEST-TOKEN-not-a-real-key-0000")
        self.assertEqual(Config(cfg.path).data["lookback_days"], 3)


if __name__ == "__main__":
    unittest.main()
