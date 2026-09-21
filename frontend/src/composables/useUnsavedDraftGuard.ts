import { onBeforeUnmount, onMounted, type Ref } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import { Modal } from 'ant-design-vue'

/**
 * 未保存草稿提示：窗口离开 + 设置内路由切换。
 * 各视图各自注册，避免配置域与 MCP 域草稿交叉提交。
 */
export function useUnsavedDraftGuard(dirty: Ref<boolean>, message = '有未保存的草稿，确定离开吗？') {
  const onBeforeUnload = (event: BeforeUnloadEvent) => {
    if (!dirty.value) return
    event.preventDefault()
    event.returnValue = message
  }

  onMounted(() => {
    window.addEventListener('beforeunload', onBeforeUnload)
  })

  onBeforeUnmount(() => {
    window.removeEventListener('beforeunload', onBeforeUnload)
  })

  onBeforeRouteLeave((_to, _from, next) => {
    if (!dirty.value) {
      next()
      return
    }
    Modal.confirm({
      title: '未保存的草稿',
      content: message,
      okText: '离开',
      cancelText: '继续编辑',
      onOk: () => next(),
      onCancel: () => next(false),
    })
  })
}
