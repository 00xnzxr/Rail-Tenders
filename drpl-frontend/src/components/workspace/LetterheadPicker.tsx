interface Props {
  templates: any[];
  value: number | null;
  onChange: (id: number | null) => void;
  disabled?: boolean;
}

export default function LetterheadPicker({ templates, value, onChange, disabled }: Props) {
  const hasMatchingTemplate =
    value == null || templates.some((t) => t.id === value);

  return (
    <select
      value={value == null ? '' : String(value)}
      onChange={(e) => onChange(e.target.value === '' ? null : Number(e.target.value))}
      disabled={disabled}
      className="border border-border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-drpl-secondary disabled:opacity-60"
    >
      <option value="">No letterhead</option>
      {templates.map((t) => (
        <option key={t.id} value={t.id}>{t.name}</option>
      ))}
      {!hasMatchingTemplate && value != null && (
        <option value={String(value)} disabled>
          Template #{value} (deleted)
        </option>
      )}
    </select>
  );
}
