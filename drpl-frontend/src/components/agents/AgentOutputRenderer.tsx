/**
 * AgentOutputRenderer - Renders agent output based on output_type.
 *
 * Output types:
 * - document_analysis: Collapsible sections for requirements, risks, documents
 * - checklist: Interactive table of submission items
 * - proposal_document: Styled document content with download action
 * - cost_breakdown: Financial tables with totals
 * - general: Standard markdown rendering
 */

import { useState } from 'react';
import {
  ChevronDown, ChevronRight, AlertTriangle, FileText, CheckCircle2,
  XCircle, Info, DollarSign, Download, Shield, ClipboardList,
  FileCheck, BookOpen, Scale, Wrench, Users, Landmark,
} from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

interface AgentOutputRendererProps {
  content: string;
  outputType: string;
  onAction?: (action: string, data?: any) => void;
  structuredData?: any;
}

// --- Category Icons ---
const CATEGORY_ICONS: Record<string, any> = {
  requirements: ClipboardList,
  eligibility: Users,
  terms_conditions: Scale,
  technical_specs: Wrench,
  financial: DollarSign,
  experience: BookOpen,
  compliance: Shield,
};

const CATEGORY_COLORS: Record<string, string> = {
  requirements: 'border-accent/20 bg-accent/10',
  eligibility: 'border-emerald-200 dark:border-emerald-500/20 bg-emerald-50 dark:bg-emerald-500/15',
  terms_conditions: 'border-purple-200 dark:border-purple-500/20 bg-purple-50 dark:bg-purple-500/15',
  technical_specs: 'border-orange-200 dark:border-orange-500/20 bg-orange-50 dark:bg-orange-500/15',
  financial: 'border-yellow-200 dark:border-yellow-500/20 bg-yellow-50 dark:bg-yellow-500/15',
  experience: 'border-cyan-200 dark:border-cyan-500/20 bg-cyan-50 dark:bg-cyan-500/15',
  compliance: 'border-red-200 dark:border-red-500/20 bg-red-50 dark:bg-red-500/15',
};

const CATEGORY_LABELS: Record<string, string> = {
  requirements: 'General Requirements',
  eligibility: 'Eligibility Criteria',
  terms_conditions: 'Terms & Conditions',
  technical_specs: 'Technical Specifications',
  financial: 'Financial Requirements',
  experience: 'Experience Requirements',
  compliance: 'Compliance Requirements',
};

// --- Collapsible Section ---
function CollapsibleSection({
  title,
  icon: Icon,
  count,
  colorClass,
  defaultOpen = false,
  children,
}: {
  title: string;
  icon?: any;
  count?: number;
  colorClass?: string;
  defaultOpen?: boolean;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className={`border rounded-lg overflow-hidden ${colorClass || 'border-border bg-card'}`}>
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center gap-2 px-4 py-3 text-left hover:bg-card/50 transition-colors"
      >
        {open ? <ChevronDown size={16} className="text-muted-foreground shrink-0" /> : <ChevronRight size={16} className="text-muted-foreground shrink-0" />}
        {Icon && <Icon size={16} className="text-muted-foreground shrink-0" />}
        <span className="text-sm font-medium text-foreground flex-1">{title}</span>
        {count !== undefined && (
          <span className="text-xs bg-card/80 text-muted-foreground px-2 py-0.5 rounded-full font-medium">{count} items</span>
        )}
      </button>
      {open && <div className="px-4 pb-3 space-y-1.5">{children}</div>}
    </div>
  );
}

// --- Severity Badge ---
function SeverityBadge({ severity }: { severity: string }) {
  const colors: Record<string, string> = {
    critical: 'bg-red-100 dark:bg-red-500/20 text-red-700 dark:text-red-400 border-red-200 dark:border-red-500/20',
    high: 'bg-orange-100 dark:bg-orange-500/20 text-orange-700 dark:text-orange-400 border-orange-200 dark:border-orange-500/20',
    medium: 'bg-yellow-100 dark:bg-yellow-500/20 text-yellow-700 dark:text-yellow-400 border-yellow-200 dark:border-yellow-500/20',
    low: 'bg-muted text-muted-foreground border-border',
  };
  return (
    <span className={`inline-block px-2 py-0.5 rounded text-[10px] font-semibold uppercase border ${colors[severity] || colors.medium}`}>
      {severity}
    </span>
  );
}

// --- Preamble stripper: removes filler opening lines agents sometimes emit ---
const PREAMBLE_PATTERNS: RegExp[] = [
  /^\s*(?:I['\u2019]ll|I will|Let me|Now let me|Now I will|I am going to|I'm going to|I shall)\s+(?:conduct|perform|analyze|analyse|research|compile|draft|prepare|create|generate|produce|gather|review|examine|look|start|begin|now)\b[^\n]*\n+/i,
  /^\s*(?:Sure|Certainly|Of course|Absolutely|Alright|Okay|Ok|Great)[,.!]?\s*(?:here(?:['\u2019]s| is)|I['\u2019]ll|let me)\b[^\n]*\n+/i,
  /^\s*(?:Based on|Looking at|After reviewing)\s+(?:your|the)\s+(?:request|query|message|tender|document)[^\n]*\n+/i,
  /^\s*(?:Here(?:['\u2019]s| is)\s+(?:the|a|my)|Below is)\s+[^\n]*?(?:analysis|report|breakdown|checklist|proposal|summary)[^\n]*\n+/i,
];

function stripPreambles(text: string): string {
  if (!text) return text;
  let out = text.replace(/^\s+/, '');
  for (let i = 0; i < 3; i++) {
    let changed = false;
    for (const rx of PREAMBLE_PATTERNS) {
      const next = out.replace(rx, '');
      if (next !== out) { out = next.replace(/^\s+/, ''); changed = true; }
    }
    if (!changed) break;
  }
  return out;
}

// Strip `(source: doc_id N)` / `(doc_id N, p.M)` style markers from the V2
// synthesis markdown — useful for the data pipeline but visually noisy in the
// artifact panel when the renderer falls back to MarkdownContent.
function stripSourceCitations(text: string): string {
  if (!text) return text;
  return text
    .replace(/\s*\(\s*source\s*:\s*doc_id\s*\d+(?:\s*,\s*p\.?\s*\d+)?\s*\)/gi, '')
    .replace(/\s*\(\s*doc_id\s*\d+(?:\s*,\s*p\.?\s*\d+)?\s*\)/gi, '');
}

// --- Markdown Content (simplified) ---
export function MarkdownContent({ content }: { content: string }) {
  const cleaned = stripPreambles(content);
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={{
        table({ children }) {
          return <div className="overflow-x-auto my-3"><table className="min-w-full border border-border text-sm">{children}</table></div>;
        },
        th({ children }) { return <th className="border border-border bg-muted/40 px-3 py-2 text-left font-medium">{children}</th>; },
        td({ children }) { return <td className="border border-border px-3 py-2">{children}</td>; },
        ul({ children }) { return <ul className="list-disc pl-6 my-2 space-y-1">{children}</ul>; },
        ol({ children }) { return <ol className="list-decimal pl-6 my-2 space-y-1">{children}</ol>; },
        h1({ children }) { return <h1 className="text-xl font-bold mt-4 mb-2">{children}</h1>; },
        h2({ children }) { return <h2 className="text-lg font-bold mt-3 mb-2">{children}</h2>; },
        h3({ children }) { return <h3 className="text-base font-semibold mt-3 mb-1">{children}</h3>; },
        blockquote({ children }) { return <blockquote className="border-l-4 border-border pl-4 my-2 text-muted-foreground italic">{children}</blockquote>; },
        p({ children }) { return <p className="my-1.5 leading-relaxed">{children}</p>; },
        strong({ children }) { return <strong className="font-semibold">{children}</strong>; },
        code({ children, className }) {
          const inline = !className;
          if (inline) return <code className="bg-muted text-foreground px-1.5 py-0.5 rounded text-[13px] font-mono">{children}</code>;
          return <pre className="bg-foreground/90 dark:bg-muted text-background dark:text-foreground rounded-lg p-4 overflow-x-auto my-3 text-sm"><code>{children}</code></pre>;
        },
      }}
    >
      {cleaned}
    </ReactMarkdown>
  );
}

// --- Try to parse structured data from markdown ---
function tryParseStructuredData(content: string): any {
  try {
    // Try extracting JSON blocks from the content
    if (content.includes('```json')) {
      const jsonStr = content.split('```json')[1]?.split('```')[0]?.trim();
      if (jsonStr) return JSON.parse(jsonStr);
    }
    // Try direct JSON parse
    return JSON.parse(content);
  } catch {
    return null;
  }
}

// Reflow inline numbered enumerations (e.g. a verbatim Scope-of-Work that the
// tender prints as one run-on paragraph "1. … 2. … 3. …") onto separate lines
// so each numbered clause renders one-after-another. Wording is preserved
// exactly — we only insert markdown hard line breaks ("  \n") between items,
// keeping the literal numbers as text (no list renumbering) and any blockquote
// prefix intact. Guarded to fire ONLY on a strong enumeration signal (≥3
// markers, starting at 1, strictly increasing by 1) so decimals (1.5sqmm),
// dates (10.03.2015), codes (No.EL/7.1.57) and lone "clause 7." references are
// never split. Code fences and table rows are left untouched.
export function reflowNumberedRuns(md: string): string {
  if (!md) return md;
  const lines = md.split('\n');
  const out: string[] = [];
  let inFence = false;
  for (const line of lines) {
    if (/^\s*```/.test(line)) { inFence = !inFence; out.push(line); continue; }
    if (inFence || line.trimStart().startsWith('|')) { out.push(line); continue; }

    const bq = line.match(/^(\s*>\s?)/);
    const prefix = bq ? bq[1] : '';
    const body = bq ? line.slice(prefix.length) : line;

    const markerRe = /(^|[\s"'“”(])(\d{1,2})\.\s+(?=\S)/g;
    const marks: { idx: number; n: number }[] = [];
    let m: RegExpExecArray | null;
    while ((m = markerRe.exec(body)) !== null) {
      marks.push({ idx: m.index + m[1].length, n: Number(m[2]) });
    }
    const isEnum = marks.length >= 3 && marks[0].n === 1 &&
      marks.slice(1).every((x, i) => x.n === marks[i].n + 1);
    if (!isEnum) { out.push(line); continue; }

    const pieces: string[] = [];
    for (let i = 0; i < marks.length; i++) {
      const start = marks[i].idx;
      const end = i + 1 < marks.length ? marks[i + 1].idx : body.length;
      pieces.push(body.slice(start, end).trim());
    }
    const pre = body.slice(0, marks[0].idx).trim();
    const sep = pre && !/[\s"'“(]$/.test(pre) ? ' ' : '';
    const items = [pre + sep + pieces[0], ...pieces.slice(1)];
    for (let i = 0; i < items.length; i++) {
      const isLast = i === items.length - 1;
      out.push(prefix + items[i] + (isLast ? '' : '  '));
    }
  }
  return out.join('\n');
}

// --- Document Analysis Renderer ---
// Tender analysis is markdown-only: the deep_analyzer pipeline produces the
// full 10-section forensic markdown report (with embedded tables for Section
// 1 commercials, Section 3 eligibility GO/NO-GO, Section 5 negative
// keywords, Section 6 required documents, Section 7 costing handoff). Just
// render it. A trailing JSON block from an old prompt-cache hit is stripped
// defensively before rendering.
function DocumentAnalysisRenderer({
  content,
}: {
  content: string;
  structuredData?: any;
  onAction?: AgentOutputRendererProps['onAction'];
}) {
  const cleaned = stripAnalysisJsonTail(content || '');
  return <MarkdownContent content={reflowNumberedRuns(stripSourceCitations(cleaned))} />;
}

// Strip a trailing JSON block from a tender-analysis report. Defends against
// legacy artifacts in the DB and prompt-cache hits from the old synthesis
// prompt that still emit a fenced JSON tail. Anchored to end-of-string so
// inline JSON examples in the report body survive untouched.
function stripAnalysisJsonTail(content: string): string {
  if (!content) return content;
  let cleaned = content.replace(/\n*\s*```(?:json|JSON)?\s*\{[\s\S]*?\}\s*```\s*$/m, '');
  if (cleaned === content) {
    cleaned = content.replace(/\n*\s*\{[\s\S]*\}\s*$/m, '');
  }
  return cleaned.trimEnd();
}

// --- Checklist Renderer ---
function stripChecklistMarkers(text: string): string {
  // Remove MACHINE-READABLE header + CHECKLIST_JSON_START...END blocks that may leak from backend
  return text
    .replace(/\n*\s*(?:[-#*\s]*MACHINE[-_ ]?READABLE[^\n]*\n)?\s*CHECKLIST_JSON_START\s*\n?[\s\S]*?\n?\s*CHECKLIST_JSON_END\s*\n*/gi, '')
    .replace(/CHECKLIST_JSON_START[\s\S]*?CHECKLIST_JSON_END/gi, '')
    .replace(/\n*\s*[-#*\s]*MACHINE[-_ ]?READABLE\s+CHECKLIST\s+DATA[^\n]*/gi, '')
    .trim();
}

function ChecklistRenderer({ content, onAction }: { content: string; onAction?: AgentOutputRendererProps['onAction'] }) {
  const cleanContent = stripChecklistMarkers(content);
  return <MarkdownContent content={cleanContent} />;
}

// --- Proposal Document Renderer ---
function ProposalDocumentRenderer({ content, onAction }: { content: string; onAction?: AgentOutputRendererProps['onAction'] }) {
  return (
    <div>
      <div className="bg-card border border-border rounded-lg p-6 shadow-sm">
        <div className="prose prose-sm max-w-none">
          <MarkdownContent content={content} />
        </div>
      </div>
      {onAction && (
        <div className="flex items-center gap-2 mt-3">
          <button
            onClick={() => onAction('download_pdf')}
            className="flex items-center gap-1.5 text-xs font-medium text-accent hover:text-accent/80 bg-accent/10 hover:bg-accent/15 px-3 py-1.5 rounded-lg transition-colors"
          >
            <Download size={13} />
            Download as PDF
          </button>
        </div>
      )}
    </div>
  );
}

// --- Cost Breakdown Renderer ---
function escapeCsv(v: any): string {
  if (v === null || v === undefined) return '';
  const s = String(v);
  if (/[",\n\r]/.test(s)) return `"${s.replace(/"/g, '""')}"`;
  return s;
}

function buildCostBreakdownCsv(data: any): string {
  const rows: string[] = [];
  rows.push('Cost Breakdown');
  rows.push('');

  const lineItems = Array.isArray(data?.line_items) ? data.line_items : [];
  if (lineItems.length) {
    rows.push(['#', 'Description', 'Category', 'Quantity', 'Unit', 'Rate', 'Amount'].map(escapeCsv).join(','));
    lineItems.forEach((item: any, i: number) => {
      rows.push([
        i + 1,
        item.description ?? '',
        item.category ?? '',
        item.quantity ?? '',
        item.unit ?? '',
        item.rate ?? '',
        item.amount ?? '',
      ].map(escapeCsv).join(','));
    });
    rows.push('');
  }

  const summary = ([
    ['Subtotal', data?.subtotal],
    ['Overheads', data?.overheads],
    ['Profit Margin', data?.profit_margin],
    ['GST', data?.gst],
    ['Grand Total', data?.grand_total],
  ] as [string, any][]).filter(([, v]) => v !== undefined && v !== null && v !== '');
  if (summary.length) {
    rows.push(['Component', 'Amount'].map(escapeCsv).join(','));
    summary.forEach(([label, value]) => rows.push([label, value].map(escapeCsv).join(',')));
    rows.push('');
  }

  const assumptions = Array.isArray(data?.assumptions) ? data.assumptions : [];
  if (assumptions.length) {
    rows.push('Assumptions');
    assumptions.forEach((a: any) => rows.push(escapeCsv(a)));
    rows.push('');
  }

  const recommendations = Array.isArray(data?.recommendations) ? data.recommendations : [];
  if (recommendations.length) {
    rows.push('Recommendations');
    recommendations.forEach((r: any) => rows.push(escapeCsv(r)));
  }

  return rows.join('\r\n');
}

function hasExportableCostData(data: any): boolean {
  if (!data || typeof data !== 'object') return false;
  if (Array.isArray(data.line_items) && data.line_items.length > 0) return true;
  return ['subtotal', 'overheads', 'profit_margin', 'gst', 'grand_total'].some(
    (k) => data[k] !== undefined && data[k] !== null && data[k] !== ''
  );
}

function CostBreakdownRenderer({ content, structuredData }: { content: string; structuredData?: any }) {
  const canExport = hasExportableCostData(structuredData);

  const handleDownload = () => {
    const csv = buildCostBreakdownCsv(structuredData);
    // UTF-8 BOM so Excel opens as UTF-8
    const blob = new Blob(['\ufeff' + csv], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'cost_breakdown.csv';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  return (
    <div>
      {canExport && (
        <div className="flex justify-end mb-3">
          <button
            onClick={handleDownload}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium border border-border rounded-md bg-card hover:bg-muted/40 text-foreground"
          >
            <Download size={14} />
            Download as Excel (CSV)
          </button>
        </div>
      )}
      <MarkdownContent content={content} />
    </div>
  );
}

// --- Main Renderer ---
export default function AgentOutputRenderer({ content, outputType, onAction, structuredData }: AgentOutputRendererProps) {
  switch (outputType) {
    case 'document_analysis':
      return <DocumentAnalysisRenderer content={content} structuredData={structuredData} onAction={onAction} />;
    case 'checklist':
      return <ChecklistRenderer content={content} onAction={onAction} />;
    case 'proposal_document':
      return <ProposalDocumentRenderer content={content} onAction={onAction} />;
    case 'cost_breakdown':
      return <CostBreakdownRenderer content={content} structuredData={structuredData} />;
    case 'general':
    default:
      return <MarkdownContent content={content} />;
  }
}
