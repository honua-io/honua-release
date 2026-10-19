// Fixture credentials are scoped to the owned installer endpoint.
export async function requestJson(baseUrl, apiKey, route, body, extraHeaders = {}) {
  const endpoint = new URL(route, baseUrl);
  if (endpoint.origin !== new URL(baseUrl).origin) throw new Error('fixture origin mismatch');
  return fetch(endpoint, {
    method: 'POST', redirect: 'error',
    headers: { 'Content-Type': 'application/json', ...extraHeaders, 'X-API-Key': apiKey },
    body: JSON.stringify(body), signal: AbortSignal.timeout(30000),
  });
}
