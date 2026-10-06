import fcntl
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from .core import now


@contextmanager
def try_lock(path):
    """Yield True while holding an exclusive lock file, False if another process holds it.
    A crashed holder releases it automatically."""
    with open(path, "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        # One connection per thread; check_same_thread=False only lets the owner close it later.
        self.db = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
          key TEXT PRIMARY KEY, source TEXT, data TEXT, content_hash TEXT,
          first_seen TEXT, last_seen TEXT, missing INTEGER DEFAULT 0,
          assessment TEXT, assessment_key TEXT, assessed_at TEXT,
          link_state TEXT DEFAULT 'unverified', checked_at TEXT, error TEXT,
          attempts INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS sources (
          id TEXT PRIMARY KEY, attempted_at TEXT, succeeded_at TEXT, count INTEGER, error TEXT
        );
        CREATE TABLE IF NOT EXISTS reviews (
          id INTEGER PRIMARY KEY, job_key TEXT, at TEXT, model TEXT, input_key TEXT,
          duration REAL, usage TEXT, raw TEXT, error TEXT, stage TEXT DEFAULT 'details'
        );
        CREATE TABLE IF NOT EXISTS inference_queue (
          job_key TEXT NOT NULL, input_key TEXT NOT NULL,
          stage TEXT NOT NULL DEFAULT 'overview', overview TEXT,
          status TEXT NOT NULL DEFAULT 'queued', stage_attempts INTEGER NOT NULL DEFAULT 0,
          total_attempts INTEGER NOT NULL DEFAULT 0, failure_rounds INTEGER NOT NULL DEFAULT 0,
          next_retry_at TEXT, error TEXT, updated_at TEXT NOT NULL,
          PRIMARY KEY(job_key,input_key)
        );
        CREATE INDEX IF NOT EXISTS inference_due ON inference_queue(status,next_retry_at);
        CREATE TABLE IF NOT EXISTS service_status (
          component TEXT PRIMARY KEY, data TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS screen_scores (
          job_key TEXT NOT NULL, content_hash TEXT NOT NULL, version TEXT NOT NULL,
          score REAL NOT NULL, scored_at TEXT NOT NULL,
          PRIMARY KEY(job_key, content_hash, version)
        );
        """)
        if "stage" not in {r["name"] for r in self.db.execute("PRAGMA table_info(reviews)")}:
            self.db.execute("ALTER TABLE reviews ADD COLUMN stage TEXT DEFAULT 'details'")
            self.db.commit()
        if "assessed_by" not in {r["name"] for r in self.db.execute("PRAGMA table_info(jobs)")}:
            # Exact weights behind the stored judgment; NULL rows predate this column.
            self.db.execute("ALTER TABLE jobs ADD COLUMN assessed_by TEXT")
            self.db.commit()

    def claim_source_attempt(self, source, minimum_interval_seconds=0):
        """Atomically reserve a provider interval before any network request starts."""
        if minimum_interval_seconds < 0:
            raise ValueError("Source minimum interval must be nonnegative")
        attempted = now()
        eligible_before = (datetime.fromisoformat(attempted) - timedelta(seconds=minimum_interval_seconds)).isoformat(timespec="seconds")
        with self.db:
            claim = self.db.execute("""INSERT INTO sources(id,attempted_at,succeeded_at,count,error)
              VALUES (?,?,NULL,0,NULL) ON CONFLICT(id) DO UPDATE SET attempted_at=excluded.attempted_at
              WHERE sources.attempted_at IS NULL OR julianday(sources.attempted_at)<=julianday(?)""",
              (source,attempted,eligible_before))
        return claim.rowcount == 1

    def snapshot(self, source, jobs, *, membership_mode="snapshot"):
        if membership_mode not in {"snapshot","rolling"}:raise ValueError("Unknown feed membership mode")
        t = now();new = changed = 0
        with self.db:
            if membership_mode=="snapshot":
                self.db.execute("UPDATE jobs SET missing=missing+1 WHERE source=?", (source,))
            for j in jobs:
                old = self.db.execute("SELECT content_hash FROM jobs WHERE key=?", (j.key,)).fetchone()
                self.db.execute("""INSERT INTO jobs(key,source,data,content_hash,first_seen,last_seen)
                  VALUES (?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET
                  data=excluded.data, content_hash=excluded.content_hash, last_seen=excluded.last_seen, missing=0""",
                  (j.key, source, j.model_dump_json(), j.content_hash(), t, t))
                new += old is None
                if old and old["content_hash"] != j.content_hash():
                    changed += 1
                    self.db.execute("UPDATE jobs SET assessment=NULL,assessment_key=NULL,assessed_by=NULL,error=NULL,attempts=0,link_state='unverified',checked_at=NULL WHERE key=?", (j.key,))
            self.db.execute("""INSERT INTO sources VALUES (?,?,?,?,NULL) ON CONFLICT(id) DO UPDATE SET
              attempted_at=excluded.attempted_at,succeeded_at=excluded.succeeded_at,count=excluded.count,error=NULL""", (source,t,t,len(jobs)))
        return {"jobs": len(jobs), "new": new, "changed": changed}

    def source_error(self, source, error):
        with self.db:
            self.db.execute("""INSERT INTO sources VALUES (?,?,NULL,0,?) ON CONFLICT(id) DO UPDATE SET
              attempted_at=excluded.attempted_at,error=excluded.error""", (source,now(),str(error)))

    def task(self, job_key, input_key):
        return self.db.execute("SELECT * FROM inference_queue WHERE job_key=? AND input_key=?", (job_key,input_key)).fetchone()

    def enqueue(self, job_key, input_key):
        return self.enqueue_many([(job_key,input_key)])[(job_key,input_key)]

    def enqueue_many(self, items):
        """Enqueue (job_key, input_key) pairs in one transaction; a full queue scan
        would otherwise commit once per job."""
        t = now()
        with self.db:
            self.db.executemany("INSERT OR IGNORE INTO inference_queue(job_key,input_key,updated_at) VALUES (?,?,?)", [(j,k,t) for j,k in items])
            # Only jobs.assessment stores the current validated result. A completed queue
            # row alone cannot satisfy a task after policy/model/content cycles A -> B -> A.
            self.db.executemany("""UPDATE inference_queue SET stage='overview',overview=NULL,
              status='queued',stage_attempts=0,failure_rounds=0,next_retry_at=NULL,error=NULL,updated_at=?
              WHERE job_key=? AND input_key=? AND (stage='complete' OR status='complete')
              AND EXISTS (SELECT 1 FROM jobs WHERE jobs.key=inference_queue.job_key
                AND jobs.assessment_key IS NOT inference_queue.input_key)""", [(t,j,k) for j,k in items])
        return {(j,k): self.task(j,k) for j,k in items}

    def rows(self):
        return self.db.execute("SELECT * FROM jobs").fetchall()

    def set_status(self, component, **fields):
        """Merge fields into one component's status record; each component has one writer."""
        row = self.db.execute("SELECT data FROM service_status WHERE component=?", (component,)).fetchone()
        data = {**(json.loads(row["data"]) if row else {}), **fields, "updated_at": now()}
        with self.db:
            self.db.execute("INSERT INTO service_status VALUES (?,?) ON CONFLICT(component) DO UPDATE SET data=excluded.data",
                (component, json.dumps(data, ensure_ascii=False)))
        return data

    def statuses(self):
        return {r["component"]: json.loads(r["data"]) for r in self.db.execute("SELECT * FROM service_status ORDER BY component")}

    def screen_scores(self, version):
        """Ordering scores that still describe each job's current content."""
        return dict(self.db.execute("""SELECT s.job_key, s.score FROM screen_scores s JOIN jobs j
          ON j.key=s.job_key AND j.content_hash=s.content_hash WHERE s.version=?""", (version,)).fetchall())

    def save_screen_scores(self, version, items):
        """items: (job_key, content_hash, score) triples from one screening chunk."""
        t = now()
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO screen_scores(job_key,content_hash,version,score,scored_at) VALUES (?,?,?,?,?)",
                [(k, h, version, s, t) for k, h, s in items])
