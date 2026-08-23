(function exposeSessionCost(root, factory) {
  const sessionCost = factory();
  if (typeof module !== 'undefined' && module.exports) module.exports = sessionCost;
  root.SessionCost = sessionCost;
})(typeof globalThis !== 'undefined' ? globalThis : this, function createSessionCost() {
  function timestamp(value) {
    const parsed = Date.parse(value || '');
    return Number.isFinite(parsed) ? parsed : null;
  }

  function estimate(record, nowMs = Date.now()) {
    const startedAt = timestamp(record?.apply_started_at);
    if (startedAt === null) return { elapsedSeconds: null, costUsd: null, finalized: false };
    const destroyedAt = timestamp(record?.destroyed_at)
      ?? (record?.state === 'destroyed' ? timestamp(record?.updated_at) : null);
    const end = Math.max(startedAt, destroyedAt ?? nowMs);
    const elapsedSeconds = Math.max(0, Math.floor((end - startedAt) / 1000));
    const rawRate = record?.estimated_hourly_cost_usd;
    const rate = rawRate === null || rawRate === undefined || rawRate === '' ? Number.NaN : Number(rawRate);
    const costUsd = Number.isFinite(rate) && rate >= 0 ? (elapsedSeconds / 3600) * rate : null;
    return { elapsedSeconds, costUsd, finalized: destroyedAt !== null };
  }

  function formatDuration(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return '—';
    const whole = Math.floor(seconds);
    const hours = Math.floor(whole / 3600);
    const minutes = Math.floor((whole % 3600) / 60);
    const remainder = whole % 60;
    if (hours) return `${hours}h ${String(minutes).padStart(2, '0')}m ${String(remainder).padStart(2, '0')}s`;
    return `${String(minutes).padStart(2, '0')}m ${String(remainder).padStart(2, '0')}s`;
  }

  function formatCost(costUsd) {
    if (!Number.isFinite(costUsd) || costUsd < 0) return 'Unavailable';
    if (costUsd < 0.001) return `$${costUsd.toFixed(5)}`;
    if (costUsd < 0.01) return `$${costUsd.toFixed(4)}`;
    if (costUsd < 1) return `$${costUsd.toFixed(3)}`;
    return `$${costUsd.toFixed(2)}`;
  }

  return { estimate, formatCost, formatDuration };
});
