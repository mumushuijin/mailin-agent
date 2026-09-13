import assert from 'node:assert/strict'
import test from 'node:test'

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

  assert.equal(groups.length, 2)
  assert.equal(groups[0]?.label, 'beta')
  assert.deepEqual(groups[0]?.sessions.map((s) => s.id), ['b1'])
  assert.equal(groups[1]?.label, 'alpha')
  assert.deepEqual(groups[1]?.sessions.map((s) => s.id), ['a2', 'a1'])
})

test('unbound sessions form a trailing group', () => {
  const groups = groupSessionsByProject([
    session({ id: 'bound', workspace_path: '/tmp/proj', updated_at: 1 }),
    session({ id: 'old', workspace_path: null, updated_at: 99 }),
  ])

  assert.equal(groups.length, 2)
  assert.equal(groups[0]?.label, 'proj')
  assert.equal(groups[1]?.key, UNBOUND_GROUP_KEY)
  assert.equal(groups[1]?.label, '未绑定')
  assert.deepEqual(groups[1]?.sessions.map((s) => s.id), ['old'])
})
