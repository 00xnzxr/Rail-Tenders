import { useState, useEffect, useCallback, useRef } from 'react';
import {
  Plus, Upload, Trash2, FileSpreadsheet, ChevronLeft, Loader2,
  Layers, Package, AlertCircle, CheckCircle2,
} from 'lucide-react';
import { toast } from 'sonner';

import Header from '../../components/layout/Header';
import {
  listRatecards, createRatecard, getRatecard, deleteRatecard,
  uploadRatecardFile, previewRatecardFile, listRatecardItems,
  type Ratecard, type RatecardDetail, type RatecardItem, type RatecardPreview,
} from '../../lib/api';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter,
} from '@/components/ui/dialog';
import { Skeleton } from '@/components/ui/skeleton';

export default function RatecardsPage() {
  const [ratecards, setRatecards] = useState<Ratecard[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [createOpen, setCreateOpen] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      setRatecards(await listRatecards());
    } catch {
      toast.error('Failed to load ratecards');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  if (selectedId != null) {
    return (
      <RatecardDetailView
        ratecardId={selectedId}
        onBack={() => { setSelectedId(null); refresh(); }}
      />
    );
  }

  return (
    <div className="flex flex-col h-full">
      <Header title="Ratecards" subtitle="Structured OEM / Fleetguard price lists used as the primary source for component costing" />

      <div className="flex-1 overflow-y-auto p-4 sm:p-6 lg:p-8">
        <div className="max-w-5xl mx-auto">
          <div className="flex items-center justify-between mb-6">
            <p className="text-sm text-muted-foreground">
              {ratecards.length} ratecard{ratecards.length === 1 ? '' : 's'}
            </p>
            <Button variant="accent" onClick={() => setCreateOpen(true)}>
              <Plus size={16} />
              New Ratecard
            </Button>
          </div>

          {loading ? (
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              {[0, 1, 2, 3].map((i) => <Skeleton key={i} className="h-28 rounded-xl" />)}
            </div>
          ) : ratecards.length === 0 ? (
            <div className="text-center py-16 border-2 border-dashed border-border rounded-xl">
              <FileSpreadsheet size={48} className="mx-auto text-muted-foreground/40 mb-4" />
              <h3 className="text-lg font-semibold text-foreground mb-1">No ratecards yet</h3>
              <p className="text-sm text-muted-foreground mb-6">
                Create a ratecard, then upload an Excel price list to populate it.
              </p>
              <Button variant="accent" onClick={() => setCreateOpen(true)}>
                <Plus size={16} />
                New Ratecard
              </Button>
            </div>
          ) : (
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              {ratecards.map((rc) => (
                <button
                  key={rc.id}
                  onClick={() => setSelectedId(rc.id)}
                  className="text-left bg-card rounded-xl border border-border p-5 shadow-card hover:shadow-card-hover hover:border-accent/40 transition-all"
                >
                  <div className="flex items-start gap-3">
                    <div className="p-2.5 rounded-lg bg-accent/10 text-accent shrink-0">
                      <FileSpreadsheet size={20} />
                    </div>
                    <div className="min-w-0 flex-1">
                      <h3 className="font-semibold text-foreground truncate">{rc.name}</h3>
                      {rc.source_label && (
                        <p className="text-xs text-muted-foreground">{rc.source_label}</p>
                      )}
                      <div className="flex items-center gap-3 mt-3 text-xs text-muted-foreground">
                        <span className="inline-flex items-center gap-1">
                          <Package size={13} /> {rc.item_count} parts
                        </span>
                        <span className="inline-flex items-center gap-1">
                          <Layers size={13} /> {rc.check_schedule_count} kits
                        </span>
                      </div>
                    </div>
                  </div>
                </button>
              ))}
            </div>
          )}
        </div>
      </div>

      <CreateRatecardDialog
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onCreated={(id) => { setCreateOpen(false); refresh(); setSelectedId(id); }}
      />
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────

function CreateRatecardDialog({
  open, onClose, onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: (id: number) => void;
}) {
  const [name, setName] = useState('');
  const [sourceLabel, setSourceLabel] = useState('');
  const [description, setDescription] = useState('');
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (open) { setName(''); setSourceLabel(''); setDescription(''); }
  }, [open]);

  const submit = async () => {
    if (!name.trim()) return;
    setSaving(true);
    try {
      const rc = await createRatecard({
        name: name.trim(),
        source_label: sourceLabel.trim() || undefined,
        description: description.trim() || undefined,
      });
      toast.success(`Ratecard "${rc.name}" created`);
      onCreated(rc.id);
    } catch (e: any) {
      toast.error(e?.response?.data?.detail || 'Failed to create ratecard');
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>New Ratecard</DialogTitle>
        </DialogHeader>
        <div className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="rc-name">Name</Label>
            <Input
              id="rc-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. Cummins/Fleetguard FY26"
              autoFocus
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="rc-source">Default source label <span className="text-muted-foreground font-normal">(optional)</span></Label>
            <Input
              id="rc-source"
              value={sourceLabel}
              onChange={(e) => setSourceLabel(e.target.value)}
              placeholder="e.g. Cummins"
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="rc-desc">Description <span className="text-muted-foreground font-normal">(optional)</span></Label>
            <Textarea
              id="rc-desc"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="What this ratecard covers"
              rows={3}
            />
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={saving}>Cancel</Button>
          <Button variant="accent" onClick={submit} disabled={saving || !name.trim()}>
            {saving && <Loader2 size={16} className="animate-spin" />}
            Create
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ─────────────────────────────────────────────────────────────────────────────

function RatecardDetailView({ ratecardId, onBack }: { ratecardId: number; onBack: () => void }) {
  const [detail, setDetail] = useState<RatecardDetail | null>(null);
  const [items, setItems] = useState<RatecardItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [engineFilter, setEngineFilter] = useState<string>('');
  const [checkFilter, setCheckFilter] = useState<string>('');
  const [deleteConfirm, setDeleteConfirm] = useState(false);

  const loadDetail = useCallback(async () => {
    setLoading(true);
    try {
      setDetail(await getRatecard(ratecardId));
    } catch {
      toast.error('Failed to load ratecard');
    } finally {
      setLoading(false);
    }
  }, [ratecardId]);

  const loadItems = useCallback(async () => {
    try {
      setItems(await listRatecardItems(ratecardId, {
        engine_type: engineFilter || undefined,
        check_level: checkFilter || undefined,
        limit: 1000,
      }));
    } catch {
      toast.error('Failed to load items');
    }
  }, [ratecardId, engineFilter, checkFilter]);

  useEffect(() => { loadDetail(); }, [loadDetail]);
  useEffect(() => { loadItems(); }, [loadItems]);

  const engines = Array.from(
    new Set((detail?.check_schedules ?? []).map((s) => s.engine_type).filter(Boolean) as string[])
  );

  const handleDelete = async () => {
    try {
      await deleteRatecard(ratecardId);
      toast.success('Ratecard deleted');
      onBack();
    } catch {
      toast.error('Failed to delete ratecard');
    }
  };

  return (
    <div className="flex flex-col h-full">
      <Header
        title={detail?.name ?? 'Ratecard'}
        subtitle={detail?.source_label ?? undefined}
      />

      <div className="flex-1 overflow-y-auto p-4 sm:p-6 lg:p-8">
        <div className="max-w-5xl mx-auto">
          <div className="flex items-center justify-between mb-6">
            <Button variant="ghost" size="sm" onClick={onBack}>
              <ChevronLeft size={16} />
              Back to ratecards
            </Button>
            <Button variant="ghost" size="sm" onClick={() => setDeleteConfirm(true)} className="text-destructive hover:text-destructive">
              <Trash2 size={15} />
              Delete
            </Button>
          </div>

          {loading ? (
            <div className="space-y-3">
              <Skeleton className="h-24 rounded-xl" />
              <Skeleton className="h-64 rounded-xl" />
            </div>
          ) : detail ? (
            <>
              {/* Upload + summary */}
              <UploadCard ratecardId={ratecardId} onUploaded={() => { loadDetail(); loadItems(); }} detail={detail} />

              {/* Check schedules */}
              {detail.check_schedules.length > 0 && (
                <div className="mt-6">
                  <h3 className="text-[11px] font-semibold text-muted-foreground uppercase tracking-widest mb-3">
                    Check Schedules (kits)
                  </h3>
                  <div className="flex flex-wrap gap-2">
                    {detail.check_schedules.map((s) => (
                      <span
                        key={s.id}
                        className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium bg-accent/10 text-accent border border-accent/20"
                      >
                        <Layers size={12} />
                        {s.engine_type} · {s.check_level}-check
                      </span>
                    ))}
                  </div>
                </div>
              )}

              {/* Items table */}
              <div className="mt-6">
                <div className="flex flex-wrap items-center justify-between gap-3 mb-3">
                  <h3 className="text-[11px] font-semibold text-muted-foreground uppercase tracking-widest">
                    Parts ({items.length})
                  </h3>
                  <div className="flex items-center gap-2">
                    <select
                      value={engineFilter}
                      onChange={(e) => setEngineFilter(e.target.value)}
                      className="h-9 rounded-md border border-input bg-background px-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                    >
                      <option value="">All engines</option>
                      {engines.map((e) => <option key={e} value={e}>{e}</option>)}
                    </select>
                    <select
                      value={checkFilter}
                      onChange={(e) => setCheckFilter(e.target.value)}
                      className="h-9 rounded-md border border-input bg-background px-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                    >
                      <option value="">All checks</option>
                      <option value="B">B-check</option>
                      <option value="C">C-check</option>
                      <option value="D">D-check</option>
                    </select>
                  </div>
                </div>

                {items.length === 0 ? (
                  <div className="text-center py-12 border-2 border-dashed border-border rounded-xl text-sm text-muted-foreground">
                    No parts. Upload an Excel ratecard above to populate this.
                  </div>
                ) : (
                  <div className="overflow-x-auto rounded-xl border border-border">
                    <table className="w-full text-sm">
                      <thead className="bg-muted/40 border-b border-border">
                        <tr className="text-left text-muted-foreground">
                          <th className="px-3 py-2 font-medium">Part No.</th>
                          <th className="px-3 py-2 font-medium">Description</th>
                          <th className="px-3 py-2 font-medium">Engine</th>
                          <th className="px-3 py-2 font-medium text-right">Qty</th>
                          <th className="px-3 py-2 font-medium text-right">Rate (₹)</th>
                          <th className="px-3 py-2 font-medium">Source</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-border">
                        {items.map((it, i) => (
                          <tr key={i} className="hover:bg-muted/40">
                            <td className="px-3 py-2 font-medium text-foreground whitespace-nowrap">{it.part_no || '—'}</td>
                            <td className="px-3 py-2 text-muted-foreground max-w-xs truncate">{it.description || '—'}</td>
                            <td className="px-3 py-2 text-muted-foreground whitespace-nowrap">{it.engine_type || '—'}</td>
                            <td className="px-3 py-2 text-right tabular-nums">{it.qty ?? '—'}</td>
                            <td className="px-3 py-2 text-right tabular-nums">
                              {it.rate != null ? it.rate.toLocaleString('en-IN', { minimumFractionDigits: 2 }) : '—'}
                            </td>
                            <td className="px-3 py-2 text-muted-foreground whitespace-nowrap">{it.source || '—'}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            </>
          ) : null}
        </div>
      </div>

      <Dialog open={deleteConfirm} onOpenChange={(o) => !o && setDeleteConfirm(false)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete ratecard</DialogTitle>
          </DialogHeader>
          <p className="text-sm text-muted-foreground">
            Delete &quot;{detail?.name}&quot; and all its parts + check schedules? This cannot be undone.
          </p>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteConfirm(false)}>Cancel</Button>
            <Button variant="destructive" onClick={handleDelete}>Delete</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────

function UploadCard({
  ratecardId, onUploaded, detail,
}: {
  ratecardId: number;
  onUploaded: () => void;
  detail: RatecardDetail;
}) {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [preview, setPreview] = useState<RatecardPreview | null>(null);
  const [pendingFile, setPendingFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);

  const onPick = async (file: File) => {
    setPendingFile(file);
    setPreview(null);
    setBusy(true);
    try {
      setPreview(await previewRatecardFile(file));
    } catch (e: any) {
      toast.error(e?.response?.data?.detail || 'Could not preview file');
      setPendingFile(null);
    } finally {
      setBusy(false);
    }
  };

  const confirmUpload = async () => {
    if (!pendingFile) return;
    setBusy(true);
    try {
      const res = await uploadRatecardFile(ratecardId, pendingFile);
      toast.success(`Ingested ${res.items} parts · ${res.check_schedules} check schedules`);
      setPendingFile(null);
      setPreview(null);
      onUploaded();
    } catch (e: any) {
      toast.error(e?.response?.data?.detail || 'Upload failed');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="bg-card rounded-xl border border-border p-5 shadow-card">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <p className="text-sm font-medium text-foreground">
            {detail.item_count} parts · {detail.check_schedules.length} check schedules
          </p>
          {detail.original_file_name && (
            <p className="text-xs text-muted-foreground mt-0.5">
              Last upload: {detail.original_file_name}
            </p>
          )}
        </div>
        <Button
          variant="outline"
          onClick={() => fileInputRef.current?.click()}
          disabled={busy}
        >
          <Upload size={16} />
          Upload Excel ratecard
        </Button>
        <input
          ref={fileInputRef}
          type="file"
          accept=".xlsx,.xlsm"
          className="hidden"
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) onPick(f);
            e.target.value = '';
          }}
        />
      </div>

      {busy && !preview && (
        <div className="mt-4 flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 size={15} className="animate-spin" />
          Parsing file…
        </div>
      )}

      {/* Preview before commit */}
      {preview && pendingFile && (
        <div className="mt-4 rounded-lg border border-accent/20 bg-accent/5 p-4">
          <div className="flex items-start gap-2">
            <AlertCircle size={16} className="text-accent mt-0.5 shrink-0" />
            <div className="flex-1 min-w-0">
              <p className="text-sm font-medium text-foreground">
                Preview: {preview.item_count} parts, {preview.check_schedule_count} check schedules
              </p>
              <p className="text-xs text-muted-foreground mt-0.5 truncate">
                From {pendingFile.name}
              </p>
              {preview.check_schedules.length > 0 && (
                <div className="flex flex-wrap gap-1.5 mt-2">
                  {preview.check_schedules.slice(0, 8).map((s, i) => (
                    <span key={i} className="text-[11px] px-2 py-0.5 rounded-full bg-accent/10 text-accent border border-accent/20">
                      {s.engine_type} · {s.check_level}
                    </span>
                  ))}
                </div>
              )}
              {preview.skipped.length > 0 && (
                <p className="text-xs text-warning mt-2">{preview.skipped.length} row(s) skipped</p>
              )}
            </div>
          </div>
          <div className="flex items-center justify-end gap-2 mt-3">
            <Button variant="ghost" size="sm" onClick={() => { setPreview(null); setPendingFile(null); }} disabled={busy}>
              Cancel
            </Button>
            <Button variant="accent" size="sm" onClick={confirmUpload} disabled={busy}>
              {busy ? <Loader2 size={15} className="animate-spin" /> : <CheckCircle2 size={15} />}
              Confirm import
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
