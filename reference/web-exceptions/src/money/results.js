import { renderResults, loadResults } from '../shared/page.js';

export function updateAccountResults(state) {
  return renderResults(state);
}

export function mountAccountPage(state) {
  return updateAccountResults(state);
}

export async function fetchAccountResults(state) {
  return await loadResults(state);
}

export async function openAccountRoute(state) {
  return await fetchAccountResults(state);
}

export function initializePage(state) {
  return state.remote ? openAccountRoute(state) : mountAccountPage(state);
}
