/**
 * Component tests for the conversion flow.
 *
 * The focus is the behaviour a user depends on and the security properties
 * the UI must not violate — in particular that a filename coming back from
 * the server is rendered as text and used as a download name, never injected
 * as markup.
 *
 * fetch is mocked rather than stubbed at a higher level, because the client
 * module's whole job is to construct the right request and interpret the
 * right response; mocking around it would test nothing.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Converter } from './Converter'
import { TOOL_BY_ID } from '../tools'

const pdfTool = TOOL_BY_ID['pdf-to-image']
const imagesTool = TOOL_BY_ID['images-to-pdf']
const docxTool = TOOL_BY_ID['docx-to-pdf']

function pdfFile(name = 'document.pdf', size = 1024): File {
  return new File([new Uint8Array(size)], name, { type: 'application/pdf' })
}

function pngFile(name = 'photo.png', size = 512): File {
  return new File([new Uint8Array(size)], name, { type: 'image/png' })
}

function okResponse(body: BlobPart, init: { type?: string; headers?: Record<string, string> } = {}) {
  return new Response(body, {
    status: 200,
    headers: { 'Content-Type': init.type ?? 'application/octet-stream', ...init.headers },
  })
}

function errorResponse(status: number, payload: unknown, headers: Record<string, string> = {}) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json', ...headers },
  })
}

/** Select files the normal way: the input's `accept` attribute filters them. */
async function chooseFiles(files: File[]) {
  const input = screen.getByTestId('file-input')
  await userEvent.upload(input, files)
}

/**
 * Select a file that the `accept` attribute would block.
 *
 * `userEvent.upload` honours the accept list itself and silently drops a
 * non-matching file, so it can never exercise the extension check. A real user
 * hits this by choosing "All Files" in the OS dialog, or by drag-and-drop,
 * which does not consult `accept` at all. `fireEvent.change` bypasses the
 * filtering and reproduces that path.
 */
async function chooseFilesBypassingAccept(files: File[]) {
  const input = screen.getByTestId('file-input')
  fireEvent.change(input, { target: { files } })
}

beforeEach(() => {
  vi.restoreAllMocks()
})

describe('file selection', () => {
  it('disables Convert until a file is chosen', () => {
    render(<Converter tool={pdfTool} maxBytes={1024 * 1024} />)
    expect(screen.getByTestId('convert-button')).toBeDisabled()
  })

  it('enables Convert once a valid file is chosen', async () => {
    render(<Converter tool={pdfTool} maxBytes={1024 * 1024} />)
    await chooseFiles([pdfFile()])
    expect(screen.getByTestId('convert-button')).toBeEnabled()
  })

  it('rejects a file with the wrong extension before uploading it', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch')
    render(<Converter tool={pdfTool} maxBytes={1024 * 1024} />)

    await chooseFilesBypassingAccept([
      new File([new Uint8Array(10)], 'malware.exe', { type: 'application/x-msdownload' }),
    ])

    // The dropzone rejection alert and the status area both use role=status,
    // so match on the message text rather than on the role.
    expect(await screen.findByText(/not a pdf file/i)).toBeInTheDocument()
    expect(screen.getByTestId('convert-button')).toBeDisabled()
    expect(fetchSpy).not.toHaveBeenCalled()
  })

  it('rejects an empty file', async () => {
    render(<Converter tool={pdfTool} maxBytes={1024 * 1024} />)
    await chooseFilesBypassingAccept([new File([], 'empty.pdf', { type: 'application/pdf' })])
    expect(await screen.findByRole('status')).toHaveTextContent(/the file is empty/i)
  })

  it('rejects a file over the client size limit', async () => {
    render(<Converter tool={pdfTool} maxBytes={1024} />)
    await chooseFilesBypassingAccept([pdfFile('big.pdf', 4096)])
    expect(await screen.findByRole('status')).toHaveTextContent(/larger than the .* limit/i)
  })

  it('appends files in order for a multi-file tool', async () => {
    render(<Converter tool={imagesTool} maxBytes={1024 * 1024} />)
    await chooseFiles([pngFile('one.png'), pngFile('two.png')])
    await chooseFiles([pngFile('three.png')])

    const list = screen.getByRole('list', { name: /selected files/i })
    const names = within(list).getAllByRole('listitem').map((li) => li.textContent ?? '')
    expect(names).toHaveLength(3)
    expect(names[0]).toContain('one.png')
    expect(names[2]).toContain('three.png')
  })

  it('replaces the file for a single-file tool', async () => {
    render(<Converter tool={pdfTool} maxBytes={1024 * 1024} />)
    await chooseFiles([pdfFile('first.pdf')])
    await chooseFiles([pdfFile('second.pdf')])
    const list = screen.getByRole('list', { name: /selected files/i })
    expect(within(list).getAllByRole('listitem')).toHaveLength(1)
    expect(list).toHaveTextContent('second.pdf')
  })

  it('removes a file', async () => {
    render(<Converter tool={pdfTool} maxBytes={1024 * 1024} />)
    await chooseFiles([pdfFile()])
    await userEvent.click(screen.getByRole('button', { name: /remove/i }))
    expect(screen.getByTestId('convert-button')).toBeDisabled()
  })

  it('reorders images to set page order', async () => {
    render(<Converter tool={imagesTool} maxBytes={1024 * 1024} />)
    await chooseFiles([pngFile('first.png'), pngFile('second.png')])
    await userEvent.click(screen.getByRole('button', { name: /move second\.png earlier/i }))

    const list = screen.getByRole('list', { name: /selected files/i })
    const names = within(list).getAllByRole('listitem').map((li) => li.textContent ?? '')
    expect(names[0]).toContain('second.png')
    expect(names[1]).toContain('first.png')
  })
})

describe('conversion', () => {
  it('posts the file to the right endpoint and downloads the result', async () => {
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(
        okResponse(new Uint8Array([1, 2, 3]), {
          type: 'image/png',
          headers: { 'Content-Disposition': 'attachment; filename="document-page-0001.png"' },
        }),
      )

    render(<Converter tool={pdfTool} maxBytes={1024 * 1024} />)
    await chooseFiles([pdfFile()])
    await userEvent.click(screen.getByTestId('convert-button'))

    await waitFor(() => expect(fetchSpy).toHaveBeenCalled())

    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit]
    expect(url).toContain('/api/pdf-to-image')
    expect(url).toContain('format=png')
    expect(init.method).toBe('POST')
    expect(init.body).toBeInstanceOf(FormData)

    expect(await screen.findByTestId('result')).toHaveTextContent('document-page-0001.png')
  })

  it('sends every selected image for a multi-file tool', async () => {
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(okResponse(new Uint8Array([1]), { type: 'application/pdf' }))

    render(<Converter tool={imagesTool} maxBytes={1024 * 1024} />)
    await chooseFiles([pngFile('a.png'), pngFile('b.png'), pngFile('c.png')])
    await userEvent.click(screen.getByTestId('convert-button'))

    await waitFor(() => expect(fetchSpy).toHaveBeenCalled())
    const body = (fetchSpy.mock.calls[0] as [string, RequestInit])[1].body as FormData
    expect(body.getAll('files')).toHaveLength(3)
  })

  it('passes the chosen DPI through', async () => {
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(okResponse(new Uint8Array([1]), { type: 'image/png' }))

    render(<Converter tool={pdfTool} maxBytes={1024 * 1024} />)
    await chooseFiles([pdfFile()])
    const dpi = screen.getByLabelText(/resolution/i)
    await userEvent.clear(dpi)
    await userEvent.type(dpi, '300')
    await userEvent.click(screen.getByTestId('convert-button'))

    await waitFor(() => expect(fetchSpy).toHaveBeenCalled())
    expect((fetchSpy.mock.calls[0] as [string, RequestInit])[0]).toContain('dpi=300')
  })

  it('omits the page parameter when the field is blank', async () => {
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(okResponse(new Uint8Array([1]), { type: 'image/png' }))

    render(<Converter tool={pdfTool} maxBytes={1024 * 1024} />)
    await chooseFiles([pdfFile()])
    await userEvent.click(screen.getByTestId('convert-button'))

    await waitFor(() => expect(fetchSpy).toHaveBeenCalled())
    expect((fetchSpy.mock.calls[0] as [string, RequestInit])[0]).not.toContain('page=')
  })

  it('stays interactive and reports a ZIP result for a multi-page PDF', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      okResponse(new Uint8Array([1, 2]), {
        type: 'application/zip',
        headers: { 'Content-Disposition': 'attachment; filename="document.zip"' },
      }),
    )

    render(<Converter tool={pdfTool} maxBytes={1024 * 1024} />)
    await chooseFiles([pdfFile()])
    await userEvent.click(screen.getByTestId('convert-button'))

    expect(await screen.findByTestId('result')).toHaveTextContent('document.zip')
  })
})

describe('errors', () => {
  it('shows the server message for a rejected file', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      errorResponse(422, {
        error: { code: 'corrupt_file', message: 'That file could not be read.' },
      }),
    )

    render(<Converter tool={pdfTool} maxBytes={1024 * 1024} />)
    await chooseFiles([pdfFile()])
    await userEvent.click(screen.getByTestId('convert-button'))

    const alert = await screen.findByTestId('error')
    expect(alert).toHaveTextContent('That file could not be read.')
    expect(alert).toHaveAttribute('data-error-code', 'corrupt_file')
  })

  it('adds the real limit to a too-large error', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      errorResponse(413, {
        error: { code: 'file_too_large', message: 'That file is larger than the upload limit.', limit_mb: 25 },
      }),
    )

    render(<Converter tool={pdfTool} maxBytes={1024 * 1024} />)
    await chooseFiles([pdfFile()])
    await userEvent.click(screen.getByTestId('convert-button'))

    expect(await screen.findByTestId('error')).toHaveTextContent(/the limit is 25mb/i)
  })

  it('explains a rate limit with the retry delay when the server sends one', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      errorResponse(
        429,
        { error: { code: 'rate_limited', message: 'Too many requests.' } },
        { 'Retry-After': '42' },
      ),
    )

    render(<Converter tool={pdfTool} maxBytes={1024 * 1024} />)
    await chooseFiles([pdfFile()])
    await userEvent.click(screen.getByTestId('convert-button'))

    expect(await screen.findByTestId('error')).toHaveTextContent(/try again in 42 seconds/i)
  })

  it('falls back to a generic rate-limit message without Retry-After', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      errorResponse(429, { error: { code: 'rate_limited', message: 'Too many requests.' } }),
    )

    render(<Converter tool={pdfTool} maxBytes={1024 * 1024} />)
    await chooseFiles([pdfFile()])
    await userEvent.click(screen.getByTestId('convert-button'))

    expect(await screen.findByTestId('error')).toHaveTextContent(/please wait a moment/i)
  })

  it('never renders a non-JSON error body as text', async () => {
    // A misconfigured proxy can answer with an HTML error page. Its markup
    // must not reach the DOM as visible text.
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response('<html><body><h1>502 Bad Gateway</h1></body></html>', {
        status: 502,
        headers: { 'Content-Type': 'text/html' },
      }),
    )

    render(<Converter tool={pdfTool} maxBytes={1024 * 1024} />)
    await chooseFiles([pdfFile()])
    await userEvent.click(screen.getByTestId('convert-button'))

    const alert = await screen.findByTestId('error')
    expect(alert).toHaveTextContent(/conversion failed \(http 502\)/i)
    expect(alert).not.toHaveTextContent(/bad gateway/i)
    expect(document.body.querySelector('h1')).toBeNull()
  })

  it('explains an unsafe_file rejection without guessing the cause', async () => {
    // The server intentionally does not say which check fired. The UI must
    // therefore list the plausible categories rather than asserting one --
    // the earlier wording claimed "larger than it appears, or a malformed
    // archive", which is simply wrong for a hostile SVG.
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      errorResponse(422, {
        error: { code: 'unsafe_file', message: 'That file was rejected by a safety check.' },
      }),
    )

    render(<Converter tool={pdfTool} maxBytes={1024 * 1024} />)
    await chooseFiles([pdfFile()])
    await userEvent.click(screen.getByTestId('convert-button'))

    const alert = await screen.findByTestId('error')
    expect(alert).toHaveAttribute('data-error-code', 'unsafe_file')
    expect(alert).toHaveTextContent(/active content/i)
    expect(alert).toHaveTextContent(/expands far beyond its real size/i)
    expect(alert).not.toHaveTextContent(/malformed archive/i)
  })

  it('reports a network failure distinctly', async () => {
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new TypeError('Failed to fetch'))

    render(<Converter tool={pdfTool} maxBytes={1024 * 1024} />)
    await chooseFiles([pdfFile()])
    await userEvent.click(screen.getByTestId('convert-button'))

    const alert = await screen.findByTestId('error')
    expect(alert).toHaveAttribute('data-error-code', 'network_error')
    expect(alert).toHaveTextContent(/could not reach the conversion service/i)
  })

  it('shows a progress state while converting', async () => {
    let release: (() => void) | undefined
    vi.spyOn(globalThis, 'fetch').mockImplementation(
      () =>
        new Promise<Response>((resolve) => {
          release = () => resolve(okResponse(new Uint8Array([1]), { type: 'image/png' }))
        }),
    )

    render(<Converter tool={pdfTool} maxBytes={1024 * 1024} />)
    await chooseFiles([pdfFile()])
    await userEvent.click(screen.getByTestId('convert-button'))

    expect(await screen.findByTestId('progress')).toBeInTheDocument()
    expect(screen.getByTestId('convert-button')).toBeDisabled()
    expect(screen.getByRole('button', { name: /cancel/i })).toBeInTheDocument()

    release?.()
    await screen.findByTestId('result')
  })
})

describe('XSS resistance', () => {
  it('renders a hostile server filename as text, not markup', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      okResponse(new Uint8Array([1]), {
        type: 'image/png',
        headers: {
          'Content-Disposition':
            'attachment; filename="<img src=x onerror=\'window.__pwned=1\'>.png"',
        },
      }),
    )

    render(<Converter tool={pdfTool} maxBytes={1024 * 1024} />)
    await chooseFiles([pdfFile()])
    await userEvent.click(screen.getByTestId('convert-button'))

    await screen.findByTestId('result')
    // The element must not exist: nothing should have been injected.
    expect(document.querySelector('img[src="x"]')).toBeNull()
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined()
  })

  it('escapes a hostile error message rather than interpreting it', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      errorResponse(422, {
        error: { code: 'corrupt_file', message: '<img src=x onerror="window.__pwned=2">bad file' },
      }),
    )

    render(<Converter tool={pdfTool} maxBytes={1024 * 1024} />)
    await chooseFiles([pdfFile()])
    await userEvent.click(screen.getByTestId('convert-button'))

    const alert = await screen.findByTestId('error')
    expect(alert.querySelector('img')).toBeNull()
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined()
    // The text is present, just inert.
    expect(alert.textContent).toContain('onerror')
  })

  it('never puts the response content type into the download', async () => {
    const createSpy = vi.spyOn(URL, 'createObjectURL')
    // A hostile backend answers with SVG despite a PNG request.
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      okResponse(new Uint8Array([1]), { type: 'image/svg+xml' }),
    )

    render(<Converter tool={pdfTool} maxBytes={1024 * 1024} />)
    await chooseFiles([pdfFile()])
    await userEvent.click(screen.getByTestId('convert-button'))

    await screen.findByTestId('result')
    expect(createSpy).toHaveBeenCalled()
    const blob = createSpy.mock.calls[0]?.[0] as Blob | undefined
    expect(blob?.type).toBe('image/png')
    expect(blob?.type).not.toContain('svg')
  })
})

describe('docx tool', () => {
  it('states its fidelity limits instead of implying Word parity', () => {
    render(<Converter tool={docxTool} maxBytes={1024 * 1024} />)
    const notes = screen.getByText(/what this conversion does and does not do/i)
    expect(notes).toBeInTheDocument()
    expect(document.body.textContent).toMatch(/pure-python/i)
    expect(document.body.textContent).toMatch(/not reproduced|are not reproduced/i)
  })
})