export type SignatureConfig = {
  signature_id: number;
  position: string;
  position_x?: number;
  position_y?: number;
  page: string | number;
};

const PAGE_OPTIONS = [
  { value: 'last', label: 'Last Page' },
  { value: 'first', label: 'First Page' },
  { value: 'all', label: 'All Pages' },
  { value: 'custom', label: 'Specific Page' },
];

interface Props {
  signatures: any[];
  value: SignatureConfig[];
  onChange: (configs: SignatureConfig[]) => void;
}

export default function SignatureChecklist({ signatures, value, onChange }: Props) {
  const isSignatureSelected = (sigId: number) =>
    value.some((c) => c.signature_id === sigId);

  const getSignatureConfig = (sigId: number): SignatureConfig | undefined =>
    value.find((c) => c.signature_id === sigId);

  const toggleSignature = (sigId: number) => {
    if (isSignatureSelected(sigId)) {
      onChange(value.filter((c) => c.signature_id !== sigId));
      return;
    }
    const sig = signatures.find((s) => s.id === sigId);
    const next: SignatureConfig = {
      signature_id: sigId,
      position: sig?.default_position || 'bottom-right',
      page: 'last',
    };
    onChange([...value, next]);
  };

  const updateSignatureConfig = (sigId: number, updates: Partial<SignatureConfig>) => {
    onChange(value.map((c) => (c.signature_id === sigId ? { ...c, ...updates } : c)));
  };

  if (signatures.length === 0) {
    return (
      <p className="text-xs text-muted-foreground">
        No signatures available. Add signatures in Settings.
      </p>
    );
  }

  return (
    <div className="space-y-2">
      {signatures.map((sig) => {
        const selected = isSignatureSelected(sig.id);
        const config = getSignatureConfig(sig.id);
        return (
          <div
            key={sig.id}
            className={`rounded-lg border transition-colors ${
              selected ? 'border-drpl-secondary bg-accent/10' : 'border-border hover:border-border'
            }`}
          >
            <label className="flex items-center gap-3 p-3 cursor-pointer">
              <input
                type="checkbox"
                checked={selected}
                onChange={() => toggleSignature(sig.id)}
                className="rounded border-border text-accent focus:ring-drpl-secondary"
              />
              <div>
                <p className="text-sm font-medium text-foreground">{sig.name}</p>
                {sig.designation && (
                  <p className="text-xs text-muted-foreground">{sig.designation}</p>
                )}
              </div>
            </label>

            {selected && config && (
              <div className="px-3 pb-3 pt-1 border-t border-border space-y-2">
                <div>
                  <label className="block text-[10px] font-medium text-muted-foreground mb-0.5">
                    Page
                  </label>
                  <select
                    value={typeof config.page === 'number' ? 'custom' : config.page}
                    onChange={(e) => {
                      const val = e.target.value;
                      updateSignatureConfig(sig.id, {
                        page: val === 'custom' ? 1 : val,
                      });
                    }}
                    className="w-full border border-border rounded px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-drpl-secondary"
                  >
                    {PAGE_OPTIONS.map((p) => (
                      <option key={p.value} value={p.value}>
                        {p.label}
                      </option>
                    ))}
                  </select>
                </div>

                {typeof config.page === 'number' && (
                  <div>
                    <label className="block text-[10px] font-medium text-muted-foreground mb-0.5">
                      Page Number
                    </label>
                    <input
                      type="number"
                      value={config.page}
                      onChange={(e) =>
                        updateSignatureConfig(sig.id, {
                          page: Math.max(1, Number(e.target.value)),
                        })
                      }
                      min={1}
                      className="w-full border border-border rounded px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-drpl-secondary"
                    />
                  </div>
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
