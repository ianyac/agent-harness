import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { PermissionPrompt } from './PermissionPrompt'

describe('PermissionPrompt', () => {
  it('shows the tool, args, and three answers', async () => {
    const onAnswer = vi.fn()
    render(
      <PermissionPrompt
        name="bash" args={{ command: 'rm -rf build' }} answer={null} onAnswer={onAnswer}
      />,
    )
    expect(screen.getByText(/bash/)).toBeInTheDocument()
    expect(screen.getByText(/rm -rf build/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /always/i }))
    expect(onAnswer).toHaveBeenCalledWith('always')
  })

  it('renders the recorded decision without buttons once answered', () => {
    render(<PermissionPrompt name="bash" args={{}} answer="no" />)
    expect(screen.getByText(/denied/i)).toBeInTheDocument()
    expect(screen.queryAllByRole('button')).toHaveLength(0)
  })
})
