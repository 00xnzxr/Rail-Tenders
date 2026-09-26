import { useMemo, useCallback, useRef, useEffect, useState } from 'react';
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  type Node,
  type Edge,
  type NodeChange,
  type NodeTypes,
  type NodeProps,
  applyNodeChanges,
  Handle,
  Position,
  BackgroundVariant,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { Bot } from 'lucide-react';
import type { WorkspaceItem, DocumentFormatTemplate, CanvasLayout } from '../../types/workspace';

interface Props {
  items: WorkspaceItem[];
  templateMap?: Record<number, DocumentFormatTemplate>;
  layoutJson: CanvasLayout | null;
  onLayoutSave: (layout: CanvasLayout) => void;
  onOpen: (id: number) => void;
}

// ---- Custom Node Component ----

const STATUS_BORDER: Record<string, string> = {
  not_started: 'border-l-gray-400',
  drafting: 'border-l-blue-500',
  in_review: 'border-l-amber-500',
  approved: 'border-l-emerald-500',
  rejected: 'border-l-red-500',
};

const STATUS_LABEL: Record<string, { text: string; color: string }> = {
  not_started: { text: 'Not Started', color: 'text-muted-foreground' },
  drafting: { text: 'Drafting', color: 'text-accent' },
  in_review: { text: 'In Review', color: 'text-amber-600 dark:text-amber-400' },
  approved: { text: 'Approved', color: 'text-emerald-600 dark:text-emerald-400' },
  rejected: { text: 'Rejected', color: 'text-red-600 dark:text-red-400' },
};

const CATEGORY_COLORS: Record<string, string> = {
  standard: 'bg-muted text-muted-foreground',
  generated: 'bg-accent/10 text-accent',
  analysis: 'bg-amber-50 dark:bg-amber-500/15 text-amber-600 dark:text-amber-400',
};

type DocumentNodeData = {
  label: string;
  item: WorkspaceItem;
  onOpen: (id: number) => void;
};

function DocumentCardNode({ data }: NodeProps<Node<DocumentNodeData>>) {
  const { item, onOpen } = data;
  const statusBorder = STATUS_BORDER[item.review_status] || STATUS_BORDER.not_started;
  const statusInfo = STATUS_LABEL[item.review_status] || STATUS_LABEL.not_started;
  const catColor = CATEGORY_COLORS[item.item_category] || CATEGORY_COLORS.standard;

  return (
    <>
      <Handle type="target" position={Position.Left} className="!w-2 !h-2 !bg-indigo-400 !border-white !border-2" />
      <div
        className={`bg-card border rounded-lg shadow-sm border-l-4 ${statusBorder} cursor-pointer hover:shadow-md transition-shadow`}
        style={{ width: 260, minHeight: 80 }}
        onDoubleClick={() => onOpen(item.id)}
      >
        <div className="p-3">
          {/* Top: category + status */}
          <div className="flex items-center justify-between mb-1.5">
            <span className={`text-[10px] font-medium px-1.5 py-0.5 rounded-full ${catColor}`}>
              {item.item_category}
            </span>
            <span className={`text-[10px] font-medium ${statusInfo.color}`}>
              {item.is_not_required ? 'Not Required' : statusInfo.text}
            </span>
          </div>

          {/* Name */}
          <p className="text-xs font-semibold text-foreground line-clamp-2 leading-tight mb-1.5">
            {item.item_name}
          </p>

          {/* Bottom: agent + version */}
          <div className="flex items-center gap-1">
            {item.agent_key && (
              <span className="inline-flex items-center gap-0.5 text-[9px] text-purple-600 dark:text-purple-400 bg-purple-50 dark:bg-purple-500/15 px-1 py-0.5 rounded">
                <Bot size={8} />
                {item.agent_key}
              </span>
            )}
            {item.content_version > 0 && (
              <span className="text-[9px] text-muted-foreground ml-auto">v{item.content_version}</span>
            )}
          </div>
        </div>
      </div>
      <Handle type="source" position={Position.Right} className="!w-2 !h-2 !bg-indigo-400 !border-white !border-2" />
    </>
  );
}

const nodeTypes: NodeTypes = {
  documentCard: DocumentCardNode as any,
};

// ---- Minimap color by status ----

function getMinimapColor(node: Node): string {
  const item = (node.data as DocumentNodeData)?.item;
  if (!item) return '#d1d5db';
  switch (item.review_status) {
    case 'approved': return '#10b981';
    case 'drafting': return '#3b82f6';
    case 'in_review': return '#f59e0b';
    case 'rejected': return '#ef4444';
    default: return '#9ca3af';
  }
}

// ---- Main Component ----

export default function WorkspaceCanvasView({
  items,
  templateMap,
  layoutJson,
  onLayoutSave,
  onOpen,
}: Props) {
  const saveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const positionsRef = useRef<Record<string, { x: number; y: number }>>({});

  // Build initial nodes from items + saved positions
  const initialNodes = useMemo(() => {
    const savedPositions = layoutJson?.canvas?.positions || {};

    return items.map((item, i) => {
      const savedPos = savedPositions[String(item.id)];
      const defaultX = (i % 4) * 320;
      const defaultY = Math.floor(i / 4) * 180;

      const pos = savedPos || { x: defaultX, y: defaultY };
      positionsRef.current[String(item.id)] = pos;

      return {
        id: String(item.id),
        type: 'documentCard',
        position: pos,
        data: {
          label: item.item_name,
          item,
          onOpen,
        },
      } satisfies Node<DocumentNodeData>;
    });
  }, [items, layoutJson, onOpen]);

  const [nodes, setNodes] = useState<Node<DocumentNodeData>[]>(initialNodes);

  // Re-sync nodes when items change
  useEffect(() => {
    setNodes(initialNodes);
  }, [initialNodes]);

  // Build edges from depends_on relationships (through the workspace data in items)
  const edges = useMemo(() => {
    const result: Edge[] = [];
    // We need to look up each item's depends_on. WorkspaceItem doesn't carry depends_on directly,
    // but the items are keyed — we rely on the item list structure.
    // For the canvas, dependencies are embedded from the workspace overview.
    // Since WorkspaceItem doesn't expose depends_on, we derive from the items list:
    // Actually depends_on is in the DocumentWorkspaceDetail, not in WorkspaceItem.
    // For now, skip edges if there's no depends_on data. The canvas still shows nodes.
    return result;
  }, []);

  const onNodesChange = useCallback(
    (changes: NodeChange<Node<DocumentNodeData>>[]) => {
      setNodes((nds) => {
        const updated = applyNodeChanges(changes, nds);

        // Track position changes for debounced save
        let positionsChanged = false;
        for (const change of changes) {
          if (change.type === 'position' && change.position) {
            positionsRef.current[change.id] = change.position;
            positionsChanged = true;
          }
        }

        if (positionsChanged) {
          // Debounced save: 3 seconds after last position change
          if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
          saveTimerRef.current = setTimeout(() => {
            const newLayout: CanvasLayout = {
              ...(layoutJson || {}),
              canvas: {
                positions: { ...positionsRef.current },
                viewport: layoutJson?.canvas?.viewport,
              },
            };
            onLayoutSave(newLayout);
          }, 3000);
        }

        return updated;
      });
    },
    [layoutJson, onLayoutSave]
  );

  // Cleanup timer on unmount
  useEffect(() => {
    return () => {
      if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
    };
  }, []);

  return (
    <div className="bg-card border rounded-xl overflow-hidden" style={{ height: 'calc(100vh - 240px)' }}>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onNodesChange={onNodesChange}
        fitView
        fitViewOptions={{ padding: 0.2 }}
        minZoom={0.3}
        maxZoom={2}
        snapToGrid
        snapGrid={[20, 20]}
        proOptions={{ hideAttribution: true }}
      >
        <Background variant={BackgroundVariant.Dots} gap={20} size={1} color="#e5e7eb" />
        <Controls
          showInteractive={false}
          className="!bg-card !border !rounded-lg !shadow-sm"
        />
        <MiniMap
          nodeColor={getMinimapColor}
          maskColor="rgba(255,255,255,0.7)"
          className="!bg-muted/40 !border !rounded-lg"
          style={{ width: 160, height: 100 }}
        />
      </ReactFlow>
    </div>
  );
}
