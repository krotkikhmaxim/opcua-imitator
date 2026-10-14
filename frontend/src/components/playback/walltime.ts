/** Helpers времени таймлайна: ISO тренда ↔ локальная стена и поля ввода. */

/** ISO (включая микросекунды) → epoch ms; наивные метки — локальным временем. */
export function parseIsoMs(iso: string | null | undefined): number | null {
  if (!iso) return null
  const trimmed = iso.replace(/\.(\d{3})\d+/, '.$1')
  const ms = new Date(trimmed).getTime()
  return Number.isFinite(ms) ? ms : null
}

export function formatWall(ms: number): string {
  const d = new Date(ms)
  const pad = (n: number) => String(n).padStart(2, '0')
  return (
    `${pad(d.getDate())}.${pad(d.getMonth() + 1)}.${d.getFullYear()} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
  )
}

/** epoch ms → только время суток (для компактных бейджей). */
export function formatWallTime(ms: number): string {
  const d = new Date(ms)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

export function formatDuration(ms: number): string {
  const total = Math.floor(ms / 1000)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${Math.floor(total / 3600)}:${pad(Math.floor((total % 3600) / 60))}:${pad(total % 60)}`
}

export function datetimeLocalToMs(value: string): number | null {
  if (!value) return null
  const ms = new Date(value).getTime()
  return Number.isFinite(ms) ? ms : null
}

/** epoch ms → значение для input datetime-local (локальное время). */
export function msToLocalInput(ms: number): string {
  const d = new Date(ms)
  const pad = (n: number) => String(n).padStart(2, '0')
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
    `T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
  )
}
