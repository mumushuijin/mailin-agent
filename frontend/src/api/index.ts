import axios, { type AxiosInstance, type AxiosRequestConfig } from 'axios'

function resolveApiBaseUrl(): string {
  if (import.meta.env.VITE_API_BASE_URL) {
    return import.meta.env.VITE_API_BASE_URL
  }
  if (import.meta.env.VITE_ELECTRON === 'true') {
    return '/api'
  }
  return '/api'
}

function normalizeEndpoint(raw: string | null | undefined): string {
  return String(raw || '').replace(/\/$/, '').replace(/\/api$/, '')
}

async function ensureRuntimeApiBase(): Promise<void> {
  if (import.meta.env.VITE_ELECTRON !== 'true') return
  const state = await window.mailin?.backend?.getState?.()
  if (state && !['ready', 'external'].includes(state.status)) {
    throw new Error(state.error || `后端状态不可用: ${state.status}`)
  }
  const endpoint = await window.mailin?.backend?.getEndpoint?.()
  if (!endpoint) throw new Error('后端尚未就绪')
  instance.defaults.baseURL = `${normalizeEndpoint(endpoint)}/api`
}

// 创建 axios 实例
const instance: AxiosInstance = axios.create({
  baseURL: resolveApiBaseUrl(),
  timeout: 30000,
  headers: {
    'Content-Type': 'application/json',
  },
})

// 请求拦截器
instance.interceptors.request.use(
  (config) => {
    return config
  },
  (error) => {
    return Promise.reject(error)
  },
)

// 响应拦截器
instance.interceptors.response.use(
  (response) => {
    return response.data
  },
  (error) => {
    console.error('API Error:', error)
    return Promise.reject(error)
  },
)

// 包装 API 调用以获得正确的类型
const api = {
  get: <T>(url: string, config?: AxiosRequestConfig): Promise<T> => {
    return ensureRuntimeApiBase().then(() => instance.get(url, config))
  },
  post: <T>(url: string, data?: unknown, config?: AxiosRequestConfig): Promise<T> => {
    return ensureRuntimeApiBase().then(() => instance.post(url, data, config))
  },
  patch: <T>(url: string, data?: unknown, config?: AxiosRequestConfig): Promise<T> => {
    return ensureRuntimeApiBase().then(() => instance.patch(url, data, config))
  },
  put: <T>(url: string, data?: unknown, config?: AxiosRequestConfig): Promise<T> => {
    return ensureRuntimeApiBase().then(() => instance.put(url, data, config))
  },
  delete: <T>(url: string, config?: AxiosRequestConfig): Promise<T> => {
    return ensureRuntimeApiBase().then(() => instance.delete(url, config))
  },
}

export default api

export function getStreamApiBase(): string {
  if (import.meta.env.VITE_API_BASE) {
    return import.meta.env.VITE_API_BASE
  }
  if (import.meta.env.VITE_API_BASE_URL) {
    return import.meta.env.VITE_API_BASE_URL.replace(/\/api$/, '')
  }
  if (import.meta.env.VITE_ELECTRON === 'true') {
    return ''
  }
  return ''
}

export function getWsApiBase(): string {
  const httpBase = getStreamApiBase()
  if (httpBase) {
    return httpBase.replace(/^http/, 'ws')
  }
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${window.location.host}`
}

export async function getApiBaseAsync(): Promise<string> {
  if (import.meta.env.VITE_ELECTRON === 'true') {
    const endpoint = await window.mailin?.backend?.getEndpoint?.()
    if (!endpoint) throw new Error('后端尚未就绪')
    return normalizeEndpoint(endpoint)
  }
  return getStreamApiBase()
}

export async function getWsApiBaseAsync(): Promise<string> {
  const httpBase = await getApiBaseAsync()
  if (httpBase) return httpBase.replace(/^http/, 'ws')
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${window.location.host}`
}
