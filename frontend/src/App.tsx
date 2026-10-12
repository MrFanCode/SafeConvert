import { useEffect, useState } from 'react'
import { fetchHealth } from './api/client'
import { Converter } from './components/Converter'
import { TOOLS } from './tools'

/**
 * Matches the backend's default `max_upload_bytes` (25 MB). The server is the
 * authority — this exists so an oversized file is refused locally instead of
 * being uploaded first, not as a replacement for the server check.
 */
const MAX_UPLOAD_BYTES = 25 * 1024 * 1024

export default function App() {
  const [active, setActive] = useState(TOOLS[0]?.id ?? 'pdf-to-image')
  const [health, setHealth] = useState<'checking' | 'ok' | 'down'>('checking')

  useEffect(() => {
    const controller = new AbortController()
    void fetchHealth(controller.signal).then((ok) => {
      if (!controller.signal.aborted) setHealth(ok ? 'ok' : 'down')
    })
    return () => controller.abort()
  }, [])

  const tool = TOOLS.find((t) => t.id === active) ?? TOOLS[0]

  return (
    <div className="app">
      <header className="site-header">
        <div className="wrap">
          <div className="brand">
            <span className="brand-mark" aria-hidden="true">
              ⬡
            </span>
            <div>
              <h1>SafeConvert</h1>
              <p className="brand-tag">Private file conversion. No account, no storage, no tracking.</p>
            </div>
          </div>
          <ServiceStatus state={health} />
        </div>
      </header>

      <main className="wrap">
        <section className="intro">
          <h2>Convert a file without handing it to a stranger</h2>
          <p>
            Your file is processed in memory on the server, returned to you, and never written to
            disk. Uploads are checked by their actual contents, not their filename, and every result
            comes back as a PNG, JPEG or PDF — never as an SVG, which could otherwise run script in
            your browser.
          </p>
          <ul className="promises">
            <li>
              <strong>Nothing is kept.</strong> The upload exists in memory for the length of one
              request.
            </li>
            <li>
              <strong>No LibreOffice.</strong> The container ships no office suite, so it inherits
              none of that attack surface.
            </li>
            <li>
              <strong>Permissive licences.</strong> The PDF engine is BSD/Apache, not AGPL.
            </li>
          </ul>
        </section>

        <nav className="tool-tabs" aria-label="Conversion tools">
          {TOOLS.map((t) => (
            <button
              key={t.id}
              type="button"
              className={`tab${t.id === active ? ' is-active' : ''}`}
              onClick={() => setActive(t.id)}
              aria-current={t.id === active ? 'page' : undefined}
              aria-label={`Convert ${t.from} to ${t.to}`}
              data-testid={`tab-${t.id}`}
            >
              {/* Short labels only. The full accepted-format list lives on the
                  converter body below, and rendering it in the tab made the
                  label wrap mid-word ("PNG, JPEG, WebP, / GIF", "→ PD / F")
                  at 202px. `aria-label` keeps the accessible name complete
                  even though the visible text is abbreviated. */}
              <span className="tab-from" aria-hidden="true">
                {t.tabFrom ?? t.from}
              </span>
              <span className="tab-arrow" aria-hidden="true">
                →
              </span>
              <span className="tab-to" aria-hidden="true">
                {t.tabTo ?? t.to}
              </span>
            </button>
          ))}
        </nav>

        {tool ? <Converter tool={tool} maxBytes={MAX_UPLOAD_BYTES} /> : null}
      </main>

      <footer className="site-footer">
        <div className="wrap">
	<center>
	<p> Copyright (c) 2026 MrFanCode - <a href="https://github.com/MrFanCode">github.com/MrFanCode</a></p>
	</center>
        </div>
      </footer>
    </div>
  )
}

function ServiceStatus({ state }: { state: 'checking' | 'ok' | 'down' }) {
  const label =
    state === 'ok' ? 'Service online' : state === 'down' ? 'Service unreachable' : 'Checking service…'
  return (
    <span
      className={`status-pill status-${state}`}
      // Text carries the meaning; the colour is redundant reinforcement, so no
      // aria-live here — it would chatter on every check.
      data-testid="service-status"
    >
      <span className="status-dot" aria-hidden="true" />
      {label}
    </span>
  )
}
