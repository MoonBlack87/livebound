/**
 * Date, duration and size formatting.
 *
 * Everything here reads the current locale, so switching the language in the
 * settings changes the calendar and the board without a reload.
 */

import { locale, t } from '../../i18n/index.svelte'

const EMPTY = '—'

function parse(iso: string | null | undefined): Date | null {
  if (!iso) return null
  const date = new Date(iso)
  return Number.isNaN(date.getTime()) ? null : date
}

export function formatDateTime(iso: string | null | undefined): string {
  const date = parse(iso)
  if (!date) return EMPTY
  return date.toLocaleString(locale(), {
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

export function formatTime(iso: string | null | undefined): string {
  const date = parse(iso)
  if (!date) return EMPTY
  return date.toLocaleTimeString(locale(), { hour: '2-digit', minute: '2-digit' })
}

export function formatDay(iso: string | null | undefined): string {
  const date = parse(iso)
  if (!date) return EMPTY
  return date.toLocaleDateString(locale(), { day: '2-digit', month: '2-digit', year: 'numeric' })
}

/** Weekday initials for the calendar header, in the current locale. */
export function weekdayLabels(): string[] {
  const formatter = new Intl.DateTimeFormat(locale(), { weekday: 'short' })
  // 2024-01-01 was a Monday; the calendar grid starts there too.
  return Array.from({ length: 7 }, (_, index) =>
    formatter.format(new Date(Date.UTC(2024, 0, 1 + index))),
  )
}

export function relativeTo(iso: string | null | undefined): string {
  const date = parse(iso)
  if (!date) return ''
  const minutes = Math.round((date.getTime() - Date.now()) / 60000)
  const duration = describeMinutes(Math.abs(minutes))
  return minutes < 0 ? t('time.ago', { duration }) : t('time.in', { duration })
}

export function describeMinutes(total: number): string {
  const days = Math.floor(total / 1440)
  const hours = Math.floor((total % 1440) / 60)
  const minutes = total % 60
  const parts: string[] = []
  // Compact units, not spelled-out plurals: these end up inside chips and
  // table cells where "3 days 4 hours" would not fit.
  if (days) parts.push(t('duration.days', { count: days }))
  if (hours) parts.push(t('duration.hours', { count: hours }))
  if (minutes || !parts.length) parts.push(t('duration.minutes', { count: minutes }))
  return parts.join(' ')
}

export function formatBytes(bytes: number | null | undefined): string {
  if (!bytes) return EMPTY
  const units = ['B', 'KB', 'MB', 'GB']
  let value = bytes
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  return `${value.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`
}
