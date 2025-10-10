/**
 * Smart polling utilities for managing API polling intervals
 */

/**
 * Create a polling effect with cleanup
 * @param {Function} fetchFn - Async function to call on each poll
 * @param {number} intervalMs - Polling interval in milliseconds
 * @param {Array} dependencies - Dependencies array (like useEffect)
 * @returns {Function} Cleanup function
 */
export function startPolling(fetchFn, intervalMs, { immediate = true } = {}) {
  // Fetch immediately if requested
  if (immediate) {
    fetchFn().catch(console.error)
  }

  // Set up interval
  const intervalId = setInterval(() => {
    fetchFn().catch(console.error)
  }, intervalMs)

  // Return cleanup function
  return () => clearInterval(intervalId)
}

/**
 * Polling intervals used throughout the app
 */
export const POLLING_INTERVALS = {
  HEALTH_CHECK: 10000,        // 10 seconds
  CIRCUIT_BREAKERS: 5000,     // 5 seconds
  PROJECTS: 30000,            // 30 seconds
  SYSTEM_STATUS: 15000,       // 15 seconds
}
