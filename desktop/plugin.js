// Showreel — the Desktop half. Plain ESM (no build step): a page with recorded sessions, a moment
// editor and an inline player; a REC chip in the status bar; and a player card for the videos the
// `showreel_render` tool returns. Everything goes through ctx.rest to this package's own
// dashboard/plugin_api.py. No network calls, no app internals, SDK surface only.
import * as sdk from '@hermes/plugin-sdk'
import { host, Button, ROUTES_AREA, SIDEBAR_NAV_AREA, STATUSBAR_AREAS, PALETTE_AREA } from '@hermes/plugin-sdk'
import { useCallback, useEffect, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'showreel'
const PATH = '/showreel'
const LENGTHS = [15, 30, 45, 60, 89]
const ASPECTS = ['16:9', '9:16', '1:1']
const NARRATION = [
  ['', 'Settings default'],
  ['off', 'Captions only'],
  ['local', 'Offline voice (free)'],
  ['elevenlabs', 'ElevenLabs (your key, billed)']
]

const ui = {
  page: { display: 'flex', flexDirection: 'column', gap: 12, padding: 16, height: '100%', overflow: 'auto', color: 'var(--ui-text-primary)', fontSize: 13 },
  row: { display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' },
  card: { border: '1px solid var(--ui-border, var(--chrome-border))', borderRadius: 8, padding: 12, background: 'var(--ui-surface, transparent)' },
  muted: { color: 'var(--ui-text-tertiary)' },
  h: { fontWeight: 600, fontSize: 15 },
  cols: { display: 'grid', gridTemplateColumns: 'minmax(220px, 1fr) 2fr', gap: 12, alignItems: 'start' },
  list: { display: 'flex', flexDirection: 'column', gap: 4, maxHeight: 420, overflow: 'auto' },
  item: sel => ({ textAlign: 'left', padding: '6px 8px', borderRadius: 6, border: 0, cursor: 'pointer', background: sel ? 'var(--chrome-action-hover)' : 'transparent', color: 'inherit' }),
  beat: off => ({ display: 'grid', gridTemplateColumns: '24px 64px 72px 1fr', gap: 8, alignItems: 'center', opacity: off ? 0.45 : 1, padding: '3px 0' }),
  input: { width: '100%', background: 'transparent', color: 'inherit', border: '1px solid var(--ui-border, var(--chrome-border))', borderRadius: 4, padding: '3px 6px', font: 'inherit' },
  select: { background: 'transparent', color: 'inherit', border: '1px solid var(--ui-border, var(--chrome-border))', borderRadius: 4, padding: '3px 6px', font: 'inherit' },
  video: { width: '100%', maxHeight: 480, borderRadius: 8, background: 'var(--ui-bg-sunken, transparent)' },
  rec: on => ({ display: 'inline-flex', alignItems: 'center', gap: 4, height: '100%', padding: '0 6px', border: 0, background: 'transparent', cursor: 'pointer', fontSize: 11, color: on ? 'var(--ui-text-primary)' : 'var(--ui-text-tertiary)' }),
  dot: on => ({ width: 7, height: 7, borderRadius: 4, background: on ? 'var(--ui-danger, var(--ui-accent))' : 'var(--ui-text-quaternary, var(--ui-text-tertiary))', animation: on ? 'showreel-pulse 1.4s ease-in-out infinite' : 'none' })
}

let rest = null // set in register(): ctx.rest bound to /api/plugins/showreel

function errText(e) {
  return (e && (e.message || e.detail || String(e))) || 'request failed'
}

function when(ts) {
  try { return new Date(ts * 1000).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }) } catch { return '' }
}

function Select({ value, onChange, options, label }) {
  return jsx('select', {
    'aria-label': label, style: ui.select, value, onChange: e => onChange(e.target.value),
    children: options.map(([v, l]) => jsx('option', { value: v, children: l }, v))
  })
}

function Player({ name }) {
  const [src, setSrc] = useState(null)
  const [err, setErr] = useState('')
  useEffect(() => {
    let live = true
    setSrc(null); setErr('')
    if (name) rest(`/media?name=${encodeURIComponent(name)}`).then(r => live && setSrc(r.url), e => live && setErr(errText(e)))
    return () => { live = false }
  }, [name])
  if (!name) return null
  if (err) return jsx('div', { style: ui.muted, children: err })
  if (!src) return jsx('div', { style: ui.muted, children: 'Loading video…' })
  return jsx('video', { style: ui.video, src, controls: true, playsInline: true })
}

function MomentEditor({ session, onRendered }) {
  const [length, setLength] = useState('45')
  const [aspect, setAspect] = useState('16:9')
  const [narration, setNarration] = useState('')
  const [plan, setPlan] = useState(null)
  const [skip, setSkip] = useState({})
  const [caps, setCaps] = useState({})
  const [busy, setBusy] = useState('')
  const [msg, setMsg] = useState('')

  const load = useCallback(() => {
    if (!session) return
    setBusy('plan'); setMsg('')
    const q = `/plan?session_id=${encodeURIComponent(session)}&length=${length}&narration=${narration}`
    rest(q).then(r => { setPlan(r); setSkip({}); setCaps({}) }, e => setMsg(errText(e))).finally(() => setBusy(''))
  }, [session, length, narration])
  useEffect(load, [load])

  const doRender = () => {
    if (narration === 'elevenlabs' && plan && plan.plan &&
        !window.confirm(`Send about ${plan.plan.characters} characters to ElevenLabs with your key? This is billed to your ElevenLabs account.`)) return
    setBusy('render'); setMsg('')
    const edits = { skip: Object.keys(skip).filter(k => skip[k]).map(Number), captions: caps }
    rest('/render', { method: 'POST', timeoutMs: 600000, body: { session_id: session, length: Number(length), aspect, narration: narration || undefined, edits } })
      .then(r => {
        if (!r.success) { setMsg(r.error || 'render failed'); return }
        setMsg(`Rendered ${r.duration_s}s ${r.aspect} · ${r.narration} · ${r.redactions} secret(s) redacted`)
        onRendered(r.name)
      }, e => setMsg(errText(e))).finally(() => setBusy(''))
  }

  if (!session) return jsx('div', { style: ui.muted, children: 'Pick a recorded session.' })
  const beats = (plan && plan.beats) || []
  return jsxs('div', {
    style: { display: 'flex', flexDirection: 'column', gap: 10 },
    children: [
      jsxs('div', { style: ui.row, children: [
        jsx(Select, { label: 'Length', value: length, onChange: setLength, options: LENGTHS.map(s => [String(s), `${s} s`]) }),
        jsx(Select, { label: 'Aspect', value: aspect, onChange: setAspect, options: ASPECTS.map(a => [a, a]) }),
        jsx(Select, { label: 'Narration', value: narration, onChange: setNarration, options: NARRATION }),
        jsx(Button, { size: 'sm', variant: 'outline', disabled: !!busy, onClick: load, children: busy === 'plan' ? 'Planning…' : 'Re-plan' }),
        jsx(Button, { size: 'sm', disabled: !!busy || !beats.length, onClick: doRender, children: busy === 'render' ? 'Rendering…' : 'Render' })
      ] }),
      plan && plan.plan && jsx('div', { style: ui.muted, children:
        `${plan.title} · ~${plan.plan.estimated_duration_s}s · narration: ${plan.narration}` +
        (plan.narration.startsWith('captions only') ? '' : ` · ${plan.plan.characters} characters to speak`) }),
      jsx('div', { children: beats.map((b, i) => {
        const locked = b.kind === 'title' || b.kind === 'outro'
        return jsxs('div', { style: ui.beat(!!skip[i]), children: [
          jsx('input', { type: 'checkbox', 'aria-label': `Keep beat ${i + 1}`, checked: !skip[i], disabled: locked,
            onChange: e => setSkip({ ...skip, [i]: !e.target.checked }) }),
          jsx('span', { style: ui.muted, children: `${b.start.toFixed(1)}s` }),
          jsx('span', { children: b.kind }),
          jsx('input', { style: ui.input, value: caps[i] ?? b.caption, 'aria-label': `Caption ${i + 1}`,
            onChange: e => setCaps({ ...caps, [i]: e.target.value }) })
        ] }, i)
      }) }),
      msg && jsx('div', { style: ui.muted, children: msg })
    ]
  })
}

function ShowreelPage() {
  const [status, setStatus] = useState(null)
  const [sessions, setSessions] = useState([])
  const [renders, setRenders] = useState([])
  const [session, setSession] = useState('')
  const [playing, setPlaying] = useState('')
  const [err, setErr] = useState('')

  const refresh = useCallback(() => {
    rest('/status').then(setStatus, e => setErr(errText(e)))
    rest('/sessions').then(r => {
      setSessions(r.sessions || [])
      setSession(s => s || ((r.sessions || []).find(x => x.tool_calls) || {}).session_id || '')
    }, () => {})
    rest('/renders').then(r => setRenders(r.renders || []), () => {})
  }, [])
  useEffect(refresh, [refresh])

  return jsxs('div', { style: ui.page, children: [
    jsxs('div', { style: ui.row, children: [
      jsx('span', { style: ui.dot(status && status.recording) }),
      jsx('span', { style: ui.h, children: 'Showreel' }),
      status && jsx('span', { style: ui.muted, children:
        `${status.capture ? (status.recording ? 'recording this session' : 'capture on') : 'capture off'} · ${status.sessions} session(s) · ` +
        `${status.ffmpeg ? 'ffmpeg ready' : 'ffmpeg missing'} · narration: ${status.narration_resolved}` }),
      jsx(Button, { size: 'sm', variant: 'ghost', onClick: refresh, children: 'Refresh' })
    ] }),
    err && jsx('div', { style: ui.muted, children: err }),
    jsxs('div', { style: ui.cols, children: [
      jsxs('div', { style: ui.card, children: [
        jsx('div', { style: ui.h, children: 'Sessions' }),
        jsx('div', { style: ui.list, children: sessions.length ? sessions.map(s => jsxs('button', {
          type: 'button', style: ui.item(s.session_id === session), onClick: () => setSession(s.session_id),
          children: [
            jsx('div', { children: (s.first_prompt || s.session_id).slice(0, 80) }),
            jsx('div', { style: ui.muted, children: `${when(s.started)} · ${s.tool_calls} tool calls` })
          ] }, s.session_id)) : jsx('div', { style: ui.muted, children: 'No recordings yet. Run a task with Showreel enabled.' }) })
      ] }),
      jsxs('div', { style: ui.card, children: [
        jsx('div', { style: ui.h, children: 'Moments' }),
        jsx(MomentEditor, { session, onRendered: name => { setPlaying(name); refresh() } })
      ] })
    ] }),
    jsxs('div', { style: ui.card, children: [
      jsx('div', { style: ui.h, children: 'Renders' }),
      jsx('div', { style: ui.row, children: renders.map(r => jsx(Button, {
        size: 'sm', variant: r.name === playing ? 'default' : 'outline', onClick: () => setPlaying(r.name),
        children: `${when(r.mtime)} · ${r.duration_s ?? '?'}s ${r.aspect || ''}` }, r.name)) }),
      jsx(Player, { name: playing || (renders[0] && renders[0].name) || '' })
    ] })
  ] })
}

function RecChip({ ctx }) {
  const [s, setS] = useState(null)
  useEffect(() => {
    let live = true
    const tick = () => rest('/status').then(r => live && setS(r), () => live && setS(null))
    tick()
    const stop = ctx.setInterval(tick, 5000)
    return () => { live = false; stop() }
  }, [ctx])
  if (!s || !s.capture) return null
  return jsxs('button', {
    type: 'button', style: ui.rec(s.recording), title: s.recording ? 'Showreel is recording this session' : 'Showreel capture is on',
    onClick: () => host.navigate(PATH),
    children: [jsx('span', { style: ui.dot(s.recording) }), s.recording ? 'REC' : 'Showreel']
  })
}

function ToolVideo({ file, result, fallback }) {
  const [src, setSrc] = useState(null)
  useEffect(() => {
    let live = true
    file.resolveUrl().then(u => live && setSrc(u), () => live && setSrc(''))
    return () => { live = false }
  }, [file])
  if (file.kind !== 'video' || src === '') return fallback
  let r = result
  if (typeof r === 'string') { try { r = JSON.parse(r) } catch { r = {} } }
  r = r || {}
  return jsxs('figure', { style: { margin: 0, display: 'flex', flexDirection: 'column', gap: 6 }, children: [
    src ? jsx('video', { style: ui.video, src, controls: true, playsInline: true }) : jsx('div', { style: ui.muted, children: 'Loading video…' }),
    jsxs('figcaption', { style: { ...ui.row, ...ui.muted }, children: [
      jsx('span', { children: `Showreel · ${r.duration_s ?? '?'}s ${r.aspect || ''} · ${r.narration || 'captions only'} · ${r.redactions ?? 0} secret(s) redacted` }),
      jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => host.navigate(PATH), children: 'Edit moments' })
    ] })
  ] })
}

const CSS = '@keyframes showreel-pulse{0%,100%{opacity:1}50%{opacity:.25}}'

export default {
  id: ID,
  name: 'Showreel',
  description: 'Turn a Hermes session into a demo video: sessions, moment editor, inline player, REC chip.',
  register(ctx) {
    rest = (path, opts) => ctx.rest(path, opts)
    const style = document.createElement('style')
    style.textContent = CSS
    document.head.appendChild(style)
    ctx.onDispose(() => style.remove())
    ctx.registerMany([
      { id: 'page', area: ROUTES_AREA, data: { path: PATH }, render: () => jsx(ShowreelPage, {}) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, order: 60, data: { path: PATH, label: 'Showreel', codicon: 'device-camera-video' } },
      { id: 'rec', area: STATUSBAR_AREAS.right, order: 125, render: () => jsx(RecChip, { ctx }) },
      { id: 'open', area: PALETTE_AREA, data: { id: 'showreel.open', label: 'Showreel: open sessions and renders', keywords: ['video', 'demo', 'showreel'], run: () => host.navigate(PATH) } }
    ])
    if (typeof sdk.TOOL_MEDIA_RENDERER_AREA === 'string') {
      ctx.register({ id: 'player', area: sdk.TOOL_MEDIA_RENDERER_AREA, data: { tools: ['showreel_render'], render: props => jsx(ToolVideo, props) } })
    }
  }
}
