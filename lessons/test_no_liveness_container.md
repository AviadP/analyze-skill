# test_no_liveness_container

## What It Does
Tests that no liveness probe container exists (details TBD — test never executed in observed run).

## Key Call Chain
- `health_checker` fixture (autouse) → `ceph_health_check_with_toolbox_recovery()` → `ceph_health_check_base()` → `ceph_health_recover()`

## What to Look For
- If failure is in `health_checker` fixture, this test didn't actually run — the cluster was unhealthy before the test started
- Check `ceph_health_recover()` in `ocs_ci/utility/utils.py` for import/runtime errors (this function has had bugs before)
- Check Ceph health status in the traceback — it's printed as the `health_status` argument

## Past Failures

| Date | Classification | Summary |
|------|---------------|---------|
| 2026-02-10 | Test framework issue | `datetime.timedelta` import shadowing in `ceph_health_recover()`. Fixed in PR #14372. |
