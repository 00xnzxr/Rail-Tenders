import { useState, useEffect } from 'react';
import {
  FileText, ChevronDown, ChevronUp, Link2, StickyNote,
  PenTool, Sparkles, Layers,
} from 'lucide-react';
import FormatTemplateBadge from './FormatTemplateBadge';
import FormatTemplateSelector from './FormatTemplateSelector';
import DependencyGraph from './DependencyGraph';
import AgentAssignmentDropdown from './AgentAssignmentDropdown';
import LetterheadSignaturesTab from './LetterheadSignaturesTab';
import type { DocumentWorkspaceDetail, DocumentFormatTemplate, WorkspaceItem } from '../../types/workspace';

type TabKey = 'format' | 'dependencies' | 'notes' | 'letterhead-sigs';

interface Props {
  tenderId: number;
  itemId: number;
  item: DocumentWorkspaceDetail['item'];
  workspace: DocumentWorkspaceDetail['workspace'];
  formatTemplate: DocumentFormatTemplate | null;
  allItems: WorkspaceItem[];
  onTemplateChange: (templateId: number | null) => void;
  onDependenciesChange: (deps: number[]) => void;
  onNotesChange: (notes: string) => void;
  /** Used only for the legacy-clear flow in the Letterhead tab; the
   *  primary signature insertion path is now inline in the editor. */
  onClearLegacySignatures: () => void;
  onLetterheadChange: (templateId: number | null) => void;
  onOrientationChange: (orientation: 'portrait' | 'landscape') => void;
  onAgentAssign: (agentKey: string | null) => void;
}

export default function DocumentInfoBar({
  tenderId,
  itemId,
  item,
  workspace,
  formatTemplate,
  allItems,
  onTemplateChange,
  onDependenciesChange,
  onNotesChange,
  onClearLegacySignatures,
  onLetterheadChange,
  onOrientationChange,
  onAgentAssign,
}: Props) {
  const [expanded, setExpanded] = useState(false);
  const [activeTab, setActiveTab] = useState<TabKey>('format');
  const [showTemplateSelector, setShowTemplateSelector] = useState(false);
  const [localNotes, setLocalNotes] = useState(workspace.notes || '');

  // Sync notes from parent
  useEffect(() => {
    setLocalNotes(workspace.notes || '');
  }, [workspace.notes]);

  const depsCount = (workspace.depends_on?.length || 0) + (workspace.referenced_by?.length || 0);
  const legacySigCount = workspace.signatures_json?.length || 0;
  const hasLetterhead = workspace.letterhead_template_id != null;
  const letterheadBadge: number | string | undefined =
    legacySigCount > 0 ? `${legacySigCount}` : hasLetterhead ? 'L' : undefined;

  const tabs: { key: TabKey; label: string; icon: typeof Layers; badge?: number | string }[] = [
    { key: 'format', label: 'Format', icon: Layers },
    { key: 'dependencies', label: 'Deps', icon: Link2, badge: depsCount > 0 ? depsCount : undefined },
    { key: 'notes', label: 'Notes', icon: StickyNote, badge: workspace.notes ? '...' : undefined },
    // Tab key kept as 'letterhead-sigs' for state-shape compatibility, but
    // the visible label reflects its actual scope now that signatures are
    // inserted inline from the editor toolbar.
    { key: 'letterhead-sigs', label: 'Letterhead', icon: PenTool, badge: letterheadBadge },
  ];

  const handleNotesBlur = () => {
    const trimmed = localNotes.trim();
    if (trimmed !== (workspace.notes || '').trim()) {
      onNotesChange(trimmed);
    }
  };

  return (
    <div className="bg-card border rounded-xl mb-4 overflow-hidden">
      {/* Always-visible top bar */}
      <div className="flex items-center gap-4 px-4 py-3 text-sm">
        {/* Left: metadata */}
        <span className="text-muted-foreground">
          <span className="font-medium text-foreground capitalize">{item.item_category}</span> document
        </span>

        <AgentAssignmentDropdown
          tenderId={tenderId}
          currentAgentKey={workspace.agent_key}
          onSelect={onAgentAssign}
        />

        <FormatTemplateBadge
          template={formatTemplate}
          formatInstructions={workspace.format_instructions}
          size="sm"
        />

        {workspace.content_version > 0 && (
          <span className="text-muted-foreground">Version {workspace.content_version}</span>
        )}

        {item.source_section && (
          <span className="text-muted-foreground truncate max-w-xs" title={item.source_section}>
            Source: {item.source_section}
          </span>
        )}

        {/* Right: tabs + expand toggle */}
        <div className="ml-auto flex items-center gap-1">
          {tabs.map((tab) => {
            const Icon = tab.icon;
            const isActive = expanded && activeTab === tab.key;
            return (
              <button
                key={tab.key}
                onClick={() => {
                  if (expanded && activeTab === tab.key) {
                    setExpanded(false);
                  } else {
                    setActiveTab(tab.key);
                    setExpanded(true);
                  }
                }}
                className={`flex items-center gap-1 px-2.5 py-1 text-xs rounded-lg transition-colors ${
                  isActive
                    ? 'bg-indigo-100 dark:bg-indigo-500/20 text-indigo-700 dark:text-indigo-400'
                    : 'text-muted-foreground hover:text-foreground hover:bg-muted'
                }`}
              >
                <Icon size={12} />
                {tab.label}
                {tab.badge && (
                  <span className="text-[10px] bg-muted text-muted-foreground px-1 rounded-full min-w-[14px] text-center">
                    {tab.badge}
                  </span>
                )}
              </button>
            );
          })}

          <button
            onClick={() => setExpanded(!expanded)}
            className="p-1.5 hover:bg-muted rounded-lg transition-colors ml-1"
          >
            {expanded ? (
              <ChevronUp size={14} className="text-muted-foreground" />
            ) : (
              <ChevronDown size={14} className="text-muted-foreground" />
            )}
          </button>
        </div>
      </div>

      {/* Collapsible panel */}
      <div
        className={`transition-all duration-200 ease-in-out overflow-hidden ${
          expanded ? 'max-h-[480px] border-t' : 'max-h-0'
        }`}
      >
        <div className="p-4 overflow-y-auto" style={{ maxHeight: '440px' }}>
          {/* Format Tab */}
          {activeTab === 'format' && (
            <div className="space-y-3">
              {formatTemplate ? (
                <>
                  <div className="flex items-center justify-between">
                    <div>
                      <h4 className="text-sm font-semibold text-foreground">{formatTemplate.name}</h4>
                      {formatTemplate.description && (
                        <p className="text-xs text-muted-foreground mt-0.5">{formatTemplate.description}</p>
                      )}
                    </div>
                    <button
                      onClick={() => setShowTemplateSelector(true)}
                      className="text-xs text-indigo-600 dark:text-indigo-400 hover:text-indigo-700 dark:text-indigo-400 transition-colors px-2 py-1 rounded hover:bg-indigo-50 dark:bg-indigo-500/15"
                    >
                      Change Template
                    </button>
                  </div>

                  <div className="flex items-center gap-2">
                    <span className="text-[10px] bg-teal-50 dark:bg-teal-500/15 text-teal-700 dark:text-teal-400 px-1.5 py-0.5 rounded capitalize">
                      {formatTemplate.document_category}
                    </span>
                  </div>

                  {formatTemplate.format_rules.length > 0 && (
                    <div>
                      <h5 className="text-xs font-medium text-muted-foreground mb-1">Format Rules</h5>
                      <ul className="space-y-0.5">
                        {formatTemplate.format_rules.map((rule, i) => (
                          <li key={i} className="text-xs text-muted-foreground flex items-start gap-1.5">
                            <span className="text-muted-foreground/50 mt-0.5">-</span>
                            {rule}
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}

                  {formatTemplate.required_sections.length > 0 && (
                    <div>
                      <h5 className="text-xs font-medium text-muted-foreground mb-1">Required Sections</h5>
                      <div className="flex flex-wrap gap-1">
                        {formatTemplate.required_sections.map((section, i) => (
                          <span
                            key={i}
                            className="text-[10px] px-1.5 py-0.5 bg-muted text-muted-foreground rounded"
                          >
                            {section.replace(/_/g, ' ')}
                          </span>
                        ))}
                      </div>
                    </div>
                  )}

                  {formatTemplate.content_template_markdown && (
                    <details className="group">
                      <summary className="text-xs font-medium text-muted-foreground cursor-pointer hover:text-foreground">
                        Content Skeleton
                      </summary>
                      <pre className="mt-1 p-2 bg-muted/40 rounded-lg text-[11px] text-muted-foreground overflow-x-auto whitespace-pre-wrap max-h-40 overflow-y-auto">
                        {formatTemplate.content_template_markdown}
                      </pre>
                    </details>
                  )}
                </>
              ) : workspace.format_instructions ? (
                <>
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-2">
                      <Sparkles size={14} className="text-cyan-600 dark:text-cyan-400" />
                      <h4 className="text-sm font-semibold text-foreground">DRPL-suggested format</h4>
                    </div>
                    <button
                      onClick={() => setShowTemplateSelector(true)}
                      className="text-xs text-indigo-600 dark:text-indigo-400 hover:text-indigo-700 dark:text-indigo-400 transition-colors px-2 py-1 rounded hover:bg-indigo-50 dark:bg-indigo-500/15"
                    >
                      Assign Template Instead
                    </button>
                  </div>
                  <div className="bg-cyan-50 dark:bg-cyan-500/15 border border-cyan-200 dark:border-cyan-500/20 rounded-lg p-3">
                    <p className="text-xs text-cyan-800 dark:text-cyan-400">{workspace.format_instructions}</p>
                  </div>
                </>
              ) : (
                <div className="text-center py-6">
                  <FileText size={24} className="mx-auto mb-2 text-muted-foreground/50" />
                  <p className="text-sm text-muted-foreground mb-3">No format template assigned</p>
                  <button
                    onClick={() => setShowTemplateSelector(true)}
                    className="text-sm text-indigo-600 dark:text-indigo-400 hover:text-indigo-700 dark:text-indigo-400 transition-colors px-3 py-1.5 border border-indigo-200 dark:border-indigo-500/20 rounded-lg hover:bg-indigo-50 dark:bg-indigo-500/15"
                  >
                    Choose Template
                  </button>
                </div>
              )}
            </div>
          )}

          {/* Dependencies Tab */}
          {activeTab === 'dependencies' && (
            <DependencyGraph
              tenderId={tenderId}
              itemId={itemId}
              allItems={allItems}
              dependsOn={workspace.depends_on || []}
              referencedBy={workspace.referenced_by || []}
              onUpdateDependencies={onDependenciesChange}
            />
          )}

          {/* Notes Tab */}
          {activeTab === 'notes' && (
            <div>
              <textarea
                value={localNotes}
                onChange={(e) => setLocalNotes(e.target.value)}
                onBlur={handleNotesBlur}
                placeholder="Add notes about this document..."
                rows={6}
                className="w-full border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent resize-none"
              />
              <p className="text-[10px] text-muted-foreground mt-1">Notes are saved when you click away.</p>
            </div>
          )}

          {/* Letterhead Tab */}
          {activeTab === 'letterhead-sigs' && (
            <LetterheadSignaturesTab
              workspace={workspace}
              onLetterheadChange={onLetterheadChange}
              onOrientationChange={onOrientationChange}
              legacySignatureCount={legacySigCount}
              onClearLegacySignatures={onClearLegacySignatures}
            />
          )}
        </div>
      </div>

      {/* Template Selector Modal */}
      {showTemplateSelector && (
        <FormatTemplateSelector
          tenderId={tenderId}
          currentTemplateId={workspace.format_template_id}
          itemCategory={item.item_category}
          onSelect={(templateId) => {
            onTemplateChange(templateId);
            setShowTemplateSelector(false);
          }}
          onClose={() => setShowTemplateSelector(false)}
        />
      )}
    </div>
  );
}
