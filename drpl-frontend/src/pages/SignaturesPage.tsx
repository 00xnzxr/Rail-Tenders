import { useState, useEffect } from 'react';
import { PenTool, Plus, Trash2, Upload } from 'lucide-react';
import Header from '../components/layout/Header';
import LoadingSpinner from '../components/ui/LoadingSpinner';
import { getSignatures, createSignature, deleteSignature } from '../lib/api';

const POSITIONS = [
  { value: 'top-left', label: 'Top Left' },
  { value: 'top-center', label: 'Top Center' },
  { value: 'top-right', label: 'Top Right' },
  { value: 'middle-left', label: 'Middle Left' },
  { value: 'middle-center', label: 'Middle Center' },
  { value: 'middle-right', label: 'Middle Right' },
  { value: 'bottom-left', label: 'Bottom Left' },
  { value: 'bottom-center', label: 'Bottom Center' },
  { value: 'bottom-right', label: 'Bottom Right' },
  { value: 'custom', label: 'Custom Position' },
] as const;

function ConfirmDialog({ message, onConfirm, onCancel }: { message: string; onConfirm: () => void; onCancel: () => void }) {
  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
      <div className="bg-card rounded-xl p-6 w-96 shadow-xl">
        <p className="text-sm text-foreground mb-4">{message}</p>
        <div className="flex justify-end gap-2">
          <button onClick={onCancel} className="px-4 py-2 text-sm border border-border rounded-lg hover:bg-muted/40">Cancel</button>
          <button onClick={onConfirm} className="px-4 py-2 text-sm bg-red-600 text-white rounded-lg hover:bg-red-700">Delete</button>
        </div>
      </div>
    </div>
  );
}

export default function SignaturesPage() {
  const [signatures, setSignatures] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [showForm, setShowForm] = useState(false);
  const [deleteId, setDeleteId] = useState<number | null>(null);
  const [error, setError] = useState('');

  // Form state
  const [name, setName] = useState('');
  const [designation, setDesignation] = useState('');
  const [position, setPosition] = useState('bottom-right');
  const [positionX, setPositionX] = useState<number>(20);
  const [positionY, setPositionY] = useState<number>(30);
  const [signatureFile, setSignatureFile] = useState<File | null>(null);
  const [stampFile, setStampFile] = useState<File | null>(null);

  const fetchData = async () => {
    try {
      const data = await getSignatures();
      setSignatures(data);
    } catch {
      setError('Failed to load signatures');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { fetchData(); }, []);

  const resetForm = () => {
    setName('');
    setDesignation('');
    setPosition('bottom-right');
    setPositionX(20);
    setPositionY(30);
    setSignatureFile(null);
    setStampFile(null);
    setShowForm(false);
  };

  const handleCreate = async () => {
    if (!name.trim()) return;
    setSaving(true);
    setError('');
    try {
      const formData = new FormData();
      formData.append('name', name.trim());
      formData.append('designation', designation.trim());
      formData.append('default_position', position);
      if (position === 'custom') {
        formData.append('default_position_x', String(positionX));
        formData.append('default_position_y', String(positionY));
      }
      if (signatureFile) formData.append('signature_image', signatureFile);
      if (stampFile) formData.append('stamp_image', stampFile);
      await createSignature(formData);
      resetForm();
      await fetchData();
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to create signature');
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async () => {
    if (deleteId == null) return;
    try {
      await deleteSignature(deleteId);
      setDeleteId(null);
      await fetchData();
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to delete signature');
    }
  };

  if (loading) return <><Header title="Saved signatures" /><LoadingSpinner /></>;

  return (
    <>
      <Header title="Saved signatures" subtitle="Keep approved signatures ready for document preparation" />
      <div className="mx-auto w-full max-w-[1600px] space-y-5 p-4 sm:p-6 lg:p-8">
        {error && (
          <div className="bg-red-50 border border-red-200 rounded-lg p-3 text-sm text-red-600 dark:bg-red-500/15 dark:border-red-500/20 dark:text-red-400">{error}</div>
        )}

        {/* Actions bar */}
        <div className="flex justify-between items-center">
          <p className="text-sm text-muted-foreground">{signatures.length} signature{signatures.length !== 1 ? 's' : ''}</p>
          <button
            onClick={() => setShowForm(!showForm)}
            className="flex items-center gap-1.5 bg-accent text-accent-foreground px-4 py-2 rounded-lg text-sm font-medium hover:bg-accent/90 transition-colors"
          >
            <Plus size={14} /> Add Signature
          </button>
        </div>

        {/* Add signature form */}
        {showForm && (
          <div className="bg-card rounded-xl shadow-card border border-border p-5 space-y-4">
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-semibold text-foreground">Add New Signature</h3>
              <button onClick={resetForm} className="text-xs text-muted-foreground hover:text-muted-foreground">Cancel</button>
            </div>

            <div className="grid gap-3 sm:grid-cols-2">
              <div>
                <label className="block text-xs font-medium text-muted-foreground mb-1">Name</label>
                <input
                  type="text"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="Signatory name..."
                  className="w-full border border-border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-drpl-secondary"
                />
              </div>
              <div>
                <label className="block text-xs font-medium text-muted-foreground mb-1">Designation</label>
                <input
                  type="text"
                  value={designation}
                  onChange={(e) => setDesignation(e.target.value)}
                  placeholder="e.g. Managing Director"
                  className="w-full border border-border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-drpl-secondary"
                />
              </div>
              <div>
                <label className="block text-xs font-medium text-muted-foreground mb-1">Position</label>
                <select
                  value={position}
                  onChange={(e) => setPosition(e.target.value)}
                  className="w-full border border-border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-drpl-secondary"
                >
                  {POSITIONS.map((p) => (
                    <option key={p.value} value={p.value}>{p.label}</option>
                  ))}
                </select>
              </div>
              {position === 'custom' ? (
                <div className="flex gap-2">
                  <div className="flex-1">
                    <label className="block text-xs font-medium text-muted-foreground mb-1">X (mm from left)</label>
                    <input
                      type="number"
                      value={positionX}
                      onChange={(e) => setPositionX(Number(e.target.value))}
                      min={0}
                      max={190}
                      className="w-full border border-border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-drpl-secondary"
                    />
                  </div>
                  <div className="flex-1">
                    <label className="block text-xs font-medium text-muted-foreground mb-1">Y (mm from bottom)</label>
                    <input
                      type="number"
                      value={positionY}
                      onChange={(e) => setPositionY(Number(e.target.value))}
                      min={0}
                      max={280}
                      className="w-full border border-border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-drpl-secondary"
                    />
                  </div>
                </div>
              ) : (
                <div />
              )}
              <div>
                <label className="block text-xs font-medium text-muted-foreground mb-1">Signature Image</label>
                <label className="flex items-center gap-2 border border-dashed border-border rounded-lg px-3 py-2 text-sm text-muted-foreground hover:border-drpl-secondary hover:text-accent cursor-pointer transition-colors">
                  <Upload size={14} />
                  {signatureFile ? signatureFile.name : 'Choose file...'}
                  <input
                    type="file"
                    accept="image/*"
                    className="hidden"
                    onChange={(e) => setSignatureFile(e.target.files?.[0] || null)}
                  />
                </label>
              </div>
              <div>
                <label className="block text-xs font-medium text-muted-foreground mb-1">Stamp Image (optional)</label>
                <label className="flex items-center gap-2 border border-dashed border-border rounded-lg px-3 py-2 text-sm text-muted-foreground hover:border-drpl-secondary hover:text-accent cursor-pointer transition-colors">
                  <Upload size={14} />
                  {stampFile ? stampFile.name : 'Choose file...'}
                  <input
                    type="file"
                    accept="image/*"
                    className="hidden"
                    onChange={(e) => setStampFile(e.target.files?.[0] || null)}
                  />
                </label>
              </div>
            </div>

            <div className="flex justify-end pt-2">
              <button
                onClick={handleCreate}
                disabled={saving || !name.trim()}
                className="flex items-center gap-1.5 bg-accent text-accent-foreground px-4 py-2 rounded-lg text-sm font-medium hover:bg-accent/90 transition-colors disabled:opacity-50"
              >
                <PenTool size={14} /> {saving ? 'Saving...' : 'Create Signature'}
              </button>
            </div>
          </div>
        )}

        {/* Signatures list */}
        {signatures.length === 0 && !showForm ? (
          <div className="bg-card rounded-xl border border-border p-16 text-center">
            <PenTool size={40} className="text-muted-foreground/40 mx-auto mb-3" />
            <h3 className="text-lg font-semibold text-foreground mb-1">No Signatures</h3>
            <p className="text-sm text-muted-foreground mb-4">Add your first digital signature to use in documents.</p>
            <button
              onClick={() => setShowForm(true)}
              className="inline-flex items-center gap-1.5 bg-accent text-accent-foreground px-4 py-2 rounded-lg text-sm font-medium hover:bg-accent/90"
            >
              <Plus size={14} /> Add Signature
            </button>
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {signatures.map((sig) => (
              <div key={sig.id} className="bg-card rounded-xl shadow-card border border-border p-5 hover:border-accent/40 hover:shadow-md transition-all">
                <div className="flex items-start justify-between mb-3">
                  <div className="flex items-center gap-2.5">
                    <div className="w-10 h-10 rounded-lg bg-accent/10 flex items-center justify-center">
                      <PenTool size={20} className="text-indigo-500" />
                    </div>
                    <div>
                      <h4 className="text-sm font-semibold text-foreground">{sig.name}</h4>
                      {sig.designation && (
                        <p className="text-xs text-muted-foreground">{sig.designation}</p>
                      )}
                    </div>
                  </div>
                  <button
                    onClick={() => setDeleteId(sig.id)}
                    className="text-muted-foreground hover:text-red-600 p-1 transition-colors"
                  >
                    <Trash2 size={14} />
                  </button>
                </div>

                {/* Metadata */}
                <div className="flex flex-wrap gap-1.5 mb-3">
                  {sig.position && (
                    <span className="text-[10px] bg-muted text-muted-foreground px-2 py-0.5 rounded-full capitalize">
                      {sig.position.replace('-', ' ')}
                    </span>
                  )}
                  <span className={`text-[10px] px-2 py-0.5 rounded-full font-medium ${
                    sig.has_signature_image || sig.signature_image_path
                      ? 'bg-emerald-50 text-emerald-600 dark:bg-emerald-500/15 dark:text-emerald-400'
                      : 'bg-muted text-muted-foreground'
                  }`}>
                    Signature {sig.has_signature_image || sig.signature_image_path ? 'uploaded' : 'missing'}
                  </span>
                  <span className={`text-[10px] px-2 py-0.5 rounded-full font-medium ${
                    sig.has_stamp_image || sig.stamp_image_path
                      ? 'bg-emerald-50 text-emerald-600 dark:bg-emerald-500/15 dark:text-emerald-400'
                      : 'bg-muted text-muted-foreground'
                  }`}>
                    Stamp {sig.has_stamp_image || sig.stamp_image_path ? 'uploaded' : 'none'}
                  </span>
                </div>

                {sig.created_at && (
                  <p className="text-[10px] text-muted-foreground pt-2 border-t border-border">
                    Added {new Date(sig.created_at).toLocaleDateString()}
                  </p>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      {deleteId != null && (
        <ConfirmDialog
          message="Are you sure you want to delete this signature? This action cannot be undone."
          onConfirm={handleDelete}
          onCancel={() => setDeleteId(null)}
        />
      )}
    </>
  );
}
