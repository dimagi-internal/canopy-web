import fakeredis

from canopy_sdk.ondemand import Job, JobStore


def _r():
    return fakeredis.FakeRedis(decode_responses=True)


def test_job_roundtrip():
    js = JobStore(_r(), "emod")
    j = js.create("run_pmc")
    assert js.get(j.job_id).status == "running"
    js.complete(j.job_id, {"ok": 1})
    got = js.get(j.job_id)
    assert got.status == "completed" and got.result == {"ok": 1}
    assert got.completed_at is not None


def test_fail_records_error_and_code_and_clears_result():
    js = JobStore(_r(), "emod")
    j = js.create("op")
    js.fail(j.job_id, "boom", code="E1")
    got = js.get(j.job_id)
    assert (got.status, got.error, got.error_code, got.result) == ("failed", "boom", "E1", None)


def test_get_missing_is_none():
    assert JobStore(_r(), "emod").get("nope") is None


def test_to_dict_omits_none_optionals():
    j = Job("abc", "op", "running", "2026-01-01T00:00:00+00:00")
    assert j.to_dict() == {
        "job_id": "abc", "operation": "op", "status": "running",
        "started_at": "2026-01-01T00:00:00+00:00",
    }
    f = Job("abc", "op", "failed", "t", completed_at="t2", error="e", error_code="C")
    assert f.to_dict()["error_code"] == "C" and "result" not in f.to_dict()


def test_prefix_override_and_default_key_and_ttl():
    r = _r()
    js = JobStore(r, "emod", ttl_s=50)
    j = js.create("op")
    assert r.get(f"ondemand:emod:job:{j.job_id}") and 0 < r.ttl(f"ondemand:emod:job:{j.job_id}") <= 50
    js2 = JobStore(r, "mobile", prefix="mobile:job:")
    j2 = js2.create("op")
    assert r.get(f"mobile:job:{j2.job_id}")


def test_complete_after_expiry_recreates_record():
    js = JobStore(_r(), "emod")
    js.complete("gone", {"ok": 1})
    got = js.get("gone")
    assert got.status == "completed" and got.result == {"ok": 1}
