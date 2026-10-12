/**
 * Download handling.
 *
 * Kept separate so the "save the result" step is testable without a DOM
 * <a> element, and so there is exactly one place where a filename from the
 * network becomes a browser-visible string.
 */

import { sanitizeFilename } from './api/client'

/**
 * Trigger a download of a Blob.
 *
 * Uses an object URL and revokes it on the next tick. The object URL is
 * same-origin and is what gives the download its `type` — which is why the
 * caller passes an explicit MIME type derived from the *request*, not from
 * the response headers.
 */
export function saveBlob(blob: Blob, filename: string): void {
  const safeName = sanitizeFilename(filename, 'converted-file')
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = safeName
  anchor.rel = 'noopener'
  anchor.style.display = 'none'

  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)

  // Revoking immediately can cancel the download in some browsers; a short
  // timeout is the widely-used compromise.
  window.setTimeout(() => URL.revokeObjectURL(url), 10_000)
}

/** Human-readable byte size. Uses KB/MB with one decimal, not bytes. */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '—'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}