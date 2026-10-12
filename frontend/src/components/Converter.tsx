import { useCallback, useMemo, useRef, useState } from 'react'
import type { ConversionApiError, OutputFormat } from '../api/client'
import {
  ConversionApiError as ApiError,
  convertDocxToPdf,
  convertImagesToPdf,
  convertPdfToImage,
  convertSvgToImage,
} from '../api/client'
import { formatBytes, saveBlob } from '../download'
import type { Tool } from '../tools'
import { Dropzone } from './Dropzone'
import { Options } from './Options'

interface ConverterProps {
  tool: Tool
  maxBytes: number
}

type Status = 'idle' | 'working' | 'done' | 'error'

export function Converter({ tool, maxBytes }: ConverterProps) {
  const [files, setFiles] = useState<File[]>([])
  const [values, setValues] = useState<Record<string, string | number | boolean>>(() =>
    Object.fromEntries(tool.options.map((o) => [o.key, o.defaultValue])),
  )
  const [status, setStatus] = useState<Status>('idle')
  const [message, setMessage] = useState<string | null>(null)
  const [errorCode, setErrorCode] = useState<string | null>(null)
  const [result, setResult] = useState<{ filename: string; size: number } | null>(null)

  const abortRef = useRef<AbortController | null>(null)
  const mountedRef = useRef(true)
  // Guards against a second submit while the first is in flight, which would
  // otherwise leave an orphaned AbortController and a stale result.
  const inFlightRef = useRef(false)

  const busy = status === 'working'
  const ready = files.length > 0 && !busy

  const setOption = useCallback((key: string, value: string | number | boolean) => {
    setValues((prev) => ({ ...prev, [key]: value }))
  }, [])

  const reset = useCallback(() => {
    abortRef.current?.abort()
    abortRef.current = null
    inFlightRef.current = false
    setFiles([])
    setStatus('idle')
    setMessage(null)
    setErrorCode(null)
    setResult(null)
  }, [])

  const cancel = useCallback(() => {
    abortRef.current?.abort()
    abortRef.current = null
    inFlightRef.current = false
    setStatus('idle')
    setMessage('Conversion cancelled.')
  }, [])

  const submit = useCallback(async () => {
    if (!files.length || inFlightRef.current) return

    inFlightRef.current = true
    const controller = new AbortController()
    abortRef.current = controller

    setStatus('working')
    setMessage(null)
    setErrorCode(null)
    setResult(null)

    const asFormat = (key: string): OutputFormat =>
      values[key] === 'jpeg' ? 'jpeg' : 'png'
    const asNumber = (key: string): number | undefined => {
      const raw = values[key]
      if (raw === '' || raw === undefined) return undefined
      const parsed = Number(raw)
      return Number.isFinite(parsed) ? parsed : undefined
    }

    try {
      let outcome: { blob: Blob; filename: string }

      switch (tool.id) {
        case 'pdf-to-image': {
          const page = asNumber('page')
          outcome = await convertPdfToImage(
            files[0] as File,
            {
              format: asFormat('format'),
              dpi: asNumber('dpi') ?? 150,
              ...(page !== undefined ? { page } : {}),
            },
            controller.signal,
          )
          break
        }
        case 'images-to-pdf': {
          outcome = await convertImagesToPdf(
            files,
            { compress: Boolean(values.compress) },
            controller.signal,
          )
          break
        }
        case 'svg-to-image': {
          const width = asNumber('width')
          const height = asNumber('height')
          outcome = await convertSvgToImage(
            files[0] as File,
            {
              format: asFormat('format'),
              ...(width !== undefined ? { width } : {}),
              ...(height !== undefined ? { height } : {}),
            },
            controller.signal,
          )
          break
        }
        case 'docx-to-pdf': {
          outcome = await convertDocxToPdf(files[0] as File, controller.signal)
          break
        }
      }

      if (!mountedRef.current || controller.signal.aborted) return

      saveBlob(outcome.blob, outcome.filename)
      setStatus('done')
      setResult({ filename: outcome.filename, size: outcome.blob.size })
      setMessage(null)
    } catch (caught) {
      if (!mountedRef.current) return

      if (caught instanceof ApiError) {
        setStatus(caught.code === 'cancelled' ? 'idle' : 'error')
        setErrorCode(caught.code)
        if (caught.code === 'cancelled') {
          setMessage('Conversion cancelled.')
        } else if (caught.code === 'network_error') {
          setMessage(caught.message)
        } else {
          setMessage(humanise(caught))
        }
      } else {
        setStatus('error')
        setErrorCode('unexpected')
        setMessage('Something went wrong while converting that file.')
      }
    } finally {
      inFlightRef.current = false
      if (abortRef.current === controller) abortRef.current = null
    }
  }, [files, tool.id, values])

  const notes = useMemo(() => tool.notes, [tool.notes])

  return (
    <section className="converter" aria-labelledby={`${tool.id}-heading`}>
      <header className="converter-header">
        <h2 id={`${tool.id}-heading`}>{tool.name}</h2>
        <p className="converter-flow">
          <span className="chip">{tool.from}</span>
          <span aria-hidden="true" className="arrow">
            →
          </span>
          <span className="chip chip-accent">{tool.to}</span>
        </p>
        <p className="converter-summary">{tool.summary}</p>
      </header>

      <Dropzone
        tool={tool}
        files={files}
        onFiles={setFiles}
        disabled={busy}
        maxBytes={tool.id === 'images-to-pdf' ? maxBytes : maxBytes}
      />

      <Options tool={tool} values={values} onChange={setOption} disabled={busy} />

      <div className="actions">
        <button
          type="button"
          className="button button-primary button-large"
          onClick={() => void submit()}
          disabled={!ready}
          data-testid="convert-button"
        >
          {busy ? 'Converting…' : 'Convert'}
        </button>
        {busy ? (
          <button type="button" className="button button-ghost" onClick={cancel}>
            Cancel
          </button>
        ) : files.length > 0 ? (
          <button type="button" className="button button-ghost" onClick={reset}>
            Clear
          </button>
        ) : null}
      </div>

      <div aria-live="polite" className="status-area">
        {busy ? (
          <p className="alert alert-info" data-testid="progress">
            Converting {files[0]?.name}
            {files.length > 1 ? ` and ${files.length - 1} more` : ''}…
          </p>
        ) : null}

        {status === 'done' && result ? (
          <p className="alert alert-success" data-testid="result">
            Downloaded <strong>{result.filename}</strong> ({formatBytes(result.size)})
          </p>
        ) : null}

        {message ? (
          <p
            className={`alert ${status === 'error' ? 'alert-error' : 'alert-info'}`}
            role={status === 'error' ? 'alert' : 'status'}
            data-testid={status === 'error' ? 'error' : 'notice'}
            data-error-code={status === 'error' ? (errorCode ?? undefined) : undefined}
          >
            {message}
          </p>
        ) : null}
      </div>

      <details className="notes">
        <summary>What this conversion does and does not do</summary>
        <ul>
          {notes.map((note) => (
            <li key={note}>{note}</li>
          ))}
        </ul>
      </details>
    </section>
  )
}

/**
 * Turn an API error into something a person can act on.
 *
 * The server already sends a human message; this adds the one piece of
 * context it cannot know (which limits are actually in force) and keeps the
 * raw `code` available for the class name in tests.
 */
function humanise(error: ConversionApiError): string {
  switch (error.code) {
    case 'file_too_large':
      return error.limitMb
        ? `${error.message} The limit is ${error.limitMb}MB.`
        : error.message
    case 'rate_limited':
      return error.retryAfterSeconds
        ? `Too many conversions in a row. Try again in ${error.retryAfterSeconds} seconds.`
        : 'Too many conversions in a row. Please wait a moment.'
    case 'unsupported_type':
      return error.message
    case 'unsafe_file':
      // The server deliberately does not say which check fired, because that
      // would help someone probe for thresholds. Say what it does mean here
      // rather than guessing: a zip bomb, an oversized canvas, or a document
      // carrying active content. The previous wording only mentioned size and
      // archives, which was actively misleading for a hostile SVG.
      return `${error.message} This usually means a file that expands far beyond its real size, an oversized image, or markup carrying active content such as scripts or external references.`
    default:
      return error.message
  }
}