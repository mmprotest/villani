import { safeJsonForScript } from "./safeHtml.js";
import { themeCss } from "./theme.js";
import { clientScript } from "./clientScript.js";
export function htmlTemplate(vm, body) {
    const data = {
        events: vm.timeline.map((event) => event.raw),
        timeline: vm.timeline,
        graph: vm.graph.nodes,
        provider: vm.provider,
    };
    return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Villani Flight Recorder</title><style>${themeCss()}</style></head><body>${body}<script>const replayData=${safeJsonForScript(data)};${clientScript()}</script></body></html>`;
}
