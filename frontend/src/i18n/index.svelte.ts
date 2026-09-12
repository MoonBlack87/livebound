/**
 * Translation layer.
 *
 * English is the source: `en.json` is written first and is what the components
 * are read against. `de.json` is the translation, and a missing key there falls
 * back to English rather than showing the raw key - a half-translated screen is
 * still usable, a screen full of `settings.connection.connectHint` is not.
 *
 * Keys are flat and namespaced by screen (`library.scan`, `settings.connection.connectHint`)
 * so a string can be found from the markup and back.
 */

import de from './de.json'
import en from './en.json'

export type Locale = 'en' | 'de'

export const LOCALES: { value: Locale; label: string }[] = [
  { value: 'en', label: 'English' },
  { value: 'de', label: 'Deutsch' },
]

const CATALOGUES: Record<Locale, Record<string, string>> = {
  en: en as Record<string, string>,
  de: de as Record<string, string>,
}

const state = $state<{ locale: Locale }>({ locale: 'en' })

export function locale(): Locale {
  return state.locale
}

export function setLocale(next: Locale): void {
  if (!CATALOGUES[next]) return
  state.locale = next
  document.documentElement.lang = next
}

/** Look a key up, substituting `{name}` placeholders. */
export function t(key: string, vars?: Record<string, string | number>): string {
  const text = CATALOGUES[state.locale]?.[key] ?? CATALOGUES.en[key] ?? key
  if (!vars) return text
  return Object.entries(vars).reduce(
    (carry, [name, value]) => carry.split(`{${name}}`).join(String(value)),
    text,
  )
}

/**
 * A coded message, falling back to the English sentence the backend sent.
 *
 * The same rule the API client uses for errors: an unknown code, or a
 * translation with a hole no value was supplied for, means the English text is
 * the better answer than a half-filled sentence.
 */
export function coded(
  prefix: string,
  code: string,
  message: string,
  params: Record<string, string | number> = {},
): string {
  if (!code) return message
  const key = `${prefix}.${code}`
  const text = t(key, params)
  if (text === key || /\{\w+\}/.test(text)) return message
  return text
}

/**
 * The plural form for `count`, as `<key>.one` / `<key>.other`.
 *
 * German and English happen to agree on the two forms, but the ad-hoc ternaries
 * this replaces did not survive a third language - and `Intl` already knows the
 * answer for every locale we might add.
 */
export function plural(key: string, count: number, vars?: Record<string, string | number>): string {
  const form = new Intl.PluralRules(state.locale).select(count)
  const table = CATALOGUES[state.locale] ?? CATALOGUES.en
  const chosen = table[`${key}.${form}`] !== undefined ? form : 'other'
  return t(`${key}.${chosen}`, { count, ...vars })
}
