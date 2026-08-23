(function exposeRecoveryState(root, factory) {
  const recoveryState = factory();
  if (typeof module !== 'undefined' && module.exports) module.exports = recoveryState;
  root.RecoveryState = recoveryState;
})(typeof globalThis !== 'undefined' ? globalThis : this, function createRecoveryState() {
  const STATE_RANK = {
    idle: 0,
    validating_credentials: 1,
    initializing: 2,
    planning: 3,
    provisioning: 4,
    waiting_for_cloud_init: 5,
    checking_wireguard: 6,
    verifying_egress: 7,
    ready: 8,
    destroying: 9,
    destroyed: 10,
    failed: 10,
    cancelled: 10,
  };

  function selectionAfterRefresh(selectedId, items) {
    if (!selectedId) return null;
    return items.some((item) => item && item.id === selectedId) ? selectedId : null;
  }

  function acceptsBackendRecord(current, incoming, expectedId) {
    if (!incoming || !incoming.id || incoming.id !== expectedId) return false;
    if (!current || current.id !== incoming.id) return true;
    const currentTime = Date.parse(current.updated_at || '');
    const incomingTime = Date.parse(incoming.updated_at || '');
    if (Number.isFinite(currentTime) && !Number.isFinite(incomingTime)) return false;
    if (Number.isFinite(currentTime) && Number.isFinite(incomingTime)) return incomingTime >= currentTime;
    return (STATE_RANK[incoming.state] ?? -1) >= (STATE_RANK[current.state] ?? -1);
  }

  function canRenderLogs(requestedId, displayedId, activeId) {
    if (!requestedId || requestedId !== displayedId) return false;
    return !activeId || requestedId === activeId;
  }

  function cloudInitStatus(record) {
    if (!record || record.state !== 'waiting_for_cloud_init') return null;
    const rawPhase = typeof record.bootstrap_phase === 'string' ? record.bootstrap_phase : '';
    const phase = /^[A-Za-z0-9 /_-]{1,80}$/.test(rawPhase) ? rawPhase : 'startup';
    const rawElapsed = Number(record.provisioning_elapsed_seconds);
    const elapsed = Number.isFinite(rawElapsed) && rawElapsed >= 0 ? Math.floor(rawElapsed) : 0;
    const minutes = Math.floor(elapsed / 60);
    const seconds = String(elapsed % 60).padStart(2, '0');
    return `Cloud init: ${phase} (${minutes}m${seconds}s)`;
  }

  function blockingLegacyStatesForProvider(items, providerId) {
    if (!Array.isArray(items) || typeof providerId !== 'string') return [];
    return items.filter((item) => item && item.provider_id === providerId && item.blocking === true);
  }

  return { acceptsBackendRecord, blockingLegacyStatesForProvider, canRenderLogs, cloudInitStatus, selectionAfterRefresh };
});
