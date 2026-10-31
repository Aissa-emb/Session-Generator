import { renderResults, loadResults } from '../shared/page.js';

export function updateFlightResults(state) {
  return renderResults(state);
}

export function mountFlightPage(state) {
  return updateFlightResults(state);
}

export async function fetchFlightResults(state) {
  return await loadResults(state);
}

export async function openFlightRoute(state) {
  return await fetchFlightResults(state);
}

export function initializePage(state) {
  return state.remote ? openFlightRoute(state) : mountFlightPage(state);
}
