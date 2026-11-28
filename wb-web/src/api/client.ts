export class ApiError extends Error {
  status: number
  detail: string

  constructor(status: number, detail: string) {
    super(detail)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

export async function request<T>(
  method: 'GET' | 'POST' | 'PUT' | 'DELETE',
  url: string,
  body?: unknown,
  signal?: AbortSignal,
): Promise<T> {
  const init: RequestInit = {
    method,
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    signal,
  }
  if (body !== undefined) {
    init.body = JSON.stringify(body)
  }

  const res = await fetch(url, init)
  if (!res.ok) {
    let detail = res.statusText || `HTTP ${res.status}`
    try {
      const data = await res.json()
      if (typeof data?.detail === 'string') detail = data.detail
    } catch {
      // ignore non-JSON error body
    }
    throw new ApiError(res.status, detail)
  }
  return (await res.json()) as T
}
