export function decodeResponse(body) {
  return JSON.parse(body);
}

export async function requestCollection(url) {
  const response = await fetch(url);
  const body = await response.text();
  return decodeResponse(body);
}

export async function cancelRequest(url) {
  const controller = new AbortController();
  controller.abort();
  return await fetch(url, { signal: controller.signal });
}
