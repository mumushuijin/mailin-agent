import { expect, test } from 'vitest'

import { groupSessionsByProject, UNBOUND_GROUP_KEY, type GroupableSession } from './projectGroups.ts'

function session(partial: Partial<GroupableSession> & Pick<GroupableSession, 'id'>): GroupableSession {
  return {
    title: '新会话',
    created_at: 1,
    updated_at: 1,
    workspace_path: null,
    ...partial,
  }
}

test('groups sessions by project path and sorts by recency', () => {
  const groups = groupSessionsByProject([
    session({
      id: 'a1',
      title: '旧会话',
      workspace_path: 'D:\\work\\alpha',
      updated_at: 10,
    }),
    session({
      id: 'b1',
      title: 'beta 新',
      workspace_path: 'D:\\work\\beta',
      updated_at: 30,
    }),
    session({
      id: 'a2',
      title: '新会话',
      workspace_path: 'd:/work/alpha',
      updated_at: 20,
    }),
  ])

  expect(groups.length).toBe(2)
  expect(groups[0]?.label).toBe('beta')
  expect(groups[0]?.sessions.map((s) => s.id)).toEqual(['b1'])
  expect(groups[1]?.label).toBe('alpha')
  expect(groups[1]?.sessions.map((s) => s.id)).toEqual(['a2', 'a1'])
})

test('unbound sessions form a trailing group', () => {
  const groups = groupSessionsByProject([
    session({ id: 'bound', workspace_path: '/tmp/proj', updated_at: 1 }),
    session({ id: 'old', workspace_path: null, updated_at: 99 }),
  ])

  expect(groups.length).toBe(2)
  expect(groups[0]?.label).toBe('proj')
  expect(groups[1]?.key).toBe(UNBOUND_GROUP_KEY)
  expect(groups[1]?.label).toBe('未绑定')
  expect(groups[1]?.sessions.map((s) => s.id)).toEqual(['old'])
})
