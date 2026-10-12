import { describe, expect, it } from 'vitest'
import { ConversionApiError, sanitizeFilename } from './client'

describe('sanitizeFilename', () => {
  it('keeps an ordinary filename intact', () => {
    expect(sanitizeFilename('report.pdf', 'f')).toBe('report.pdf')
  })

  it('strips path separators so a server filename cannot traverse', () => {
    const out = sanitizeFilename('../../etc/passwd', 'fallback')
    expect(out).not.toContain('/')
    expect(out).toBe('passwd')
  })

  it('strips windows separators', () => {
    expect(sanitizeFilename('C:\\Windows\\evil.dll', 'f')).toBe('evil.dll')
  })

  it('collapses characters that could break out of an attribute', () => {
    const out = sanitizeFilename('a"><script>alert(1)</script>.pdf', 'fallback')
    expect(out).not.toContain('<')
    expect(out).not.toContain('"')
    expect(out).toMatch(/^[\w.-]+$/)
  })

  it('strips control characters including newlines', () => {
    const out = sanitizeFilename('a\r\nb.pdf', 'fallback')
    expect(out).not.toMatch(/[\r\n]/)
  })

  it('falls back for empty, dot-only, or separator-only names', () => {
    for (const name of ['', '.', '..', '/', '///', '   ']) {
      expect(sanitizeFilename(name, 'fallback.bin')).toBe('fallback.bin')
    }
  })

  it('caps the length', () => {
    expect(sanitizeFilename(`${'x'.repeat(400)}.pdf`, 'f').length).toBeLessThanOrEqual(120)
  })

  it('does not mutate the input', () => {
    const original = '../../secret.pdf'
    sanitizeFilename(original, 'f')
    expect(original).toBe('../../secret.pdf')
  })
})

describe('ConversionApiError', () => {
  it('carries the machine-readable code and status', () => {
    const error = new ConversionApiError('Too big', 'file_too_large', 413, { limitMb: 25 })
    expect(error).toBeInstanceOf(Error)
    expect(error.code).toBe('file_too_large')
    expect(error.status).toBe(413)
    expect(error.limitMb).toBe(25)
  })

  it('leaves absent extras undefined rather than null', () => {
    const error = new ConversionApiError('x', 'y', 500)
    expect(error.limitMb).toBeUndefined()
    expect(error.retryAfterSeconds).toBeUndefined()
  })
})