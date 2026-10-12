import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import './styles.css'

const container = document.getElementById('root')
if (!container) {
  // Fail loudly rather than rendering nothing: a missing #root means the
  // built HTML and this bundle disagree, which is a build problem, not a
  // runtime one to paper over.
  throw new Error('Root element #root not found in the document.')
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
)