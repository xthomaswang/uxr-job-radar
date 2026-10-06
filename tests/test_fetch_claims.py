from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock

from uxr_radar.pipeline import fetch_all
from uxr_radar.store import Store


def test_concurrent_fetches_share_atomic_provider_interval(tmp_path,monkeypatch):
    path=tmp_path/"jobs.db"
    Store(path).db.close()
    start=Barrier(2);request_started=Event();release_request=Event();worker_finished=Event()
    calls=[];calls_lock=Lock()
    source={"id":"public-api","min_fetch_interval_seconds":21600}

    def fetch(client,source):
        with calls_lock:calls.append(source["id"])
        request_started.set()
        assert release_request.wait(5)
        return [],{"jobs":[]}

    monkeypatch.setattr("uxr_radar.pipeline.fetch_feed",fetch)

    def worker(index):
        store=Store(path)
        try:
            start.wait(timeout=5)
            fetch_all(store,[source],tmp_path/f"raw-{index}")
        finally:
            store.db.close()
            worker_finished.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(worker,i) for i in range(2)]
        try:
            assert request_started.wait(5)
            # The worker whose claim loses must return while the first request
            # is still running; a read-then-write interval check fails this.
            assert worker_finished.wait(5)
        finally:release_request.set()
        for future in futures:future.result()
    assert calls==["public-api"]


def test_expired_claim_preserves_last_good_snapshot_metadata(tmp_path):
    store=Store(tmp_path/"jobs.db")
    store.db.execute("INSERT INTO sources VALUES (?,?,?,?,?)",("public-api","2000-01-01T00:00:00+00:00","2000-01-01T00:00:00+00:00",42,"previous_error"))
    store.db.commit()
    assert store.claim_source_attempt("public-api",21600)
    row=store.db.execute("SELECT * FROM sources").fetchone()
    assert row["succeeded_at"]=="2000-01-01T00:00:00+00:00"
    assert row["count"]==42 and row["error"]=="previous_error"
    assert not store.claim_source_attempt("public-api",21600)


def test_failed_request_still_consumes_polling_interval(tmp_path,monkeypatch):
    store=Store(tmp_path/"jobs.db");calls=[]
    def fetch(client,source):
        calls.append(source["id"])
        raise RuntimeError("Temporary upstream failure")
    monkeypatch.setattr("uxr_radar.pipeline.fetch_feed",fetch)
    sources=[{"id":"public-api","min_fetch_interval_seconds":21600}]
    fetch_all(store,sources,tmp_path/"raw")
    fetch_all(store,sources,tmp_path/"raw")
    assert calls==["public-api"]
    assert store.db.execute("SELECT error FROM sources").fetchone()[0]=="Temporary upstream failure"
