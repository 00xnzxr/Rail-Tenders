import { memo } from 'react';
import { Handle, Position, type NodeProps } from '@xyflow/react';
import {
  Play, Square, Bot, GitBranch, GitCompare, RefreshCw,
  UserCheck, Shuffle, Variable, Wrench, StickyNote,
  Repeat, GitFork,
} from 'lucide-react';

/* ─── Shared wrapper ───────────────────────────────────────────── */

function NodeShell({
  children,
  borderColor,
  bgColor = 'bg-card',
  selected,
  hasTarget = true,
  hasSource = true,
  sourceHandles,
}: {
  children: React.ReactNode;
  borderColor: string;
  bgColor?: string;
  selected?: boolean;
  hasTarget?: boolean;
  hasSource?: boolean;
  sourceHandles?: Array<{ id: string; label: string; position?: number }>;
}) {
  return (
    <div
      className={`relative rounded-lg border-2 shadow-sm min-w-[140px] max-w-[220px] transition-shadow ${bgColor} ${borderColor} ${
        selected ? 'ring-2 ring-blue-400 ring-offset-1 shadow-md' : ''
      }`}
    >
      {hasTarget && (
        <Handle
          type="target"
          position={Position.Left}
          className={`!w-3 !h-3 !border-2 !border-white ${borderColor.replace('border-', '!bg-')}`}
        />
      )}
      <div className="px-3 py-2">{children}</div>
      {hasSource && !sourceHandles && (
        <Handle
          type="source"
          position={Position.Right}
          className={`!w-3 !h-3 !border-2 !border-white ${borderColor.replace('border-', '!bg-')}`}
        />
      )}
      {sourceHandles?.map((h, i) => {
        const total = sourceHandles.length;
        const spacing = 100 / (total + 1);
        return (
          <Handle
            key={h.id}
            type="source"
            position={Position.Right}
            id={h.id}
            style={{ top: `${spacing * (i + 1)}%` }}
            className={`!w-3 !h-3 !border-2 !border-white ${borderColor.replace('border-', '!bg-')}`}
          />
        );
      })}
    </div>
  );
}

/* ─── Start Node ───────────────────────────────────────────────── */

export const StartNode = memo(({ selected }: NodeProps) => (
  <NodeShell borderColor="border-green-400" bgColor="bg-green-50 dark:bg-green-500/15" selected={selected} hasTarget={false}>
    <div className="flex items-center gap-2 text-sm font-semibold text-green-800 dark:text-green-400">
      <Play size={14} className="fill-green-600 text-green-600 dark:text-green-400" />
      Start
    </div>
  </NodeShell>
));
StartNode.displayName = 'StartNode';

/* ─── End Node ─────────────────────────────────────────────────── */

export const EndNode = memo(({ selected }: NodeProps) => (
  <NodeShell borderColor="border-purple-400" bgColor="bg-purple-50 dark:bg-purple-500/15" selected={selected} hasSource={false}>
    <div className="flex items-center gap-2 text-sm font-semibold text-purple-800 dark:text-purple-400">
      <Square size={14} className="fill-purple-500 text-purple-500" />
      End
    </div>
  </NodeShell>
));
EndNode.displayName = 'EndNode';

/* ─── Agent Node ───────────────────────────────────────────────── */

export const AgentNode = memo(({ data, selected }: NodeProps) => (
  <NodeShell borderColor="border-blue-400" selected={selected}>
    <div className="flex items-center gap-2 text-sm font-medium text-foreground">
      <Bot size={14} className="text-accent shrink-0" />
      <span className="truncate">{(data as any)?.label || 'Agent'}</span>
    </div>
    {(data as any)?.agent_key && (
      <div className="text-[10px] text-muted-foreground mt-0.5 truncate">{(data as any).agent_key}</div>
    )}
  </NodeShell>
));
AgentNode.displayName = 'AgentNode';

/* ─── Classify / Router Node ──────────────────────────────────── */

export const ClassifyNode = memo(({ data, selected }: NodeProps) => {
  const branches: Array<{ label: string }> = (data as any)?.config?.branches || [];
  const handles = branches.map((b, i) => ({ id: b.label, label: b.label, position: i }));
  // Always have at least one default handle
  if (handles.length === 0) {
    handles.push({ id: 'default', label: 'default', position: 0 });
  }
  return (
    <NodeShell borderColor="border-amber-400" bgColor="bg-amber-50 dark:bg-amber-500/15" selected={selected} sourceHandles={handles}>
      <div className="flex items-center gap-2 text-sm font-medium text-foreground">
        <GitBranch size={14} className="text-amber-600 dark:text-amber-400 shrink-0" />
        <span className="truncate">{(data as any)?.label || 'Classify'}</span>
      </div>
      {branches.length > 0 && (
        <div className="mt-1 space-y-0.5">
          {branches.map((b) => (
            <div key={b.label} className="text-[10px] text-amber-600 dark:text-amber-400 truncate">{b.label}</div>
          ))}
        </div>
      )}
    </NodeShell>
  );
});
ClassifyNode.displayName = 'ClassifyNode';

/* ─── If/Else Node ─────────────────────────────────────────────── */

export const IfElseNode = memo(({ data, selected }: NodeProps) => {
  const trueLabel = (data as any)?.config?.true_label || 'True';
  const falseLabel = (data as any)?.config?.false_label || 'False';
  return (
    <NodeShell
      borderColor="border-yellow-400"
      bgColor="bg-yellow-50 dark:bg-yellow-500/15"
      selected={selected}
      sourceHandles={[
        { id: 'true', label: trueLabel },
        { id: 'false', label: falseLabel },
      ]}
    >
      <div className="flex items-center gap-2 text-sm font-medium text-foreground">
        <GitCompare size={14} className="text-yellow-600 dark:text-yellow-400 shrink-0" />
        <span className="truncate">{(data as any)?.label || 'If / Else'}</span>
      </div>
      <div className="text-[10px] text-muted-foreground mt-0.5 truncate">
        {(data as any)?.config?.condition_expression || 'condition'}
      </div>
    </NodeShell>
  );
});
IfElseNode.displayName = 'IfElseNode';

/* ─── While Loop Node ──────────────────────────────────────────── */

export const WhileLoopNode = memo(({ data, selected }: NodeProps) => {
  const max = (data as any)?.config?.max_iterations || '?';
  return (
    <NodeShell
      borderColor="border-orange-400"
      bgColor="bg-orange-50 dark:bg-orange-500/15"
      selected={selected}
      sourceHandles={[
        { id: 'body', label: 'Loop Body' },
        { id: 'done', label: 'Done' },
      ]}
    >
      <div className="flex items-center gap-2 text-sm font-medium text-foreground">
        <RefreshCw size={14} className="text-orange-600 dark:text-orange-400 shrink-0" />
        <span className="truncate">{(data as any)?.label || 'While Loop'}</span>
      </div>
      <div className="text-[10px] text-muted-foreground mt-0.5">max {max} iterations</div>
    </NodeShell>
  );
});
WhileLoopNode.displayName = 'WhileLoopNode';

/* ─── User Approval Node ──────────────────────────────────────── */

export const UserApprovalNode = memo(({ data, selected }: NodeProps) => (
  <NodeShell
    borderColor="border-indigo-400"
    bgColor="bg-indigo-50 dark:bg-indigo-500/15"
    selected={selected}
    sourceHandles={[
      { id: 'approved', label: 'Approved' },
      { id: 'rejected', label: 'Rejected' },
    ]}
  >
    <div className="flex items-center gap-2 text-sm font-medium text-foreground">
      <UserCheck size={14} className="text-indigo-600 dark:text-indigo-400 shrink-0" />
      <span className="truncate">{(data as any)?.label || 'User Approval'}</span>
    </div>
  </NodeShell>
));
UserApprovalNode.displayName = 'UserApprovalNode';

/* ─── Transform Node ───────────────────────────────────────────── */

export const TransformNode = memo(({ data, selected }: NodeProps) => (
  <NodeShell borderColor="border-border" selected={selected}>
    <div className="flex items-center gap-2 text-sm font-medium text-foreground">
      <Shuffle size={14} className="text-muted-foreground shrink-0" />
      <span className="truncate">{(data as any)?.label || 'Transform'}</span>
    </div>
  </NodeShell>
));
TransformNode.displayName = 'TransformNode';

/* ─── Set State Node ───────────────────────────────────────────── */

export const SetStateNode = memo(({ data, selected }: NodeProps) => (
  <NodeShell borderColor="border-border" bgColor="bg-muted/40" selected={selected}>
    <div className="flex items-center gap-2 text-sm font-medium text-foreground">
      <Variable size={14} className="text-muted-foreground shrink-0" />
      <span className="truncate">{(data as any)?.label || 'Set State'}</span>
    </div>
    {(data as any)?.config?.assignments?.length > 0 && (
      <div className="text-[10px] text-muted-foreground mt-0.5">
        {(data as any).config.assignments.length} assignments
      </div>
    )}
  </NodeShell>
));
SetStateNode.displayName = 'SetStateNode';

/* ─── Tool Node ────────────────────────────────────────────────── */

export const ToolNode = memo(({ data, selected }: NodeProps) => (
  <NodeShell borderColor="border-emerald-400" bgColor="bg-emerald-50 dark:bg-emerald-500/15" selected={selected}>
    <div className="flex items-center gap-2 text-sm font-medium text-foreground">
      <Wrench size={14} className="text-emerald-600 dark:text-emerald-400 shrink-0" />
      <span className="truncate">{(data as any)?.label || 'Tool'}</span>
    </div>
    {(data as any)?.config?.tool_key && (
      <div className="text-[10px] text-muted-foreground mt-0.5 truncate">{(data as any).config.tool_key}</div>
    )}
  </NodeShell>
));
ToolNode.displayName = 'ToolNode';

/* ─── Note Node ────────────────────────────────────────────────── */

export const NoteNode = memo(({ data, selected }: NodeProps) => (
  <div
    className={`rounded-lg border-2 border-dashed border-border bg-yellow-50/50 px-3 py-2 min-w-[120px] max-w-[200px] ${
      selected ? 'ring-2 ring-blue-400 ring-offset-1' : ''
    }`}
  >
    <div className="flex items-center gap-2 text-sm font-medium text-muted-foreground">
      <StickyNote size={14} className="text-yellow-500 shrink-0" />
      <span className="truncate">{(data as any)?.label || 'Note'}</span>
    </div>
    {(data as any)?.config?.text && (
      <p className="text-[10px] text-muted-foreground mt-1 line-clamp-2">{(data as any).config.text}</p>
    )}
  </div>
));
NoteNode.displayName = 'NoteNode';

/* ─── For Each Node ────────────────────────────────────────────── */

export const ForEachNode = memo(({ data, selected }: NodeProps) => {
  const config = (data as any)?.config || {};
  const isParallel = config.parallel;
  return (
    <NodeShell
      borderColor="border-teal-400"
      bgColor="bg-teal-50 dark:bg-teal-500/15"
      selected={selected}
      sourceHandles={[
        { id: 'body', label: 'Body' },
        { id: 'done', label: 'Done' },
      ]}
    >
      <div className="flex items-center gap-2 text-sm font-medium text-foreground">
        <Repeat size={14} className="text-teal-600 dark:text-teal-400 shrink-0" />
        <span className="truncate">{(data as any)?.label || 'For Each'}</span>
      </div>
      <div className="flex items-center gap-1.5 mt-1">
        {isParallel && (
          <span className="text-[9px] font-semibold px-1.5 py-0.5 rounded bg-teal-100 dark:bg-teal-500/20 text-teal-700 dark:text-teal-400">PARALLEL</span>
        )}
        {config.max_iterations && (
          <span className="text-[10px] text-teal-600 dark:text-teal-400">max {config.max_iterations}</span>
        )}
      </div>
      {config.collection_expression && (
        <div className="text-[10px] text-muted-foreground mt-0.5 truncate font-mono">{config.collection_expression}</div>
      )}
    </NodeShell>
  );
});
ForEachNode.displayName = 'ForEachNode';

/* ─── Parallel Node ────────────────────────────────────────────── */

export const ParallelNode = memo(({ data, selected }: NodeProps) => {
  const config = (data as any)?.config || {};
  const branches: Array<{ label: string }> = config.branches || [];
  return (
    <NodeShell
      borderColor="border-rose-400"
      bgColor="bg-rose-50 dark:bg-rose-500/15"
      selected={selected}
      sourceHandles={[{ id: 'done', label: 'Done' }]}
    >
      <div className="flex items-center gap-2 text-sm font-medium text-foreground">
        <GitFork size={14} className="text-rose-600 dark:text-rose-400 shrink-0" />
        <span className="truncate">{(data as any)?.label || 'Parallel'}</span>
      </div>
      {branches.length > 0 && (
        <div className="mt-1 space-y-0.5">
          {branches.map((b) => (
            <div key={b.label} className="text-[10px] text-rose-600 dark:text-rose-400 truncate">{b.label}</div>
          ))}
        </div>
      )}
      {branches.length === 0 && (
        <div className="text-[10px] text-muted-foreground mt-0.5">No branches configured</div>
      )}
    </NodeShell>
  );
});
ParallelNode.displayName = 'ParallelNode';

/* ─── Node Type Registry ───────────────────────────────────────── */

export const workflowNodeTypes = {
  start: StartNode,
  end: EndNode,
  agent: AgentNode,
  classify: ClassifyNode,
  if_else: IfElseNode,
  while_loop: WhileLoopNode,
  user_approval: UserApprovalNode,
  transform: TransformNode,
  set_state: SetStateNode,
  tool: ToolNode,
  note: NoteNode,
  for_each: ForEachNode,
  parallel: ParallelNode,
};
