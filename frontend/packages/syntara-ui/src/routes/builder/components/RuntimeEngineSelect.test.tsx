import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { describe, expect, it } from 'vitest'
import { axe } from 'vitest-axe'

import type { RuntimeEngine } from './runtimeEngine'
import { RuntimeEngineSelect } from './RuntimeEngineSelect'

function Harness() {
  const [value, setValue] = useState<RuntimeEngine | undefined>()
  return <RuntimeEngineSelect id="runtime-engine" value={value} onChange={setValue} />
}

describe('RuntimeEngineSelect', () => {
  it('has no accessibility violations', async () => {
    const { container } = render(<Harness />)
    expect(await axe(container)).toHaveNoViolations()
  })

  it('updates the selected runtime engine', async () => {
    render(<Harness />)
    const user = userEvent.setup()

    await user.click(screen.getByRole('button', { name: 'Agent runtime' }))
    await user.click(screen.getByRole('option', { name: 'Sandboxed' }))

    expect(screen.getByRole('button', { name: 'Agent runtime' })).toHaveTextContent('Sandboxed')
  })
})
