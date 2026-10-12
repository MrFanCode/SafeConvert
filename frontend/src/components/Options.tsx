import type { Tool, ToolOption } from '../tools'

interface OptionsProps {
  tool: Tool
  values: Record<string, string | number | boolean>
  onChange: (key: string, value: string | number | boolean) => void
  disabled?: boolean
}

export function Options({ tool, values, onChange, disabled }: OptionsProps) {
  if (tool.options.length === 0) return null

  return (
    <fieldset className="options" disabled={disabled}>
      <legend className="visually-hidden">Conversion settings</legend>
      {tool.options.map((option) => (
        <OptionField
          key={option.key}
          option={option}
          value={values[option.key] ?? option.defaultValue}
          onChange={(next) => onChange(option.key, next)}
        />
      ))}
    </fieldset>
  )
}

interface OptionFieldProps {
  option: ToolOption
  value: string | number | boolean
  onChange: (value: string | number | boolean) => void
}

function OptionField({ option, value, onChange }: OptionFieldProps) {
  const id = `opt-${option.key}`

  if (option.kind === 'checkbox') {
    return (
      <div className="option option-checkbox">
        <input
          id={id}
          type="checkbox"
          checked={Boolean(value)}
          onChange={(e) => onChange(e.target.checked)}
        />
        <div>
          <label htmlFor={id}>{option.label}</label>
          {option.help ? <p className="option-help">{option.help}</p> : null}
        </div>
      </div>
    )
  }

  if (option.kind === 'select') {
    return (
      <div className="option">
        <label htmlFor={id}>{option.label}</label>
        <select
          id={id}
          value={String(value)}
          onChange={(e) => onChange(e.target.value)}
        >
          {option.choices?.map((choice) => (
            <option key={choice.value} value={choice.value}>
              {choice.label}
            </option>
          ))}
        </select>
        {option.help ? <p className="option-help">{option.help}</p> : null}
      </div>
    )
  }

  // number and page are both free-text: the "single page" field must accept
  // blank (meaning every page), and a number input treats '' as 0.
  const isNumber = option.kind === 'number'
  const text = value === '' || value === undefined ? '' : String(value)

  return (
    <div className="option">
      <label htmlFor={id}>{option.label}</label>
      <input
        id={id}
        type={isNumber ? 'number' : 'text'}
        inputMode={isNumber ? 'numeric' : 'numeric'}
        value={text}
        min={option.min}
        max={option.max}
        placeholder={option.kind === 'page' ? 'All pages' : undefined}
        onChange={(e) => {
          const raw = e.target.value
          if (!isNumber) {
            onChange(raw)
            return
          }
          onChange(raw === '' ? '' : Number(raw))
        }}
      />
      {option.help ? <p className="option-help">{option.help}</p> : null}
    </div>
  )
}