/** A date the way a journal prints it: "13 Nov 2026". */
export function fmtDate(iso: string): string {
  const d = new Date(iso)
  return Number.isNaN(d.getTime())
    ? iso
    : d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' })
}

/** A duration in words: "52.7 s", "1 min 59 s", "1 h 4 min". */
export function fmtDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return ''
  const s = ms / 1000
  if (s < 60) return `${s < 10 ? s.toFixed(1) : Math.round(s)} s`
  const total = Math.round(s)
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const sec = total % 60
  return h ? `${h} h ${m} min` : `${m} min ${sec} s`
}
