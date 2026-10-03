export {}

declare global {
  interface Window {
    mailin?: {
      isElectron: boolean
      platform: string
      window: {
        minimize: () => Promise<void>
        maximize: () => Promise<void>
        close: () => Promise<void>
        isMaximized: () => Promise<boolean>
        onMaximizedChanged: (callback: (maximized: boolean) => void) => () => void
      }
      dialog?: {
        selectDirectory: () => Promise<string | null>
      }
      shell?: {
        openPath: (folderPath: string) => Promise<{ ok: boolean; error?: string | null }>
      }
      backend?: {
        getState: () => Promise<{
          mode: 'managed' | 'external'
          status: 'starting' | 'ready' | 'external' | 'unavailable' | 'stopped'
          endpoint: string | null
          installRoot: string | null
          runtimeRoot: string | null
          paths: { runtimeRoot: string; data: string; cache: string; tmp: string; log: string } | null
          error: string | null
          pid: number | null
        }>
        getEndpoint: () => Promise<string | null>
        retry: () => Promise<unknown>
        chooseRuntimeRoot: () => Promise<unknown>
      }
    }
  }
}
