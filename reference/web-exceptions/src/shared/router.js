export async function importRoute(url) {
  return await import(url);
}

export function loadRouteChunk(url, chunkId) {
  return new Promise(function resolveChunk(resolve, reject) {
    const script = document.createElement('script');
    script.src = url;
    script.onload = resolve;
    script.onerror = function handleChunkError() {
      const error = new Error('Loading chunk ' + chunkId + ' failed.\n(error: ' + url + ')');
      error.name = 'ChunkLoadError';
      reject(error);
    };
    document.head.appendChild(script);
  });
}
