export function readPrice(record) {
  return record.price.amount;
}

export function formatPrice(record) {
  return record.formatPrice(record.price);
}

export function buildPriceRows(records, formatter) {
  return records.map(formatter);
}

export function saveRecentlyViewed(value) {
  localStorage.setItem('vantra:recently-viewed', value);
}

export function hydrateResults(serverText, clientText) {
  if (serverText !== clientText) {
    throw new Error('Hydration failed: the server-rendered results do not match the client state.');
  }
  return clientText;
}
