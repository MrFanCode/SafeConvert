/**
 * Typed client for the conversion API.
 *
 * Every path here is relative (`/api/...`) because nginx proxies `/api` to
 * the backend on the same origin. That is deliberate: a same-origin request
 * needs no CORS grant and cannot leak the user's session to a third party.
 *
 * Two rules that matter for a security tool:
 *
 * 1. **Never surface a server string as markup.** React escapes by default,
 *    but error text arrives from the network, so it is rendered as text nodes
 *    only, and `dangerouslySetInnerHTML` is banned project-wide by lint.
 * 2. **Never trust the server's content type.** The response is turned into a
 *    Blob with an explicit type derived from what *we* asked for, so a
 *    mislabelled or hostile response cannot become an executable document in
 *    the download shelf.
 */

export type OutputFormat = 'png' | 'jpeg'

export type ConversionKind = 'pdf-to-image' | 'images-to-pdf' | 'svg-to-image' | 'docx-to-pdf'

export interface ApiErrorBody {
  error: {
    code: string
    message: string
    limit_mb?: number
    retry_after_seconds?: number
    fields?: Array<{ field: string; problem: string }>
  }
}

/** A conversion failure the UI can render. */
export class ConversionApiError extends Error {
  readonly code: string
  readonly status: number
  readonly limitMb?: number
  readonly retryAfterSeconds?: number

  constructor(
    message: string,
    code: string,
    status: number,
    extra?: { limitMb?: number | undefined; retryAfterSeconds?: number | undefined },
  ) {
    super(message)
    this.name = 'ConversionApiError'
    this.code = code
    this.status = status
    if (extra?.limitMb !== undefined) this.limitMb = extra.limitMb
    if (extra?.retryAfterSeconds !== undefined) this.retryAfterSeconds = extra.retryAfterSeconds
  }
}

const API_BASE = '/api'

function isApiErrorBody(value: unknown): value is ApiErrorBody {
  if (typeof value !== 'object' || value === null) return false
  const err = (value as { error?: unknown }).error
  if (typeof err !== 'object' || err === null) return false
  const { code, message } = err as { code?: unknown; message?: unknown }
  return typeof code === 'string' && typeof message === 'string'
}

/**
 * Parse a failed response into a typed error.
 *
 * Falls back to a generic message for any body that is not the documented
 * envelope — a proxy returning an HTML error page must not have its markup
 * rendered in the UI, and the code is replaced so a stack trace cannot be
 * shown verbatim either.
 */
async function toApiError(response: Response): Promise<ConversionApiError> {
  const fallback = `Conversion failed (HTTP ${response.status}).`
  try {
    const text = await response.text()
    let parsed: unknown
    try {
      parsed = JSON.parse(text)
    } catch {
      // Not JSON — an nginx error page, most likely. Do not echo it.
      return new ConversionApiError(fallback, 'unexpected_response', response.status)
    }
    if (!isApiErrorBody(parsed)) {
      return new ConversionApiError(fallback, 'unexpected_response', response.status)
    }
    const retryAfter = Number(response.headers.get('Retry-After') ?? '')
    return new ConversionApiError(parsed.error.message, parsed.error.code, response.status, {
      limitMb: parsed.error.limit_mb,
      retryAfterSeconds: Number.isFinite(retryAfter) && retryAfter > 0 ? retryAfter : undefined,
    })
  } catch {
    return new ConversionApiError(fallback, 'unexpected_response', response.status)
  }
}

async function request(path: string, form: FormData, signal?: AbortSignal): Promise<Response> {
  let response: Response
  try {
    // exactOptionalPropertyTypes means `signal` must be omitted rather than
    // set to undefined, so the init object is built conditionally.
    const init: RequestInit = { method: 'POST', body: form }
    if (signal) init.signal = signal
    response = await fetch(`${API_BASE}${path}`, init)
  } catch (cause) {
    // fetch rejects on network failure and on abort. Distinguish them so the
    // UI can say "cancelled" rather than "something went wrong".
    if (cause instanceof DOMException && cause.name === 'AbortError') {
      throw new ConversionApiError('Conversion cancelled.', 'cancelled', 0)
    }
    throw new ConversionApiError(
      'Could not reach the conversion service. Check your connection and try again.',
      'network_error',
      0,
    )
  }
  if (!response.ok) throw await toApiError(response)
  return response
}

/**
 * Turn a successful response into a downloadable Blob.
 *
 * The MIME type is passed in by the caller from the requested output, never
 * read from the response headers. If the backend ever answered with
 * `image/svg+xml` or `text/html`, the downloaded file would carry our declared
 * type instead — so a hostile document cannot be opened as an active
 * document by double-clicking it.
 */
async function toDownloadBlob(response: Response, mimeType: string): Promise<Blob> {
  const blob = await response.blob()
  return new Blob([blob], { type: mimeType })
}

/** Derive a filename from the Content-Disposition header, falling back sanely. */
export function filenameFromResponse(response: Response, fallback: string): string {
  const disposition = response.headers.get('Content-Disposition')
  if (disposition) {
    // filename="name" or filename*=UTF-8''name
    const starMatch = /filename\*\s*=\s*UTF-8''([^;]+)/i.exec(disposition)
    if (starMatch?.[1]) {
      try {
        return sanitizeFilename(decodeURIComponent(starMatch[1]), fallback)
      } catch {
        /* fall through to the plain form */
      }
    }
    const plainMatch = /filename\s*=\s*"([^"]*)"/i.exec(disposition)
    if (plainMatch?.[1]) return sanitizeFilename(plainMatch[1], fallback)
  }
  return fallback
}

/**
 * Reduce a server-supplied filename to something safe for the DOM.
 *
 * The backend already sanitises, but the client does not depend on that: a
 * filename arriving over the network is untrusted input regardless of what
 * the server says it did.
 */
export function sanitizeFilename(name: string, fallback: string): string {
  const base = name.split(/[\\/]/).pop() ?? ''
  // Order matters: collapse disallowed characters to "_", then strip leading
  // dots, then re-check. Whitespace must go before the emptiness test —
  // otherwise a name of " " becomes "_" and is returned instead of the
  // fallback. Also: a stripped-of-everything name must not survive as "_".
  const cleaned = base
    .replace(/[^A-Za-z0-9._-]+/g, '_')
    .replace(/^[._-]+/, '')
    .replace(/^$/, '')
  if (!cleaned || cleaned === '.' || cleaned === '..' || /^_+$/.test(cleaned)) {
    return fallback
  }
  return cleaned.slice(0, 120) || fallback
}

export interface ConversionResult {
  blob: Blob
  filename: string
  isArchive: boolean
}

function buildForm(entries: Array<[string, File | Blob]>): FormData {
  const form = new FormData()
  for (const [key, value] of entries) form.append(key, value)
  return form
}

export async function convertPdfToImage(
  file: File,
  options: { format: OutputFormat; dpi: number; page?: number },
  signal?: AbortSignal,
): Promise<ConversionResult> {
  const params = new URLSearchParams({ format: options.format, dpi: String(options.dpi) })
  if (options.page !== undefined) params.set('page', String(options.page))
  const response = await request(
    `/pdf-to-image?${params.toString()}`,
    buildForm([['file', file]]),
    signal,
  )
  const mime = options.format === 'png' ? 'image/png' : 'image/jpeg'
  const zip = response.headers.get('content-type')?.includes('zip') ?? false
  return {
    blob: await toDownloadBlob(response, zip ? 'application/zip' : mime),
    filename: filenameFromResponse(response, zip ? 'pages.zip' : `converted.${options.format === 'png' ? 'png' : 'jpg'}`),
    isArchive: zip,
  }
}

export async function convertImagesToPdf(
  files: File[],
  options: { compress: boolean },
  signal?: AbortSignal,
): Promise<ConversionResult> {
  const params = new URLSearchParams({ compress: String(options.compress) })
  const response = await request(
    `/images-to-pdf?${params.toString()}`,
    buildForm(files.map((f) => ['files', f] as [string, File])),
    signal,
  )
  return {
    blob: await toDownloadBlob(response, 'application/pdf'),
    filename: filenameFromResponse(response, 'converted.pdf'),
    isArchive: false,
  }
}

export async function convertSvgToImage(
  file: File,
  options: { format: OutputFormat; width?: number; height?: number; dpi?: number },
  signal?: AbortSignal,
): Promise<ConversionResult> {
  const params = new URLSearchParams({ format: options.format })
  if (options.width !== undefined) params.set('width', String(options.width))
  if (options.height !== undefined) params.set('height', String(options.height))
  if (options.dpi !== undefined) params.set('dpi', String(options.dpi))
  const response = await request(
    `/svg-to-image?${params.toString()}`,
    buildForm([['file', file]]),
    signal,
  )
  const mime = options.format === 'png' ? 'image/png' : 'image/jpeg'
  return {
    blob: await toDownloadBlob(response, mime),
    filename: filenameFromResponse(response, `converted.${options.format === 'png' ? 'png' : 'jpg'}`),
    isArchive: false,
  }
}

export async function convertDocxToPdf(file: File, signal?: AbortSignal): Promise<ConversionResult> {
  const response = await request('/docx-to-pdf', buildForm([['file', file]]), signal)
  return {
    blob: await toDownloadBlob(response, 'application/pdf'),
    filename: filenameFromResponse(response, 'converted.pdf'),
    isArchive: false,
  }
}

export async function fetchHealth(signal?: AbortSignal): Promise<boolean> {
  try {
    const init: RequestInit = {}
    if (signal) init.signal = signal
    const response = await fetch(`${API_BASE}/health`, init)
    if (!response.ok) return false
    const body: unknown = await response.json()
    return typeof body === 'object' && body !== null && (body as { status?: unknown }).status === 'ok'
  } catch {
    return false
  }
}