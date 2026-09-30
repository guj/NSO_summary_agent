"""One targeted read-only recheck; retain fleet evidence and unresolved errors."""
from nso_facts.health import parse_in_sync


async def recheck_sync(client, initial, devices, *, call):
    effective = dict(initial)
    audit = {}
    for device in sorted(set(devices)):
        original = initial.get(device)
        normalized = str(original or '').lower().replace('_', '-')
        # Locked/admin-disabled devices require operator action, not retries.
        if normalized in {'in-sync', 'out-of-sync', 'locked', 'disabled'}:
            continue
        try:
            response = await call(client, 'check_device_sync', {'device_name': device})
        except Exception as exc:
            response = {'status': 'error', 'error': str(exc)}
        synced = parse_in_sync(response)
        if synced is not None:
            effective[device] = 'in-sync' if synced else 'out-of-sync'
        audit[device] = {'fleet_result': original, 'retry_result': response,
                         'effective_result': effective.get(device) or 'unknown',
                         'resolved': synced is not None}
    return effective, audit
