export class ApiError extends Error {
  constructor(message: string, public status: number, public retryAfter: number | null = null) {
    super(message); this.name = 'ApiError';
  }
}

function messageFrom(data: unknown, fallback: string): string {
  if (!data || typeof data !== 'object' || !('detail' in data)) return fallback;
  const detail = data.detail;
  if (typeof detail === 'string') return detail;
  // Pydantic error inputs can contain credentials. Render messages, never input values.
  if (Array.isArray(detail)) return detail.map(item => typeof item?.msg === 'string' ? item.msg : 'Datos no válidos').join('. ');
  return fallback;
}

export class ApiClient {
  private csrf = '';
  private controllers = new Set<AbortController>();
  constructor(private onUnauthorized: () => void) {}
  setSession(csrf: string) {this.csrf = csrf;}
  clear() {this.csrf = ''; this.controllers.forEach(controller => controller.abort()); this.controllers.clear();}
  async request<T>(path: string, options: RequestInit = {}): Promise<T> {
    const url = new URL('/api/v1' + path, window.location.origin);
    if (!path.startsWith('/') || url.origin !== window.location.origin || !url.pathname.startsWith('/api/v1/')) {
      throw new Error('La API debe pertenecer al mismo origen.');
    }
    const controller = new AbortController();
    const abort = () => controller.abort();
    options.signal?.addEventListener('abort', abort, {once: true});
    if (options.signal?.aborted) controller.abort();
    this.controllers.add(controller);
    const headers = new Headers(options.headers);
    if (!['GET','HEAD','OPTIONS'].includes((options.method || 'GET').toUpperCase()) && this.csrf) headers.set('X-CSRF-Token', this.csrf);
    try {
      const response = await fetch(url, {...options, headers, signal: controller.signal, credentials: 'same-origin'});
      if (response.status === 401) {
        this.onUnauthorized();
        throw new ApiError('La sesión expiró. Inicia sesión nuevamente.', 401);
      }
      const data: unknown = await response.json().catch(() => null);
      if (!response.ok) {
        const fallback = response.status === 429 ? 'Se alcanzó un límite de solicitudes. Inténtalo más tarde.'
          : response.status === 503 ? 'El servicio no está disponible temporalmente.' : 'La operación no se completó (HTTP ' + response.status + ').';
        const retry = response.headers.get('Retry-After');
        throw new ApiError(messageFrom(data, fallback), response.status, retry && Number.isFinite(Number(retry)) ? Number(retry) : null);
      }
      return data as T;
    } finally {
      this.controllers.delete(controller);
      options.signal?.removeEventListener('abort', abort);
    }
  }
  post<T>(path: string, body?: unknown, signal?: AbortSignal) {
    return this.request<T>(path, {method:'POST', signal, ...(body === undefined ? {} : {
      headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)})});
  }
}
export const errorMessage = (error: unknown) => error instanceof Error ? error.message : 'No se pudo completar la operación.';
export const isAborted = (error: unknown) => error instanceof Error && error.name === 'AbortError';
