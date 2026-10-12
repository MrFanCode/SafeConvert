import { useCallback, useRef, useState } from 'react'
import type { ChangeEvent, DragEvent } from 'react'
import type { Tool } from '../tools'
import { formatBytes } from '../download'

interface DropzoneProps {
  tool: Tool
  files: File[]
  onFiles: (files: File[]) => void
  disabled?: boolean
  /** Max single-file size in bytes; multi-file tools get a larger cap. */
  maxBytes: number
}

/**
 * Drag-and-drop / file-picker input.
 *
 * Notes on the security-relevant behaviour:
 * - Accept is enforced by the `accept` attribute (a hint to the OS dialog) and
 *   again here, because the attribute is trivially bypassed.
 * - File *contents* are what the backend trusts. This check exists only to
 *   avoid uploading something obviously wrong and to give an immediate,
 *   specific message rather than a round trip.
 * - No file is ever read with FileReader or rendered as a data: URL. The only
 *   time bytes exist in this app is inside the FormData handed to fetch.
 */
export function Dropzone({ tool, files, onFiles, disabled, maxBytes }: DropzoneProps) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [dragging, setDragging] = useState(false)
  const [rejected, setRejected] = useState<string | null>(null)

  const validate = useCallback(
    (candidate: File[]): { ok: File[]; problems: string[] } => {
      const ok: File[] = []
      const problems: string[] = []

      for (const file of candidate) {
        const ext = file.name.split('.').pop()?.toLowerCase() ?? ''
        if (!tool.extensions.includes(ext)) {
          problems.push(`${file.name}: not a ${tool.extensions.join('/')} file`)
          continue
        }
        if (file.size === 0) {
          problems.push(`${file.name}: the file is empty`)
          continue
        }
        if (file.size > maxBytes) {
          problems.push(`${file.name}: larger than the ${formatBytes(maxBytes)} limit`)
          continue
        }
        ok.push(file)
      }
      return { ok, problems }
    },
    [tool.extensions, maxBytes],
  )

  const accept = useCallback(
    (candidate: File[]) => {
      const { ok, problems } = validate(candidate)
      setRejected(problems.length ? problems.join('. ') : null)
      if (!ok.length) return

      if (tool.multiple) {
        // Preserve selection order: the user sets page order by adding in
        // order, so appending (not replacing) is the behaviour they expect.
        onFiles([...files, ...ok])
      } else {
        onFiles(ok.slice(0, 1))
      }
    },
    [files, onFiles, tool.multiple, validate],
  )

  const onDrop = useCallback(
    (event: DragEvent<HTMLDivElement>) => {
      event.preventDefault()
      setDragging(false)
      if (disabled) return
      accept(Array.from(event.dataTransfer.files))
    },
    [accept, disabled],
  )

  const onPick = useCallback(
    (event: ChangeEvent<HTMLInputElement>) => {
      if (disabled) return
      accept(Array.from(event.target.files ?? []))
      // Reset so picking the same file twice in a row still fires onChange.
      event.target.value = ''
    },
    [accept, disabled],
  )

  const remove = useCallback(
    (index: number) => {
      onFiles(files.filter((_, i) => i !== index))
    },
    [files, onFiles],
  )

  const move = useCallback(
    (index: number, delta: number) => {
      const target = index + delta
      if (target < 0 || target >= files.length) return
      const next = [...files]
      const item = next[index]
      const other = next[target]
      if (!item || !other) return
      next[index] = other
      next[target] = item
      onFiles(next)
    },
    [files, onFiles],
  )

  return (
    <div className="dropzone-wrapper">
      <div
        className={`dropzone${dragging ? ' is-dragging' : ''}${disabled ? ' is-disabled' : ''}`}
        onDragOver={(e) => {
          e.preventDefault()
          if (!disabled) setDragging(true)
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
        data-testid="dropzone"
      >
        <input
          ref={inputRef}
          type="file"
          className="visually-hidden"
          accept={tool.accept}
          multiple={tool.multiple === true}
          onChange={onPick}
          disabled={disabled}
          data-testid="file-input"
          aria-label={`Choose ${tool.from} file${tool.multiple ? 's' : ''}`}
        />
        <p className="dropzone-title">
          {files.length === 0 ? 'Drop your file here' : 'Drop another file, or add more'}
        </p>
        <p className="dropzone-sub">
          {tool.multiple ? `Up to ${Math.floor(maxBytes / (1024 * 1024))}MB each` : `Up to ${formatBytes(maxBytes)}`}
        </p>
        <button
          type="button"
          className="button button-primary"
          onClick={() => inputRef.current?.click()}
          disabled={disabled}
        >
          Choose {tool.multiple ? 'files' : 'file'}
        </button>
      </div>

      {rejected ? (
        <p className="alert alert-warn" role="status">
          {rejected}
        </p>
      ) : null}

      {files.length > 0 ? (
        <ul className="file-list" aria-label="Selected files">
          {files.map((file, index) => (
            <li key={`${file.name}-${index}`} className="file-item">
              <span className="file-name" title={file.name}>
                {file.name}
              </span>
              <span className="file-size">{formatBytes(file.size)}</span>
              {tool.multiple ? (
                <span className="file-actions">
                  <button
                    type="button"
                    className="button button-ghost button-icon"
                    onClick={() => move(index, -1)}
                    disabled={index === 0}
                    aria-label={`Move ${file.name} earlier`}
                  >
                    ↑
                  </button>
                  <button
                    type="button"
                    className="button button-ghost button-icon"
                    onClick={() => move(index, 1)}
                    disabled={index === files.length - 1}
                    aria-label={`Move ${file.name} later`}
                  >
                    ↓
                  </button>
                </span>
              ) : null}
              <button
                type="button"
                className="button button-ghost button-icon"
                onClick={() => remove(index)}
                aria-label={`Remove ${file.name}`}
              >
                ✕
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  )
}