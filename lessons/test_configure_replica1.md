# test_configure_replica1

## What the test does

1. Patches StorageCluster to enable `cephNonResilientPools`
2. Waits for StorageCluster READY (instant — already Ready before patch)
3. Waits for replica-1 OSDs via `wait_for_replica1_osds(timeout=150)`
4. Creates pod on failure domain, runs FIO, verifies PG/KB distribution

## Key call chain

- `test_replica1.py:346` → `replica1_setup()`
- `test_replica1.py:159` → `set_non_resilient_pool()` (patches CR)
- `test_replica1.py:162` → `wait_for_resource(STATUS_READY)` (no-op, see below)
- `test_replica1.py:165` → `wait_for_replica1_osds(timeout=150)`
- `replica_one.py:180` → `TimeoutSampler` calling `get_replica_1_osds()`

## Known timing sensitivities

- Enabling `cephNonResilientPools` triggers a **rook-ceph-operator restart**
- Operator restart adds ~30s dead time before reconciliation begins
- Full pipeline (restart → CephBlockPool creation → OSD prepare → OSD pods) takes 2-3 min on vSphere
- Test overrides default 300s timeout to 150s — tight on slow platforms
- `wait_for_resource(STATUS_READY)` is a no-op: StorageCluster stays Ready from previous reconciliation

## What to check in logs

- `get_replica_1_osds` output — if `{}` every poll, OSDs never created in time
- rook-ceph-operator pod AGE — if young, operator was restarted during test
- CephBlockPoolRadosNamespace events — excessive requeuing indicates slow reconciliation
- `ODFNodeLatencyHighOnOSDNodes` alert — high node latency slows everything
- OSD prepare job completion time — vSphere PVC provisioning is the bottleneck

## Past failures

| Date | Classification | Summary |
|------|---------------|---------|
| 2026-02-18 | test framework issue | 150s timeout too short on vSphere with high latency, OSDs came up 1s after timeout |
