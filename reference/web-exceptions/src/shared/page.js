import { readPrice, formatPrice, buildPriceRows, saveRecentlyViewed, hydrateResults } from './catalog.js';
import { requestCollection, cancelRequest } from './client.js';
import { importRoute, loadRouteChunk } from './router.js';

export function renderResults(state) {
  switch (state.operation) {
    case 'property_undefined': return buildPriceRows(state.records, readPrice);
    case 'not_a_function': return buildPriceRows(state.records, formatPrice);
    case 'quota_exceeded': return saveRecentlyViewed(state.recent);
    case 'hydration': return hydrateResults(state.serverText, state.clientText);
    default: return state.records;
  }
}

export async function loadResults(state) {
  switch (state.operation) {
    case 'abort': return await cancelRequest(state.endpoint);
    case 'chunk_load': return await importRoute(state.chunk);
    case 'webpack_chunk_load': return await loadRouteChunk(state.chunk, state.chunkId);
    default: return await requestCollection(state.endpoint);
  }
}
