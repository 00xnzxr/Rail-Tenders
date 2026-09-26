import { useState, useEffect } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { TestTube2, Plus, Play, Trash2, Check, X, RotateCw } from 'lucide-react';
import Header from '../../components/layout/Header';
import LoadingSpinner from '../../components/ui/LoadingSpinner';
import {
  getBuilderAgent,
  getAgentTests,
  createAgentTest,
  updateAgentTest,
  deleteAgentTest,
  runAgentTest,
  runAllAgentTests,
} from '../../lib/api';

interface TestCase {
  id: number;
  test_name: string;
  input_data: any;
  expected_output: string;
  evaluation_criteria: {
    must_contain?: string[];
    must_not_contain?: string[];
    score_threshold?: number;
  };
}

interface TestResult {
  passed: boolean;
  score: number;
  output: string;
  latency_ms: number;
}

export default function AgentTestingPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const agentId = Number(id);

  const [agentName, setAgentName] = useState('');
  const [tests, setTests] = useState<TestCase[]>([]);
  const [loading, setLoading] = useState(true);
  const [showForm, setShowForm] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [results, setResults] = useState<Record<number, TestResult>>({});
  const [runningAll, setRunningAll] = useState(false);
  const [runningIds, setRunningIds] = useState<Set<number>>(new Set());

  // Form state
  const [formData, setFormData] = useState({
    test_name: '',
    input_data: '{}',
    expected_output: '',
    must_contain: '',
    must_not_contain: '',
    score_threshold: 0.8,
  });

  const resetForm = () => {
    setFormData({ test_name: '', input_data: '{}', expected_output: '', must_contain: '', must_not_contain: '', score_threshold: 0.8 });
    setEditingId(null);
    setShowForm(false);
  };

  const load = async () => {
    setLoading(true);
    try {
      const [agent, testList] = await Promise.all([
        getBuilderAgent(agentId).catch(() => null),
        getAgentTests(agentId).catch(() => []),
      ]);
      setAgentName(agent?.display_name || 'Agent');
      setTests(testList);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, [agentId]);

  const handleSaveTest = async () => {
    const body: Record<string, any> = {
      test_name: formData.test_name,
      expected_output: formData.expected_output,
      evaluation_criteria: {
        must_contain: formData.must_contain.split(',').map((s) => s.trim()).filter(Boolean),
        must_not_contain: formData.must_not_contain.split(',').map((s) => s.trim()).filter(Boolean),
        score_threshold: formData.score_threshold,
      },
    };
    try { body.input_data = JSON.parse(formData.input_data); } catch { body.input_data = { input: formData.input_data }; }

    try {
      if (editingId) {
        await updateAgentTest(editingId, body);
      } else {
        await createAgentTest(agentId, body);
      }
      resetForm();
      load();
    } catch { /* ignore */ }
  };

  const handleEdit = (test: TestCase) => {
    setFormData({
      test_name: test.test_name,
      input_data: JSON.stringify(test.input_data, null, 2),
      expected_output: test.expected_output || '',
      must_contain: (test.evaluation_criteria?.must_contain || []).join(', '),
      must_not_contain: (test.evaluation_criteria?.must_not_contain || []).join(', '),
      score_threshold: test.evaluation_criteria?.score_threshold ?? 0.8,
    });
    setEditingId(test.id);
    setShowForm(true);
  };

  const handleDelete = async (testId: number) => {
    if (!confirm('Delete this test case?')) return;
    try {
      await deleteAgentTest(testId);
      load();
    } catch { /* ignore */ }
  };

  const handleRunSingle = async (testId: number) => {
    setRunningIds((prev) => new Set(prev).add(testId));
    try {
      const result = await runAgentTest(testId);
      setResults((prev) => ({ ...prev, [testId]: result }));
    } catch {
      setResults((prev) => ({ ...prev, [testId]: { passed: false, score: 0, output: 'Execution failed', latency_ms: 0 } }));
    } finally {
      setRunningIds((prev) => { const n = new Set(prev); n.delete(testId); return n; });
    }
  };

  const handleRunAll = async () => {
    setRunningAll(true);
    try {
      const batchResults = await runAllAgentTests(agentId);
      const mapped: Record<number, TestResult> = {};
      (batchResults || []).forEach((r: any) => {
        if (r.test_id) mapped[r.test_id] = r;
      });
      setResults((prev) => ({ ...prev, ...mapped }));
    } catch { /* ignore */ }
    finally { setRunningAll(false); }
  };

  if (loading) return <><Header title="Agent Testing" /><LoadingSpinner /></>;

  return (
    <>
      <Header title={`Testing: ${agentName}`} />
      <div className="mx-auto w-full max-w-[1600px] space-y-6 p-4 sm:p-6 lg:p-8">

        {/* Top actions */}
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <button
              onClick={() => navigate(`/admin/agent-builder/${id}`)}
              className="text-sm text-muted-foreground hover:text-drpl-primary"
            >
              Back to Editor
            </button>
          </div>
          <div className="flex items-center gap-3">
            <button
              onClick={handleRunAll}
              disabled={runningAll || tests.length === 0}
              className="px-4 py-2 text-sm font-medium border border-border rounded-lg hover:bg-muted/40 flex items-center gap-2 disabled:opacity-50"
            >
              <RotateCw size={14} className={runningAll ? 'animate-spin' : ''} /> Run All Tests
            </button>
            <button
              onClick={() => { resetForm(); setShowForm(true); }}
              className="bg-accent text-accent-foreground px-4 py-2 rounded-lg text-sm font-medium hover:bg-accent/90 flex items-center gap-2"
            >
              <Plus size={14} /> Add Test Case
            </button>
          </div>
        </div>

        {/* Add/Edit form */}
        {showForm && (
          <div className="bg-card rounded-xl shadow-card border border-border p-5 space-y-4">
            <h3 className="text-sm font-semibold text-foreground">{editingId ? 'Edit Test Case' : 'New Test Case'}</h3>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <div>
                <label className="block text-xs font-medium text-muted-foreground mb-1">Test Name</label>
                <input
                  value={formData.test_name}
                  onChange={(e) => setFormData((f) => ({ ...f, test_name: e.target.value }))}
                  className="w-full px-3 py-2 text-sm border border-border rounded-lg focus:outline-none focus:ring-2 focus:ring-drpl-primary/20"
                  placeholder="e.g. Basic greeting test"
                />
              </div>
              <div>
                <label className="block text-xs font-medium text-muted-foreground mb-1">Score Threshold</label>
                <input
                  type="number"
                  min={0}
                  max={1}
                  step={0.05}
                  value={formData.score_threshold}
                  onChange={(e) => setFormData((f) => ({ ...f, score_threshold: parseFloat(e.target.value) || 0 }))}
                  className="w-full px-3 py-2 text-sm border border-border rounded-lg focus:outline-none focus:ring-2 focus:ring-drpl-primary/20"
                />
              </div>
            </div>
            <div>
              <label className="block text-xs font-medium text-muted-foreground mb-1">Input Data (JSON)</label>
              <textarea
                value={formData.input_data}
                onChange={(e) => setFormData((f) => ({ ...f, input_data: e.target.value }))}
                rows={3}
                className="w-full px-3 py-2 text-sm border border-border rounded-lg font-mono focus:outline-none focus:ring-2 focus:ring-drpl-primary/20"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-muted-foreground mb-1">Expected Output</label>
              <textarea
                value={formData.expected_output}
                onChange={(e) => setFormData((f) => ({ ...f, expected_output: e.target.value }))}
                rows={3}
                className="w-full px-3 py-2 text-sm border border-border rounded-lg focus:outline-none focus:ring-2 focus:ring-drpl-primary/20"
              />
            </div>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <div>
                <label className="block text-xs font-medium text-muted-foreground mb-1">Must Contain (comma-separated)</label>
                <input
                  value={formData.must_contain}
                  onChange={(e) => setFormData((f) => ({ ...f, must_contain: e.target.value }))}
                  className="w-full px-3 py-2 text-sm border border-border rounded-lg focus:outline-none focus:ring-2 focus:ring-drpl-primary/20"
                  placeholder="hello, world"
                />
              </div>
              <div>
                <label className="block text-xs font-medium text-muted-foreground mb-1">Must Not Contain (comma-separated)</label>
                <input
                  value={formData.must_not_contain}
                  onChange={(e) => setFormData((f) => ({ ...f, must_not_contain: e.target.value }))}
                  className="w-full px-3 py-2 text-sm border border-border rounded-lg focus:outline-none focus:ring-2 focus:ring-drpl-primary/20"
                  placeholder="error, fail"
                />
              </div>
            </div>
            <div className="flex items-center gap-3">
              <button onClick={handleSaveTest} className="bg-accent text-accent-foreground px-4 py-2 rounded-lg text-sm font-medium hover:bg-accent/90">
                {editingId ? 'Update' : 'Create'} Test
              </button>
              <button onClick={resetForm} className="px-4 py-2 text-sm text-muted-foreground hover:text-foreground">Cancel</button>
            </div>
          </div>
        )}

        {/* Test cases list */}
        {tests.length === 0 && !showForm ? (
          <div className="text-center py-20 text-muted-foreground">
            <TestTube2 size={48} className="mx-auto mb-3 opacity-40" />
            <p className="text-lg font-medium">No test cases yet</p>
            <p className="text-sm mt-1">Add test cases to validate your agent.</p>
          </div>
        ) : (
          <div className="space-y-4">
            {tests.map((test) => {
              const result = results[test.id];
              const isRunning = runningIds.has(test.id);
              return (
                <div key={test.id} className="bg-card rounded-xl shadow-card border border-border p-5">
                  <div className="flex items-start justify-between mb-3">
                    <div>
                      <h4 className="text-sm font-semibold text-foreground flex items-center gap-2">
                        {test.test_name}
                        {result && (
                          <span className={`inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full font-medium ${
                            result.passed ? 'bg-emerald-50 dark:bg-emerald-500/15 text-emerald-600 dark:text-emerald-400' : 'bg-red-50 dark:bg-red-500/15 text-red-600 dark:text-red-400'
                          }`}>
                            {result.passed ? <Check size={12} /> : <X size={12} />}
                            {result.passed ? 'Passed' : 'Failed'}
                          </span>
                        )}
                      </h4>
                    </div>
                    <div className="flex items-center gap-2">
                      <button
                        onClick={() => handleRunSingle(test.id)}
                        disabled={isRunning}
                        className="text-xs text-muted-foreground hover:text-drpl-primary flex items-center gap-1 disabled:opacity-50"
                      >
                        <Play size={12} className={isRunning ? 'animate-pulse' : ''} /> {isRunning ? 'Running...' : 'Run'}
                      </button>
                      <button onClick={() => handleEdit(test)} className="text-xs text-muted-foreground hover:text-drpl-primary">Edit</button>
                      <button onClick={() => handleDelete(test.id)} className="text-xs text-red-400 hover:text-red-600 dark:text-red-400 flex items-center gap-1">
                        <Trash2 size={12} /> Delete
                      </button>
                    </div>
                  </div>

                  <div className="grid grid-cols-1 md:grid-cols-2 gap-4 text-xs">
                    <div>
                      <p className="text-muted-foreground mb-1">Input</p>
                      <pre className="bg-muted/40 rounded p-2 text-muted-foreground font-mono overflow-auto max-h-24">
                        {typeof test.input_data === 'string' ? test.input_data : JSON.stringify(test.input_data, null, 2)}
                      </pre>
                    </div>
                    <div>
                      <p className="text-muted-foreground mb-1">Expected Output</p>
                      <pre className="bg-muted/40 rounded p-2 text-muted-foreground font-mono overflow-auto max-h-24">
                        {test.expected_output || '-'}
                      </pre>
                    </div>
                  </div>

                  {test.evaluation_criteria && (
                    <div className="flex flex-wrap gap-2 mt-3 text-[11px]">
                      {(test.evaluation_criteria.must_contain || []).map((s) => (
                        <span key={s} className="px-2 py-0.5 bg-emerald-50 dark:bg-emerald-500/15 text-emerald-600 dark:text-emerald-400 rounded-full">must: {s}</span>
                      ))}
                      {(test.evaluation_criteria.must_not_contain || []).map((s) => (
                        <span key={s} className="px-2 py-0.5 bg-red-50 dark:bg-red-500/15 text-red-500 rounded-full">not: {s}</span>
                      ))}
                      {test.evaluation_criteria.score_threshold != null && (
                        <span className="px-2 py-0.5 bg-accent/10 text-accent rounded-full">threshold: {test.evaluation_criteria.score_threshold}</span>
                      )}
                    </div>
                  )}

                  {result && (
                    <div className="mt-3 pt-3 border-t border-border">
                      <p className="text-xs text-muted-foreground mb-1">Output</p>
                      <pre className="bg-muted/40 rounded p-2 text-xs text-muted-foreground font-mono overflow-auto max-h-32">
                        {result.output}
                      </pre>
                      <div className="flex items-center gap-4 mt-2 text-[11px] text-muted-foreground">
                        <span>Score: {result.score}</span>
                        <span>Latency: {result.latency_ms}ms</span>
                      </div>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>
    </>
  );
}
