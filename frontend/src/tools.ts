/**
 * Conversion tools available in the UI.
 *
 * Kept as data rather than four hand-written forms: one component then renders
 * any tool, and adding a format means adding an entry here, not a new branch
 * with its own copy of the upload/state/download logic.
 */

export interface ToolId {
  raw: 'pdf-to-image' | 'images-to-pdf' | 'svg-to-image' | 'docx-to-pdf'
}

export interface ToolOption {
  key: string
  label: string
  kind: 'select' | 'number' | 'checkbox' | 'page'
  help?: string
  defaultValue: string | number | boolean
  choices?: Array<{ value: string; label: string }>
  min?: number
  max?: number
}

export interface Tool {
  id: ToolId['raw']
  name: string
  from: string
  to: string
  summary: string
  /** Short forms for the tab strip, where horizontal space is tight. */
  tabFrom?: string
  tabTo?: string
  /** Multiple files become multiple pages, in this order. */
  multiple?: boolean
  accept: string
  /** Extension list for the accept attribute and client-side checking. */
  extensions: string[]
  /** Shown under the drop zone; the honest limits of the conversion. */
  notes: string[]
  options: ToolOption[]
  /** Formats this tool can emit. Never includes svg. */
  outputs: Array<'png' | 'jpeg' | 'pdf' | 'zip'>
}

export const TOOLS: Tool[] = [
  {
    id: 'pdf-to-image',
    name: 'PDF to image',
    from: 'PDF',
    to: 'PNG or JPEG',
    summary: 'Render every page of a PDF to a separate image.',
    accept: '.pdf,application/pdf',
    extensions: ['pdf'],
    outputs: ['png', 'jpeg', 'zip'],
    notes: [
      'Each page becomes one image. More than one page is returned as a ZIP.',
      'Resolution is capped so a document with an enormous page size cannot exhaust memory.',
    ],
    options: [
      {
        key: 'format',
        label: 'Image format',
        kind: 'select',
        defaultValue: 'png',
        choices: [
          { value: 'png', label: 'PNG — lossless, larger' },
          { value: 'jpeg', label: 'JPEG — smaller, slight quality loss' },
        ],
      },
      {
        key: 'dpi',
        label: 'Resolution (DPI)',
        kind: 'number',
        defaultValue: 150,
        min: 36,
        max: 400,
        help: 'Higher means sharper and larger. 150 suits most uses; 300 suits print.',
      },
      {
        key: 'page',
        label: 'Single page',
        kind: 'page',
        defaultValue: '',
        help: 'Leave blank for all pages.',
      },
    ],
  },
  {
    id: 'images-to-pdf',
    name: 'Images to PDF',
    from: 'PNG, JPEG, WebP, GIF',
    to: 'PDF',
    tabFrom: 'Images',
    summary: 'Combine images into one PDF, one image per page.',
    multiple: true,
    accept: '.png,.jpg,.jpeg,.webp,.gif,image/png,image/jpeg,image/webp,image/gif',
    extensions: ['png', 'jpg', 'jpeg', 'webp', 'gif'],
    outputs: ['pdf'],
    notes: [
      'Images are fitted to A4 in the order you add them, never cropped or stretched.',
      'Transparent areas are flattened onto white, since a PDF page is opaque.',
    ],
    options: [
      {
        key: 'compress',
        label: 'Compress images',
        kind: 'checkbox',
        defaultValue: true,
        help: 'Re-encodes as JPEG for a much smaller file. Turn off to embed losslessly.',
      },
    ],
  },
  {
    id: 'svg-to-image',
    name: 'SVG to image',
    from: 'SVG',
    to: 'PNG or JPEG',
    summary: 'Rasterise a vector graphic.',
    accept: '.svg,image/svg+xml',
    extensions: ['svg'],
    outputs: ['png', 'jpeg'],
    notes: [
      'Always returns a PNG or JPEG, never an SVG. SVG is an executable document format, so returning one would let a hostile file run script in your browser.',
      'Set a width and height to control the output size, or leave blank to use the SVG’s own size.',
    ],
    options: [
      {
        key: 'format',
        label: 'Image format',
        kind: 'select',
        defaultValue: 'png',
        choices: [
          { value: 'png', label: 'PNG — keeps transparency' },
          { value: 'jpeg', label: 'JPEG — flattens onto white' },
        ],
      },
      { key: 'width', label: 'Width (px)', kind: 'number', defaultValue: '', min: 1, max: 20000 },
      { key: 'height', label: 'Height (px)', kind: 'number', defaultValue: '', min: 1, max: 20000 },
    ],
  },
  {
    id: 'docx-to-pdf',
    name: 'DOCX to PDF',
    from: 'Word .docx',
    to: 'PDF',
    tabFrom: 'DOCX',
    summary: 'Convert a Word document to PDF.',
    accept: '.docx,application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    extensions: ['docx'],
    outputs: ['pdf'],
    notes: [
      'Headings, paragraphs, bold and italic text, lists and tables are preserved.',
      'This is a pure-Python conversion — no office suite is involved. Exact Word line breaking, images inside the document, headers and footers are not reproduced. See the fidelity note in the documentation.',
    ],
    options: [],
  },
]

export const TOOL_BY_ID = Object.fromEntries(TOOLS.map((tool) => [tool.id, tool])) as Record<
  ToolId['raw'],
  Tool
>