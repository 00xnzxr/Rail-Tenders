import { useCallback, useRef, useEffect } from 'react';
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  useNodesState,
  useEdgesState,
  addEdge,
  type Connection,
  type Node,
  type Edge,
  BackgroundVariant,
  type ReactFlowInstance,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { workflowNodeTypes } from './nodes';
import type { NodeType, WorkflowNode, WorkflowEdge } from '../../types/workflow';

interface WorkflowCanvasProps {
  initialNodes: Node[];
  initialEdges: Edge[];
  onNodesChange: (nodes: Node[]) => void;
  onEdgesChange: (edges: Edge[]) => void;
  onNodeSelect: (nodeKey: string | null) => void;
  onDrop?: (type: NodeType, position: { x: number; y: number }) => void;
  activeNodeKey?: string | null;
}

export default function WorkflowCanvas({
  initialNodes,
  initialEdges,
  onNodesChange: onNodesChangeProp,
  onEdgesChange: onEdgesChangeProp,
  onNodeSelect,
  onDrop,
  activeNodeKey,
}: WorkflowCanvasProps) {
  const reactFlowInstance = useRef<ReactFlowInstance | null>(null);
  const [nodes, setNodes, onNodesChange] = useNodesState(initialNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(initialEdges);

  // Highlight the actively executing node during test runs
  useEffect(() => {
    setNodes((nds) =>
      nds.map((n) => ({
        ...n,
        className: n.id === activeNodeKey ? 'ring-2 ring-blue-500 ring-offset-2 rounded-lg shadow-lg shadow-blue-200 animate-pulse' : '',
      })),
    );
  }, [activeNodeKey, setNodes]);

  // Sync external state on changes
  const handleNodesChange = useCallback(
    (changes: any) => {
      onNodesChange(changes);
      // Defer sync to avoid stale state
      setTimeout(() => {
        const current = reactFlowInstance.current?.getNodes();
        if (current) onNodesChangeProp(current);
      }, 0);
    },
    [onNodesChange, onNodesChangeProp],
  );

  const handleEdgesChange = useCallback(
    (changes: any) => {
      onEdgesChange(changes);
      setTimeout(() => {
        const current = reactFlowInstance.current?.getEdges();
        if (current) onEdgesChangeProp(current);
      }, 0);
    },
    [onEdgesChange, onEdgesChangeProp],
  );

  const onConnect = useCallback(
    (connection: Connection) => {
      const newEdge: Edge = {
        ...connection,
        id: `e_${connection.source}_${connection.sourceHandle || 'default'}_${connection.target}`,
        label: connection.sourceHandle || undefined,
        animated: true,
        style: { stroke: '#94a3b8', strokeWidth: 2 },
        labelStyle: { fontSize: 10, fontWeight: 500, fill: '#64748b' },
        labelBgStyle: { fill: '#f8fafc', fillOpacity: 0.9 },
        labelBgPadding: [4, 2] as [number, number],
        labelBgBorderRadius: 4,
      };
      setEdges((eds) => {
        const next = addEdge(newEdge, eds);
        setTimeout(() => onEdgesChangeProp(next), 0);
        return next;
      });
    },
    [setEdges, onEdgesChangeProp],
  );

  const onNodeClick = useCallback(
    (_: any, node: Node) => {
      onNodeSelect(node.id);
    },
    [onNodeSelect],
  );

  const onPaneClick = useCallback(() => {
    onNodeSelect(null);
  }, [onNodeSelect]);

  // Drag-and-drop from toolbox
  const onDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
  }, []);

  const handleDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      const type = e.dataTransfer.getData('application/workflow-node-type') as NodeType;
      if (!type || !reactFlowInstance.current) return;

      const position = reactFlowInstance.current.screenToFlowPosition({
        x: e.clientX,
        y: e.clientY,
      });
      onDrop?.(type, position);
    },
    [onDrop],
  );

  // Expose nodes/edges to parent via setter
  const updateNodes = useCallback(
    (updater: (prev: Node[]) => Node[]) => {
      setNodes((prev) => {
        const next = updater(prev);
        setTimeout(() => onNodesChangeProp(next), 0);
        return next;
      });
    },
    [setNodes, onNodesChangeProp],
  );

  return (
    <div className="flex-1 h-full">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodesChange={handleNodesChange}
        onEdgesChange={handleEdgesChange}
        onConnect={onConnect}
        onNodeClick={onNodeClick}
        onPaneClick={onPaneClick}
        onDragOver={onDragOver}
        onDrop={handleDrop}
        onInit={(instance) => { reactFlowInstance.current = instance; }}
        nodeTypes={workflowNodeTypes}
        fitView
        snapToGrid
        snapGrid={[15, 15]}
        defaultEdgeOptions={{
          animated: true,
          style: { stroke: '#94a3b8', strokeWidth: 2 },
        }}
        deleteKeyCode={['Backspace', 'Delete']}
      >
        <Background variant={BackgroundVariant.Dots} gap={15} size={1} color="#e2e8f0" />
        <Controls position="bottom-left" showInteractive={false} />
        <MiniMap
          position="bottom-right"
          nodeColor={(n) => {
            const colors: Record<string, string> = {
              start: '#86efac', end: '#c4b5fd', agent: '#93c5fd',
              classify: '#fcd34d', if_else: '#fde68a', while_loop: '#fdba74',
              user_approval: '#a5b4fc', transform: '#cbd5e1', set_state: '#cbd5e1',
              tool: '#6ee7b7', note: '#fef3c7',
              for_each: '#5eead4', parallel: '#fda4af',
            };
            return colors[n.type || ''] || '#e2e8f0';
          }}
          maskColor="rgba(0,0,0,0.05)"
          pannable
          zoomable
        />
      </ReactFlow>
    </div>
  );
}

/* ─── Helpers to convert between backend and React Flow formats ── */

export function toReactFlowNodes(workflowNodes: WorkflowNode[]): Node[] {
  return workflowNodes.map((n) => ({
    id: n.node_key,
    type: n.node_type,
    position: n.position || { x: n.position_x || 0, y: n.position_y || 0 },
    data: {
      label: n.display_name || n.node_type,
      config: n.config || {},
      agent_key: n.config?.agent_key,
    },
  }));
}

export function toReactFlowEdges(workflowEdges: WorkflowEdge[]): Edge[] {
  return workflowEdges.map((e, i) => ({
    id: e.id ? `e_${e.id}` : `e_${e.source}_${e.sourceHandle || 'default'}_${e.target}_${i}`,
    source: e.source,
    target: e.target,
    sourceHandle: e.sourceHandle || undefined,
    label: e.label || e.sourceHandle || undefined,
    animated: true,
    style: { stroke: '#94a3b8', strokeWidth: 2 },
    labelStyle: { fontSize: 10, fontWeight: 500, fill: '#64748b' },
    labelBgStyle: { fill: '#f8fafc', fillOpacity: 0.9 },
    labelBgPadding: [4, 2] as [number, number],
    labelBgBorderRadius: 4,
  }));
}

export function fromReactFlowNodes(nodes: Node[]): WorkflowNode[] {
  return nodes.map((n, i) => ({
    node_key: n.id,
    node_type: (n.type || 'agent') as NodeType,
    display_name: (n.data as any)?.label || n.type || '',
    description: (n.data as any)?.description,
    position: n.position,
    position_x: n.position.x,
    position_y: n.position.y,
    config: (n.data as any)?.config || {},
    sort_order: i,
  }));
}

export function fromReactFlowEdges(edges: Edge[]): WorkflowEdge[] {
  return edges.map((e, i) => ({
    source: e.source,
    target: e.target,
    sourceHandle: e.sourceHandle || undefined,
    label: typeof e.label === 'string' ? e.label : undefined,
    sort_order: i,
  }));
}
