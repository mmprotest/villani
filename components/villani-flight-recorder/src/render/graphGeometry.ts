import { GraphNodeViewModel } from "./viewModel.js";

export const GRAPH_WIDTH = 980;
export const GRAPH_HEIGHT = 350;
export const NODE_H = 64;
export const NODE_W = 138;
export const NODE_W_WIDE = 164;

export const LANE_Y = {
  recorder: 56,
  captured: 166,
  repository: 276,
} as const;

export interface Point {
  x: number;
  y: number;
}

export const GRAPH_NODE_LAYOUT = {
  discover: { x: 24, y: LANE_Y.recorder, width: NODE_W, height: NODE_H },
  parse: { x: 198, y: LANE_Y.recorder, width: NODE_W, height: NODE_H },
  normalize: { x: 372, y: LANE_Y.recorder, width: NODE_W, height: NODE_H },
  replayOutput: {
    x: 552,
    y: LANE_Y.recorder,
    width: NODE_W_WIDE,
    height: NODE_H,
  },
  agentEvents: { x: 372, y: LANE_Y.captured, width: NODE_W, height: NODE_H },
  commandsTools: {
    x: 552,
    y: LANE_Y.captured,
    width: NODE_W_WIDE,
    height: NODE_H,
  },
  fileChanges: { x: 748, y: LANE_Y.captured, width: NODE_W, height: NODE_H },
  correlate: { x: 198, y: LANE_Y.repository, width: NODE_W, height: NODE_H },
  gitState: { x: 372, y: LANE_Y.repository, width: NODE_W, height: NODE_H },
  diffCapture: {
    x: 552,
    y: LANE_Y.repository,
    width: NODE_W_WIDE,
    height: NODE_H,
  },
} as const;

export const GRAPH_COORDS: Record<
  string,
  { x: number; y: number; width: number; height: number }
> = {
  discover: GRAPH_NODE_LAYOUT.discover,
  parse: GRAPH_NODE_LAYOUT.parse,
  normalize: GRAPH_NODE_LAYOUT.normalize,
  correlate: GRAPH_NODE_LAYOUT.correlate,
  "replay-output": GRAPH_NODE_LAYOUT.replayOutput,
  "agent-events": GRAPH_NODE_LAYOUT.agentEvents,
  commands: GRAPH_NODE_LAYOUT.commandsTools,
  "file-changes": GRAPH_NODE_LAYOUT.fileChanges,
  "git-state": GRAPH_NODE_LAYOUT.gitState,
  "diff-capture": GRAPH_NODE_LAYOUT.diffCapture,
};

export function leftCenter(node: GraphNodeViewModel): Point {
  return { x: node.x, y: node.y + node.height / 2 };
}

export function rightCenter(node: GraphNodeViewModel): Point {
  return { x: node.x + node.width, y: node.y + node.height / 2 };
}

export function topCenter(node: GraphNodeViewModel): Point {
  return { x: node.x + node.width / 2, y: node.y };
}

export function bottomCenter(node: GraphNodeViewModel): Point {
  return { x: node.x + node.width / 2, y: node.y + node.height };
}

function straightPath(from: Point, to: Point): string {
  return `M ${from.x} ${from.y} L ${to.x} ${to.y}`;
}

export function elbowPath(
  from: Point,
  to: Point,
  options: { radius?: number; viaX?: number; viaY?: number } = {},
): string {
  if (Math.abs(from.y - to.y) < 1 || Math.abs(from.x - to.x) < 1) {
    return straightPath(from, to);
  }
  const corner = { x: options.viaX ?? to.x, y: options.viaY ?? from.y };
  const r = Math.min(
    options.radius ?? 10,
    Math.abs(corner.x - from.x) / 2,
    Math.abs(to.y - corner.y) / 2,
  );
  const sx = Math.sign(corner.x - from.x) || 1;
  const sy = Math.sign(to.y - corner.y) || 1;
  return `M ${from.x} ${from.y} H ${corner.x - sx * r} Q ${corner.x} ${from.y} ${corner.x} ${from.y + sy * r} V ${to.y}`;
}

export function horizontalLink(
  fromNode: GraphNodeViewModel,
  toNode: GraphNodeViewModel,
): string {
  return straightPath(rightCenter(fromNode), leftCenter(toNode));
}

export function verticalDropLink(
  fromNode: GraphNodeViewModel,
  toNode: GraphNodeViewModel,
): string {
  return straightPath(bottomCenter(fromNode), topCenter(toNode));
}

export function elbowLink(
  fromNode: GraphNodeViewModel,
  toNode: GraphNodeViewModel,
  options: { viaX?: number; viaY?: number; radius?: number } = {},
): string {
  const from = bottomCenter(fromNode);
  const to = topCenter(toNode);
  const viaY = options.viaY ?? from.y + 18;
  const viaX = options.viaX ?? to.x;
  return `M ${from.x} ${from.y} V ${viaY} H ${viaX} V ${to.y}`;
}
