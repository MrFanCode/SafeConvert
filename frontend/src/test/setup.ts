import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach, beforeEach, vi } from 'vitest'

beforeEach(() => {
  // jsdom has no URL.createObjectURL. Every test needs it because a successful
  // conversion always ends in a download, so provide a stub globally rather
  // than repeating it per test.
  if (!URL.createObjectURL) {
    Object.defineProperty(URL, 'createObjectURL', {
      writable: true,
      value: vi.fn(() => 'blob:mock'),
    })
  }
  if (!URL.revokeObjectURL) {
    Object.defineProperty(URL, 'revokeObjectURL', {
      writable: true,
      value: vi.fn(),
    })
  }
})

afterEach(() => {
  cleanup()
})