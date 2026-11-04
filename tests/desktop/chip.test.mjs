// The chip's text/tooltip model, run against the real plugin.js with a stub SDK.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { chipModel } from '../../desktop/plugin.js'

const status = (state, providers, extra = {}) => ({ state, threshold_percent: 100, fail_open: false, providers, ...extra })
const p = (provider, state, ...used) => ({ provider, state, peak_percent: used.length ? Math.max(...used) : null,
  windows: used.map((u, i) => ({ label: ['Session', 'Weekly'][i], used_percent: u, resets: 'in 2h' })) })

test('ok shows the highest window across providers', () => {
  const m = chipModel(status('ok', [p('anthropic', 'ok', 42, 61), p('openai-codex', 'ok', 12)]))
  assert.equal(m.state, 'ok'); assert.equal(m.text, '61%')
  assert.match(m.tip, /anthropic: ok · Session 42% \(resets in 2h\) · Weekly 61%/)
})

test('blocked says blocked, not a percentage', () => {
  assert.equal(chipModel(status('blocked', [p('anthropic', 'blocked', 100)])).text, 'blocked')
})

test('no reading yet is spelled out, and fail_open is called out', () => {
  const m = chipModel(status('unverified', [p('anthropic', 'unverified')], { fail_open: true }))
  assert.equal(m.text, 'guard'); assert.match(m.tip, /fail_open is ON/); assert.match(m.tip, /allowed without a reading \(fail_open\) · no reading/)
})

test('an unreachable backend hides the chip instead of guessing', () => {
  assert.equal(chipModel(null).state, 'unknown')
  assert.equal(chipModel({}).state, 'unknown')
})

test('a provider never used is named as such and does not raise the peak', () => {
  const m = chipModel(status('ok', [p('anthropic', 'ok', 30), p('openai-codex', 'idle')]))
  assert.equal(m.text, '30%'); assert.match(m.tip, /openai-codex: not used yet$/m)
})
