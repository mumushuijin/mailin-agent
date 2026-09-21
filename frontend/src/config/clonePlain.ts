/** 纯 JSON 深拷贝：兼容 Vue reactive Proxy，避免 structuredClone 失败。 */
export function clonePlainJson<T>(value: T): T {
  if (value === undefined) {
    return value
  }
  return JSON.parse(JSON.stringify(value)) as T
}
