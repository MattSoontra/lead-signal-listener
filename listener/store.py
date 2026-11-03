"""SQLite storage with de-duplication on (source, external_id), plus a
canonical-URL merge so the same post arriving via two sources stays one row."""
import csv
import io
import json
import sqlite3
import threading
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

STATUSES = ("new", "pitched", "talking", "won", "lost")

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL, kind TEXT NOT NULL, external_id TEXT NOT NULL,
  url TEXT, url_key TEXT, title TEXT, text TEXT, author TEXT, author_url TEXT, community TEXT,
  created_utc INTEGER, engagement INTEGER DEFAULT 0, query TEXT,
  score INTEGER DEFAULT 0, persona TEXT, matched TEXT,
  status TEXT DEFAULT 'new', note TEXT DEFAULT '',
  first_seen INTEGER NOT NULL,
  UNIQUE(source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_signals_score ON signals(score DESC);
CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started INTEGER, finished INTEGER, new_signals INTEGER DEFAULT 0,
  fetched INTEGER DEFAULT 0, cost_micro INTEGER DEFAULT 0, calls INTEGER DEFAULT 0, log TEXT
);
"""

EXPORT_FIELDS = ["id", "score", "persona", "status", "source", "kind", "community", "author", "author_url",
                 "title", "text", "url", "query", "matched", "engagement", "created_utc", "note"]


def canonical_url(url):
    """Stable key for 'same post, different feed' URL matching, or None.

    Conservative: lowercase scheme/host, drop the fragment and utm_* tracking
    params, ignore a trailing slash. Query params that identify the post stay.
    """
    if not url:
        return None
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if not parts.netloc:
        return None
    path = parts.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                       if not k.lower().startswith("utm_")])
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, ""))


class Store:
    def __init__(self, path):
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._ensure_url_key()
        self.db.commit()

    def _ensure_url_key(self):
        """Add + backfill url_key on databases created before it existed."""
        cols = [r[1] for r in self.db.execute("PRAGMA table_info(signals)")]
        if "url_key" not in cols:
            self.db.execute("ALTER TABLE signals ADD COLUMN url_key TEXT")
        rows = self.db.execute("SELECT id, url FROM signals WHERE url IS NOT NULL AND url_key IS NULL").fetchall()
        for r in rows:
            self.db.execute("UPDATE signals SET url_key = ? WHERE id = ?", (canonical_url(r["url"]), r["id"]))
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_signals_url_key ON signals(url_key)")

    def upsert_signals(self, signals):
        """Insert unseen signals; returns number of new rows.

        A signal whose canonical URL already exists merges into that row
        (see _merge) instead of duplicating it under another source.
        """
        new = 0
        now = int(time.time())
        with self.lock:
            for s in signals:
                url_key = canonical_url(s.get("url"))
                row = None
                if url_key:
                    row = self.db.execute("SELECT * FROM signals WHERE url_key = ?", (url_key,)).fetchone()
                if row is not None:
                    self._merge(row, s)
                    continue
                cur = self.db.execute(
                    """INSERT OR IGNORE INTO signals
                       (source, kind, external_id, url, url_key, title, text, author, author_url, community,
                        created_utc, engagement, query, score, persona, matched, first_seen)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (s["source"], s["kind"], s["external_id"], s.get("url"), url_key, s.get("title"), s.get("text"),
                     s.get("author"), s.get("author_url"), s.get("community"), s.get("created_utc"),
                     s.get("engagement", 0), s.get("query"), s.get("score", 0), s.get("persona"),
                     json.dumps(s.get("matched", []), ensure_ascii=False), now))
                new += cur.rowcount
            self.db.commit()
        return new

    def _merge(self, row, s):
        """Fold an incoming duplicate into an existing row: fill gaps, keep the
        richer text/score, never touch status or note (a human may have
        already worked the lead)."""
        upd = {}
        for col in ("title", "author", "author_url", "community", "query", "persona"):
            if not row[col] and s.get(col):
                upd[col] = s[col]
        if s.get("created_utc") and not row["created_utc"]:
            upd["created_utc"] = s["created_utc"]
        if s.get("text") and len(s["text"]) > len(row["text"] or ""):
            upd["text"] = s["text"]
        if (s.get("engagement") or 0) > (row["engagement"] or 0):
            upd["engagement"] = s["engagement"]
        if (s.get("score") or 0) > (row["score"] or 0):
            upd["score"] = s["score"]
        try:
            matched = json.loads(row["matched"] or "[]")
        except json.JSONDecodeError:
            matched = []
        extra = [m for m in (s.get("matched") or []) if m not in matched]
        if extra:
            upd["matched"] = json.dumps(matched + extra, ensure_ascii=False)
        if upd:
            cols = ", ".join(f"{c} = ?" for c in upd)
            self.db.execute(f"UPDATE signals SET {cols} WHERE id = ?", (*upd.values(), row["id"]))

    @staticmethod
    def _where(status=None, source=None, persona=None, min_score=0, q=None, since=None):
        sql, args = " WHERE score >= ?", [int(min_score or 0)]
        for col, val in (("status", status), ("source", source), ("persona", persona)):
            if val:
                sql += f" AND {col} = ?"
                args.append(val)
        if q:
            sql += " AND (title LIKE ? OR text LIKE ? OR author LIKE ?)"
            args += [f"%{q}%"] * 3
        if since:
            sql += " AND first_seen >= ?"
            args.append(int(since))
        return sql, args

    def list_signals(self, limit=200, offset=0, sort="score", **filters):
        where, args = self._where(**filters)
        # "recent" = newest first by when we first saw the lead; "score" = best leads first.
        order = " ORDER BY first_seen DESC, score DESC" if sort == "recent" else " ORDER BY score DESC, first_seen DESC"
        with self.lock:
            rows = self.db.execute("SELECT * FROM signals" + where + order + " LIMIT ? OFFSET ?",
                                   args + [int(limit), int(offset)]).fetchall()
        return [_row(r) for r in rows]

    def count_signals(self, **filters):
        where, args = self._where(**filters)
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM signals" + where, args).fetchone()[0]

    def count_by_status(self, **filters):
        """Lead counts per status under the other filters (the status filter itself is ignored)."""
        filters.pop("status", None)
        where, args = self._where(**filters)
        with self.lock:
            rows = self.db.execute("SELECT status, COUNT(*) FROM signals" + where + " GROUP BY status", args).fetchall()
        counts = {s: 0 for s in STATUSES}
        counts.update({r[0]: r[1] for r in rows if r[0] in counts})
        return counts

    def last_run(self):
        """Most recent run that has finished: {started, new} or None."""
        with self.lock:
            r = self.db.execute("SELECT started, new_signals FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return {"started": r[0], "new": r[1]} if r else None

    def update_signal(self, sid, status=None, note=None):
        if status is not None and status not in STATUSES:
            raise ValueError(f"invalid status: {status}")
        with self.lock:
            if status is not None:
                self.db.execute("UPDATE signals SET status=? WHERE id=?", (status, sid))
            if note is not None:
                self.db.execute("UPDATE signals SET note=? WHERE id=?", (note, sid))
            self.db.commit()

    def rescore_all(self, scorer):
        with self.lock:
            rows = self.db.execute("SELECT id, kind, title, text, community, engagement FROM signals").fetchall()
            for r in rows:
                score, persona, matched = scorer(dict(r))
                self.db.execute("UPDATE signals SET score=?, persona=?, matched=? WHERE id=?",
                                (score, persona, json.dumps(matched, ensure_ascii=False), r["id"]))
            self.db.commit()
        return len(rows)

    def stats(self):
        with self.lock:
            by_status = dict(self.db.execute("SELECT status, COUNT(*) FROM signals GROUP BY status").fetchall())
            by_source = dict(self.db.execute("SELECT source, COUNT(*) FROM signals GROUP BY source").fetchall())
            spent = self.db.execute("SELECT COALESCE(SUM(cost_micro),0), COALESCE(SUM(calls),0) FROM runs").fetchone()
            month_start = int(time.time()) - 30 * 86400
            spent30 = self.db.execute("SELECT COALESCE(SUM(cost_micro),0) FROM runs WHERE started >= ?",
                                      (month_start,)).fetchone()[0]
        last = self.last_run()
        return {"by_status": by_status, "by_source": by_source, "total": sum(by_status.values()), "last_run": last,
                "spent_usd": spent[0] / 1e6, "spent_30d_usd": spent30 / 1e6, "calls": spent[1]}

    def add_run(self, started, finished, new_signals, fetched, cost_micro, calls, log):
        with self.lock:
            self.db.execute("INSERT INTO runs (started, finished, new_signals, fetched, cost_micro, calls, log)"
                            " VALUES (?,?,?,?,?,?,?)",
                            (started, finished, new_signals, fetched, cost_micro, calls, "\n".join(log)))
            self.db.commit()

    def recent_runs(self, limit=10):
        with self.lock:
            rows = self.db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def export(self, fmt="csv", **filters):
        rows = self.list_signals(limit=100000, **filters)
        if fmt == "json":
            return json.dumps(rows, ensure_ascii=False, indent=2)
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=EXPORT_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            r = dict(r)
            r["matched"] = ", ".join(r.get("matched") or [])
            w.writerow(r)
        return "﻿" + buf.getvalue()  # BOM so Excel opens UTF-8 correctly


def _row(r):
    d = dict(r)
    try:
        d["matched"] = json.loads(d.get("matched") or "[]")
    except json.JSONDecodeError:
        d["matched"] = []
    return d
