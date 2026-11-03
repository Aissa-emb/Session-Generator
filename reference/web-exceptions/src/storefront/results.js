import { renderResults, loadResults } from '../shared/page.js';

export function updateProductResults(state) {
  return renderResults(state);
}

export function mountProductPage(state) {
  return updateProductResults(state);
}

export async function fetchProductResults(state) {
  return await loadResults(state);
}

export async function openProductRoute(state) {
  return await fetchProductResults(state);
}

export function initializePage(state) {
  return state.remote ? openProductRoute(state) : mountProductPage(state);
}
