(function exposeRecoveryState(root, factory) {
  const recoveryState = factory();
  if (typeof module !== 'undefined' && module.exports) module.exports = recoveryState;
  root.RecoveryState = recoveryState;
})(typeof globalThis !== 'undefined' ? globalThis : this, function createRecoveryState() {
  function selectionAfterRefresh(selectedId, items) {
    if (!selectedId) return null;
    return items.some((item) => item && item.id === selectedId) ? selectedId : null;
  }

  return { selectionAfterRefresh };
});
