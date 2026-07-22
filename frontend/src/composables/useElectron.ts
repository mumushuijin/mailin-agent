import { ref, onMounted } from 'vue'

const isElectron = ref(false)

export function useElectron() {
  onMounted(() => {
    isElectron.value = !!window.mailin?.isElectron
  })

  return { isElectron }
}

/** 同步检测，供模板初始渲染使用 */
export function isElectronApp(): boolean {
  return !!window.mailin?.isElectron
}
