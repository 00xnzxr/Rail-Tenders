import { Bot, Wrench, Brain, Play, Square, Eye, RotateCcw } from 'lucide-react';

interface AgentNodeGraphProps {
  agentType: 'chain_of_thought' | 'react' | 'tool_use' | 'orchestrator';
  tools?: Array<{ tool_key: string; display_name: string }>;
}

interface NodeDef {
  x: number;
  y: number;
  w: number;
  h: number;
  label: string;
  fill: string;
  stroke: string;
  icon?: 'play' | 'bot' | 'square' | 'brain' | 'wrench' | 'eye' | 'rotate';
}

function NodeIcon({ icon, x, y }: { icon?: string; x: number; y: number }) {
  const props = { size: 14, x: x - 7, y: y - 7, className: 'text-muted-foreground' };
  switch (icon) {
    case 'play': return <Play {...props} />;
    case 'bot': return <Bot {...props} />;
    case 'square': return <Square {...props} />;
    case 'brain': return <Brain {...props} />;
    case 'wrench': return <Wrench {...props} />;
    case 'eye': return <Eye {...props} />;
    case 'rotate': return <RotateCcw {...props} />;
    default: return null;
  }
}

function RoundedNode({ node }: { node: NodeDef }) {
  return (
    <g>
      <rect
        x={node.x} y={node.y} width={node.w} height={node.h}
        rx={12} fill={node.fill} stroke={node.stroke} strokeWidth={1.5}
      />
      <foreignObject x={node.x} y={node.y} width={node.w} height={node.h}>
        <div className="flex items-center justify-center gap-1.5 h-full text-xs font-medium text-foreground select-none">
          <NodeIcon icon={node.icon} x={0} y={0} />
          {node.label}
        </div>
      </foreignObject>
    </g>
  );
}

function Arrow({ x1, y1, x2, y2 }: { x1: number; y1: number; x2: number; y2: number }) {
  return <line x1={x1} y1={y1} x2={x2} y2={y2} stroke="#94a3b8" strokeWidth={1.5} markerEnd="url(#arrow)" />;
}

function ChainOfThoughtGraph() {
  const nodes: NodeDef[] = [
    { x: 20, y: 30, w: 100, h: 40, label: 'Input', fill: '#dcfce7', stroke: '#86efac', icon: 'play' },
    { x: 180, y: 30, w: 120, h: 40, label: 'LLM Call', fill: '#dbeafe', stroke: '#93c5fd', icon: 'bot' },
    { x: 360, y: 30, w: 100, h: 40, label: 'Output', fill: '#f3e8ff', stroke: '#c4b5fd', icon: 'square' },
  ];
  return (
    <svg viewBox="0 0 480 100" className="w-full h-auto">
      <defs>
        <marker id="arrow" viewBox="0 0 10 10" refX={8} refY={5} markerWidth={6} markerHeight={6} orient="auto-start-reverse">
          <path d="M 0 0 L 10 5 L 0 10 z" fill="#94a3b8" />
        </marker>
      </defs>
      {nodes.map((n, i) => <RoundedNode key={i} node={n} />)}
      <Arrow x1={120} y1={50} x2={180} y2={50} />
      <Arrow x1={300} y1={50} x2={360} y2={50} />
    </svg>
  );
}

function ReactGraph({ tools }: { tools?: Array<{ tool_key: string; display_name: string }> }) {
  const toolList = tools ?? [];
  const toolCount = Math.max(toolList.length, 1);
  const toolBlockW = toolCount * 100 + (toolCount - 1) * 10;
  const svgW = Math.max(520, toolBlockW + 140);
  const svgH = toolCount > 0 ? 310 : 250;

  const inputNode: NodeDef = { x: 10, y: 40, w: 90, h: 40, label: 'Input', fill: '#dcfce7', stroke: '#86efac', icon: 'play' };
  const thinkNode: NodeDef = { x: 130, y: 40, w: 120, h: 40, label: 'Think (LLM)', fill: '#dbeafe', stroke: '#93c5fd', icon: 'brain' };
  const decisionNode: NodeDef = { x: 280, y: 40, w: 110, h: 40, label: 'Decision', fill: '#fef9c3', stroke: '#fde047', icon: 'bot' };
  const outputNode: NodeDef = { x: 420, y: 40, w: 90, h: 40, label: 'Output', fill: '#f3e8ff', stroke: '#c4b5fd', icon: 'square' };
  const actNode: NodeDef = { x: 290, y: 120, w: 90, h: 40, label: 'Act (Tool)', fill: '#ffedd5', stroke: '#fdba74', icon: 'wrench' };
  const observeNode: NodeDef = { x: 285, y: 230, w: 100, h: 40, label: 'Observe', fill: '#e0e7ff', stroke: '#a5b4fc', icon: 'eye' };

  const toolStartX = (svgW - toolBlockW) / 2;

  return (
    <svg viewBox={`0 0 ${svgW} ${svgH}`} className="w-full h-auto">
      <defs>
        <marker id="arrow" viewBox="0 0 10 10" refX={8} refY={5} markerWidth={6} markerHeight={6} orient="auto-start-reverse">
          <path d="M 0 0 L 10 5 L 0 10 z" fill="#94a3b8" />
        </marker>
      </defs>
      {/* Main flow */}
      <RoundedNode node={inputNode} />
      <RoundedNode node={thinkNode} />
      <RoundedNode node={decisionNode} />
      <RoundedNode node={outputNode} />
      <Arrow x1={100} y1={60} x2={130} y2={60} />
      <Arrow x1={250} y1={60} x2={280} y2={60} />
      <Arrow x1={390} y1={60} x2={420} y2={60} />

      {/* Down to Act */}
      <Arrow x1={335} y1={80} x2={335} y2={120} />
      <RoundedNode node={actNode} />

      {/* Tool nodes */}
      {toolList.map((tool, i) => {
        const tx = toolStartX + i * 110;
        const toolNode: NodeDef = {
          x: tx, y: 175, w: 100, h: 36, label: tool.display_name,
          fill: '#f0fdf4', stroke: '#86efac', icon: 'wrench',
        };
        return (
          <g key={tool.tool_key}>
            <RoundedNode node={toolNode} />
            {/* dashed line from Act to tool */}
            <line x1={335} y1={160} x2={tx + 50} y2={175} stroke="#a3e635" strokeWidth={1} strokeDasharray="4 3" />
          </g>
        );
      })}

      {/* Down to Observe */}
      <Arrow x1={335} y1={toolList.length > 0 ? 211 : 160} x2={335} y2={230} />
      <RoundedNode node={observeNode} />

      {/* Curved arrow back from Observe to Think */}
      <path
        d={`M 285 250 Q 80 250 80 100 Q 80 60 130 60`}
        fill="none" stroke="#94a3b8" strokeWidth={1.5} strokeDasharray="6 3"
        markerEnd="url(#arrow)"
      />
      <foreignObject x={30} y={140} width={50} height={20}>
        <span className="text-[9px] text-muted-foreground italic">loop</span>
      </foreignObject>
    </svg>
  );
}

export default function AgentNodeGraph({ agentType, tools }: AgentNodeGraphProps) {
  if (agentType === 'orchestrator') {
    return (
      <div className="flex items-center justify-center py-8 text-sm text-muted-foreground italic border border-dashed border-border rounded-lg bg-muted/40">
        Use the visual editor below to configure the workflow graph.
      </div>
    );
  }

  if (agentType === 'chain_of_thought') {
    return (
      <div className="border border-border rounded-lg bg-card p-4">
        <ChainOfThoughtGraph />
      </div>
    );
  }

  return (
    <div className="border border-border rounded-lg bg-card p-4">
      <ReactGraph tools={tools} />
    </div>
  );
}
