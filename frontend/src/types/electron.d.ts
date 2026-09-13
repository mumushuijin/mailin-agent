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
    }
  }
}
