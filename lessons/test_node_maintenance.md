# test_node_maintenance

## What the test does

Drains a worker or master node (unschedule + drain), then validates cluster functionality by creating PVCs (RBD + CephFS), pods, running IO, and creating OBCs via `Sanity.create_resources()`. After validation, it uncordons the node and checks cluster/Ceph health.

**File**: `tests/functional/z_cluster/nodes/test_nodes_maintenance.py`
**Class**: `TestNodesMaintenance`
**Markers**: `@tier1`, `@skipif_managed_service`, `@skipif_hci_provider`

## Key call chain

1. `init_sanity()` fixture → `Sanity()` (plain, not `SanityProviderMode`)
2. `drain_nodes([typed_node_name])`
3. `wait_for_pods_to_be_running(provis_pod_names)` — waits for CSI provisioner pods
4. `self.sanity_helpers.create_resources(pvc_factory, pod_factory, bucket_factory, rgw_bucket_factory)`
5. `self.sanity_helpers.delete_resources()`
6. `schedule_nodes([typed_node_name])`
7. `self.sanity_helpers.health_check(tries=90)`

## Known issues

- `Sanity.__init__()` skips `self.ceph_cluster = CephCluster()` on HCI client clusters (line 42-43)
- `Sanity.create_resources()` unconditionally calls `self.ceph_cluster.wait_for_noobaa_health_ok()` (line 133)
- This causes constant `AttributeError` on HCI client clusters

## What to look for in logs

- `sanity_helpers.create_resources` log line — confirms the test reached resource creation
- `ResourceNotFoundError` for CSI provisioner pods — indicates pods hadn't rescheduled yet after drain
- OBC creation logs (`objectbucketclaim` Bound status) — confirms bucket_factory succeeded before the crash
- Check kubeconfig paths — provider (`lr4-*`) vs client (`dosypenk-ag-*`) to confirm cluster context

## Platform-specific behavior

- **HCI client clusters**: `ceph_cluster` not initialized → `AttributeError` on `create_resources()` line 133. Constant failure.
- **Standalone / non-HCI**: Works fine, `ceph_cluster` always initialized.

## Past failures

| Date | Classification | Summary |
|------|---------------|---------|
| 2026-02-19 | Test framework issue | `AttributeError: 'Sanity' object has no attribute 'ceph_cluster'` — missing `is_hci_client_cluster()` guard at `sanity_helpers.py:133` |
