export type Json = Record<string, any>;
export async function api<T = any>(path: string, body?: unknown, method?: string): Promise<T> {
  const token = sessionStorage.getItem('sg_access_token');
  const response = await fetch('/api' + path, {method: method || (body === undefined ? 'GET' : 'POST'), headers: {'Content-Type':'application/json', ...(token ? {Authorization:'Bearer ' + token} : {})}, body:body === undefined ? undefined : JSON.stringify(body)});
  const result = await response.json();
  if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : JSON.stringify(result.detail || result));
  return result;
}
export const pretty = (v: unknown) => JSON.stringify(v,null,2);
export const number = (n: number | undefined) => n === undefined ? '—' : new Intl.NumberFormat().format(n);
export const time = (n: number | null) => n ? new Date(n * 1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}) : 'Not started';
