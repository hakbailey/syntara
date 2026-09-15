import { MenuToggle, type MenuToggleElement, SelectList, SelectOption } from '@patternfly/react-core'
import { useState } from 'react'

import { SynSelect } from '../../../components/SynSelect'

import { RUNTIME_ENGINE_LABELS, type RuntimeEngine } from './runtimeEngine'

function isRuntimeEngine(value: string): value is RuntimeEngine {
  return value === 'in_process' || value === 'sandboxed'
}

type RuntimeEngineSelectProps = Readonly<{
  id: string
  value?: RuntimeEngine
  onChange: (value: RuntimeEngine | undefined) => void
  isDisabled?: boolean
  ariaLabel?: string
}>

export function RuntimeEngineSelect({
  id,
  value,
  onChange,
  isDisabled = false,
  ariaLabel = 'Agent runtime',
}: RuntimeEngineSelectProps) {
  const [isOpen, setIsOpen] = useState(false)
  const selectedLabel = value ? RUNTIME_ENGINE_LABELS[value] : 'Use workflow or deployment default'

  return (
    <SynSelect
      id={id}
      selected={value ?? ''}
      isOpen={isOpen}
      onOpenChange={setIsOpen}
      onSelect={(_event, selected) => {
        const selectedValue = String(selected)
        if (selectedValue === '') {
          onChange(undefined)
        } else if (isRuntimeEngine(selectedValue)) {
          onChange(selectedValue)
        }
        setIsOpen(false)
      }}
      toggle={(toggleRef: React.Ref<MenuToggleElement>) => (
        <MenuToggle
          ref={toggleRef}
          onClick={() => setIsOpen((previous) => !previous)}
          isExpanded={isOpen}
          isFullWidth
          isDisabled={isDisabled}
          aria-label={ariaLabel}
        >
          {selectedLabel}
        </MenuToggle>
      )}
    >
      <SelectList>
        <SelectOption value="" isSelected={!value}>
          Use workflow or deployment default
        </SelectOption>
        <SelectOption value="in_process" isSelected={value === 'in_process'}>
          {RUNTIME_ENGINE_LABELS.in_process}
        </SelectOption>
        <SelectOption value="sandboxed" isSelected={value === 'sandboxed'}>
          {RUNTIME_ENGINE_LABELS.sandboxed}
        </SelectOption>
      </SelectList>
    </SynSelect>
  )
}
