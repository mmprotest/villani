import path from "node:path";
import { SessionIndex, SessionRecord } from "../index/sessionTypes.js";
import { escapeHtml, safeJsonForScript } from "./safeHtml.js";
import { themeCss } from "./theme.js";
import { formatTokenCount } from "../providers/helpers/tokens.js";

const label = (p: string) =>
  p === "claude"
    ? "Claude"
    : p === "codex"
      ? "Codex"
      : p === "pi"
        ? "Pi"
        : p === "generic"
          ? "Generic"
          : p;
const outcomeOf = (s: SessionRecord) =>
  s.outcome ?? (s.failedCommandCount > 0 ? "failed" : "unknown");
export function renderSessionBrowser(
  index: SessionIndex,
  opts: { replayDir?: string; browserOut?: string } = {},
) {
  const rows = index.sessions.map((s) => ({
    id: s.id,
    provider: s.provider,
    providerLabel: s.providerLabel ?? label(String(s.provider)),
    outcome: outcomeOf(s),
    projectId:
      s.projectId ??
      s.repoIds?.[0] ??
      s.projectPath ??
      s.repoRoots?.[0] ??
      "unknown",
    repoIds: s.repoIds ?? [],
    repoRoots: s.repoRoots ?? [],
    project:
      s.projectDisplayName ??
      s.projectName ??
      s.repoRoots?.[0]?.split(/[\\/]/).pop() ??
      "Unknown project",
    projectPath: s.projectPath ?? s.projectRoot ?? s.repoRoots?.[0] ?? "",
    title: s.title ?? s.firstPrompt ?? "Untitled session",
    updated: s.updatedAt ?? s.lastEventAt ?? s.indexedAt ?? "",
    eventCount: s.eventCount ?? 0,
    failedCommandCount: s.failedCommandCount ?? 0,
    changedFileCount: s.changedFileCount ?? s.fileEventCount ?? 0,
    changedFiles: s.changedFiles ?? [],
    model: s.model ?? "",
    durationMs: s.durationMs,
    tokenCount: s.tokenCount,
    tokenLabel:
      s.tokenCount !== undefined ? formatTokenCount(s.tokenCount) : "",
    failureSummary: s.failureSummary ?? "",
    sourcePath: s.sourcePath,
    replayHref:
      opts.replayDir && opts.browserOut
        ? path.relative(
            path.dirname(opts.browserOut),
            path.join(opts.replayDir, `${s.id}.html`),
          )
        : `replays/${s.id}.html`,
  }));
  const providers = [...new Set(rows.map((r) => r.provider))];
  const failed = rows.filter((r) => r.outcome === "failed").length;
  const projectMap = new Map<
    string,
    {
      id: string;
      name: string;
      path?: string;
      sessionCount: number;
      failedCount: number;
      latestUpdated?: string;
    }
  >();
  for (const r of rows) {
    const id = r.projectId || r.projectPath || r.project;
    const cur = projectMap.get(id) || {
      id,
      name: r.project,
      path: r.projectPath,
      sessionCount: 0,
      failedCount: 0,
      latestUpdated: "",
    };
    cur.sessionCount++;
    if (r.outcome === "failed") cur.failedCount++;
    if (String(r.updated) > String(cur.latestUpdated || ""))
      cur.latestUpdated = r.updated;
    projectMap.set(id, cur);
  }
  const projects = [...projectMap.values()].sort((a, b) =>
    a.name.localeCompare(b.name),
  );
  const lastIndexed =
    index.generatedAt ||
    rows
      .map((r) => r.updated)
      .sort()
      .at(-1) ||
    "—";
  const empty = rows.length === 0;
  const data = rows.map((r) => ({
    ...r,
    search: [
      r.providerLabel,
      r.outcome,
      r.project,
      r.projectPath,
      r.title,
      r.sourcePath,
      r.model,
      r.tokenLabel,
    ]
      .join(" ")
      .toLowerCase(),
    replayCommand: `vfr replay --id ${r.id}`,
  }));
  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Villani Flight Recorder Sessions</title>
<style>${themeCss()}
/* legacy avoided: grid-template-columns:minmax(92px,112px) minmax(158px,176px) minmax(72px,84px) */.session-browser .app-shell{max-width:1360px}.browser-hero{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:18px;align-items:end}.browser-kicker{margin:0 0 5px;color:var(--text-muted);font-size:13px}.browser-subtitle{margin:7px 0 0;color:var(--text-soft);max-width:760px;line-height:1.45}.summary-row{display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end}.summary-row .replay-chip b{color:var(--text)}.snapshot-note{margin:10px 0 0;color:var(--text-muted);font-size:12px;line-height:1.45}.controls{display:grid;gap:9px;margin-top:14px}.control-line{display:flex;gap:9px;align-items:center;flex-wrap:wrap}.control-line input,.control-line select{border:1px solid rgba(154,178,205,.22);border-radius:11px;background:rgba(255,255,255,.62);color:var(--text);padding:8px 10px;font:inherit;font-size:13px}.control-line input{min-width:min(360px,100%);flex:1}.filter-button,.clear-button,.show-more{border:1px solid rgba(88,116,143,.22);border-radius:999px;background:rgba(255,255,255,.46);color:var(--text-soft);padding:8px 11px;font:inherit;font-size:12px;cursor:pointer}.filter-button.active{border-color:rgba(232,198,107,.78);background:rgba(232,198,107,.15);color:var(--text)}.clear-button{font-weight:800}.clear-button:disabled{opacity:.45;cursor:default}.result-count{color:var(--text-muted);font-size:13px;margin-left:auto}.active-summary{color:var(--text-muted);font-size:13px}.browser-layout{display:grid;grid-template-columns:minmax(0,1fr) minmax(280px,340px);gap:14px;align-items:start}.session-table{display:grid;gap:8px;padding:14px;min-width:0}.session-row.head{display:none}.session-group{display:grid;gap:8px}.session-group-header{border:1px solid rgba(154,178,205,.16);border-radius:14px;padding:12px;background:rgba(248,250,252,.76)}.session-group-header h2{margin:0;font-size:16px}.session-group-header p{margin:4px 0 0;color:var(--text-muted);font-size:12px}.session-group-path{margin-top:6px;color:var(--text-dim);font-size:12px;overflow-wrap:anywhere}.session-row{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:12px;align-items:center;width:100%;min-width:0;text-align:left;color:inherit;border:1px solid rgba(154,178,205,.12);background:rgba(255,255,255,.26);border-radius:14px;padding:13px;font:inherit;cursor:pointer}.session-row>*{min-width:0}.session-row:hover,.session-row.selected{background:rgba(88,116,143,.08);border-color:rgba(118,169,224,.35)}.session-row-main{min-width:0;display:grid;gap:7px}.session-row-kicker,.session-row-top{display:flex;gap:7px;align-items:center;flex-wrap:wrap}.session-row-title{font-weight:850;font-size:15px;line-height:1.3;overflow-wrap:anywhere}.session-row-failure{color:#7a5b16;font-size:12px;line-height:1.35;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.session-row-meta{display:flex;flex-wrap:wrap;gap:8px 12px;color:var(--text-muted);font-size:12px;line-height:1.35}.session-row-source{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text-dim);font-size:12px}.session-row-action{white-space:nowrap;align-self:center}.provider-badge,.outcome-pill{display:inline-flex;align-items:center;width:max-content;max-width:100%;border:1px solid rgba(88,116,143,.22);border-radius:999px;padding:4px 7px;background:rgba(255,255,255,.42);font-size:12px;font-weight:800}.outcome-pill.success{border-color:rgba(126,226,139,.50);background:rgba(145,211,154,.16)}.outcome-pill.error{border-color:rgba(240,128,128,.62);background:rgba(240,128,128,.12)}.outcome-pill.info{border-color:rgba(88,116,143,.28);background:rgba(88,116,143,.08)}.open,.transport{display:inline-flex;align-items:center;justify-content:center;border:1px solid rgba(88,116,143,.24);border-radius:999px;color:var(--text);text-decoration:none;background:rgba(255,255,255,.56);padding:8px 12px;font-size:12px;font-weight:850}.preview-panel{position:sticky;top:76px;min-width:0}.preview-body{display:grid;gap:13px}.preview-title{font-size:18px;font-weight:850;line-height:1.2;overflow-wrap:anywhere}.preview-facts,.preview-stats{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.preview-stat{min-width:0;overflow:hidden}.preview-stat-value{display:block;overflow-wrap:anywhere;word-break:break-word;line-height:1.2}.preview-stat-label{display:block;margin-top:4px;color:var(--text-muted);font-size:12px}.preview-meta-block{margin-top:4px}.preview-meta-label{color:var(--text-muted);font-size:12px;text-transform:uppercase;letter-spacing:.04em}.preview-meta-value{overflow-wrap:anywhere;word-break:break-word;line-height:1.35}.safe-path-list li{overflow-wrap:anywhere;word-break:break-word}.muted{color:var(--text-muted);overflow-wrap:anywhere}.file-list{margin:0;padding-left:18px;color:var(--text-soft);font-size:12px}.copy-command{display:block;white-space:pre-wrap;overflow-wrap:anywhere;border:1px solid rgba(154,178,205,.16);border-radius:12px;padding:10px;background:#f8fafc;color:#334155}.empty-filter{display:none}.empty-filter.show{display:block}.pager{display:flex;justify-content:space-between;gap:10px;align-items:center;color:var(--text-muted);font-size:12px;padding:4px}.show-more[hidden]{display:none}@media(max-width:1280px){.browser-layout{grid-template-columns:1fr}.preview-panel{position:static;order:2}}@media(max-width:900px){.browser-hero{grid-template-columns:1fr}.summary-row{justify-content:flex-start}.result-count{margin-left:0}}@media(max-width:520px){.preview-facts,.preview-stats{grid-template-columns:1fr}}@media(max-width:760px){/* legacy avoided: grid-template-columns:minmax(92px,112px) minmax(158px,176px) minmax(72px,84px) */.session-browser .app-shell{padding:12px}.panel{padding:14px}.session-table{padding:10px}.session-row{grid-template-columns:1fr;gap:10px;padding:14px}.session-row-action{width:100%;padding:10px 12px}.session-row-source{display:none}.session-row-title{font-size:16px}.session-row-meta{gap:6px 10px}.session-row-failure{white-space:normal}.preview-panel{display:none}.control-line input{min-width:100%}.control-line select{flex:1;min-width:140px}.browser-layout{gap:10px}.snapshot-note,.active-summary,.result-count{font-size:12px}.pager{justify-content:flex-start;flex-wrap:wrap}}</style>
</head><body class="session-browser"><main class="app-shell"><section class="panel browser-hero"><div><p class="browser-kicker">Local investigation index</p><h1>Villani Flight Recorder</h1><p class="browser-subtitle">Local coding-agent sessions. Scan local transcripts, browse the investigation index, then open a static replay report for the selected session.</p></div><div class="summary-row"><span class="replay-chip"><b>${rows.length}</b> sessions</span><span class="replay-chip"><b>${failed}</b> failed</span><span class="replay-chip">${providers.map(label).join(", ") || "No providers"}</span><span class="replay-chip">Indexed ${escapeHtml(lastIndexed)}</span></div></section>
<section class="panel controls" aria-label="Session controls"><div class="control-line"><input id="search" placeholder="Search title, prompt, project, path, provider" /><span id="resultCount" class="result-count">Showing ${Math.min(rows.length, 100)} of ${rows.length} matching sessions · ${rows.length} indexed</span></div><div class="control-line" aria-label="Agent filters"><button class="filter-button active" data-agent="all">All Agents</button><button class="filter-button" data-agent="claude">Claude</button><button class="filter-button" data-agent="codex">Codex</button><button class="filter-button" data-agent="pi">Pi</button><button class="filter-button" data-agent="generic">Generic</button><select id="projectFilter" aria-label="Project filter"><option value="all">All projects</option>${projects.map((p) => `<option value="${escapeHtml(p.id)}">${escapeHtml(p.name)} · ${p.sessionCount} session${p.sessionCount === 1 ? "" : "s"}</option>`).join("")}</select><select id="groupBy" aria-label="Group sessions"><option value="none">Group by: None</option><option value="project">Group by: Project</option><option value="agent">Group by: Agent</option><option value="outcome">Group by: Outcome</option></select><select id="outcomeFilter" aria-label="Outcome filter"><option value="all">All Outcomes</option><option value="failed">Failed</option><option value="success">Success</option><option value="unknown">Unknown</option></select><select id="sort" aria-label="Sort sessions"><option value="updated-desc">Updated newest first</option><option value="updated-asc">Updated oldest first</option><option value="failed-desc">Failed commands</option><option value="provider-asc">Provider</option><option value="project-asc">Project</option></select><button id="clearFilters" class="clear-button" type="button">Clear filters</button></div><div id="activeFilterSummary" class="active-summary">No filters active</div><p class="snapshot-note">This browser was generated from the local index at ${escapeHtml(lastIndexed)}. Open Replay links point to replay HTML generated for this browser snapshot. Run vfr scan and vfr browse again to refresh.</p></section>
${empty ? `<div class="empty-state"><h2>No sessions indexed yet</h2><p>Run vfr scan to index local agent sessions.</p></div>` : `<section class="browser-layout"><section class="panel session-table" aria-label="Sessions"><div id="sessionRows"></div><div id="emptyFilter" class="empty-state empty-filter"><h2>No sessions match the current filters.</h2><p>Clear filters to show all indexed sessions.</p><button class="clear-button" type="button" data-clear-filters>Clear filters</button></div><div class="pager"><span id="pageCount"></span><button id="showMore" class="show-more" type="button">Show more</button></div></section><aside class="panel preview-panel" aria-live="polite"><div class="panel-head"><div><h2>Session preview</h2><p>Select a session to inspect it before opening the replay.</p></div></div><div id="preview" class="preview-body"></div></aside></section>`}
<script>const sessions=${safeJsonForScript(data)};const projects=${safeJsonForScript(projects)};let agent='all';const PAGE_SIZE=100;const state={outcome:'all',sort:'updated-desc',project:'all',group:'none',selected:sessions[0]?.id||'',shown:PAGE_SIZE};const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));const tone=o=>o==='success'?'success':o==='failed'?'error':'info';const labels={claude:'Claude',codex:'Codex',pi:'Pi',generic:'Generic',failed:'Failed',success:'Success',unknown:'Unknown','updated-asc':'Updated oldest first','failed-desc':'Failed commands','provider-asc':'Provider','project-asc':'Project'};const rowsEl=document.getElementById('sessionRows');const previewEl=document.getElementById('preview');const searchEl=document.getElementById('search');function filtered(){const q=(searchEl?.value||'').toLowerCase();const out=sessions.filter(s=>(agent==='all'||s.provider===agent)&&(state.outcome==='all'||s.outcome===state.outcome)&&(state.project==='all'||s.projectId===state.project||s.repoIds.includes(state.project)||s.repoRoots.includes(projects.find(p=>p.id===state.project)?.path||''))&&s.search.includes(q));out.sort((a,b)=>state.sort==='updated-asc'?String(a.updated).localeCompare(String(b.updated)):state.sort==='failed-desc'?b.failedCommandCount-a.failedCommandCount:state.sort==='provider-asc'?String(a.providerLabel).localeCompare(String(b.providerLabel)):state.sort==='project-asc'?String(a.project).localeCompare(String(b.project)):String(b.updated).localeCompare(String(a.updated)));return out}function filterParts(){const q=(searchEl?.value||'').trim();const parts=[];if(agent!=='all')parts.push(labels[agent]||agent);if(state.outcome!=='all')parts.push(labels[state.outcome]||state.outcome);if(state.project!=='all')parts.push('Project '+(projects.find(p=>p.id===state.project)?.name||state.project));if(state.group!=='none')parts.push('Group by '+state.group);if(q)parts.push('Search “'+q+'”');if(state.sort!=='updated-desc')parts.push('Sort '+(labels[state.sort]||state.sort));return parts}function resetPaging(){state.shown=PAGE_SIZE}function updateSummary(out){const parts=filterParts();const active=parts.length>0;const summary=document.getElementById('activeFilterSummary');if(summary)summary.textContent=active?'Filters active: '+parts.join(', '):'No filters active';document.getElementById('clearFilters')?.toggleAttribute('disabled',!active);document.querySelectorAll('[data-clear-filters]').forEach(b=>b.toggleAttribute('disabled',!active));document.getElementById('resultCount').textContent='Showing '+Math.min(state.shown,out.length)+' of '+out.length+' matching sessions · '+sessions.length+' indexed';document.getElementById('pageCount').textContent=out.length>Math.min(state.shown,out.length)?'Showing '+Math.min(state.shown,out.length)+' of '+out.length:'All matching sessions shown';document.getElementById('showMore')?.toggleAttribute('hidden',state.shown>=out.length)}function rowHtml(s){return '<article class="session-row session '+(s.id===state.selected?'selected':'')+'" data-id="'+esc(s.id)+'"><div class="session-row-main"><div class="session-row-kicker"><span class="provider-badge">'+esc(s.providerLabel)+'</span><span class="outcome-pill '+tone(s.outcome)+'">'+esc(s.outcome)+'</span></div><h3 class="session-row-title">'+esc(s.title)+'</h3>'+(s.failureSummary?'<div class="session-row-failure">'+esc(s.failureSummary)+'</div>':'')+'<div class="session-row-meta"><span class="session-project">'+esc(s.project)+'</span><span class="session-updated">'+esc(String(s.updated).slice(0,10)||'No date')+'</span><span class="session-events">'+s.eventCount+' events</span><span class="session-failed">'+s.failedCommandCount+' failed</span><span class="session-files">'+s.changedFileCount+' files</span>'+(s.tokenLabel?'<span class="session-tokens">'+esc(s.tokenLabel)+' tokens</span>':'')+'</div></div><div class="session-row-action"><a class="open session-action" href="'+esc(s.replayHref)+'" onclick="event.stopPropagation()">Open Replay</a></div></article>'}function groupMeta(items){const failed=items.filter(s=>s.outcome==='failed').length;const latest=items.map(s=>s.updated).sort().at(-1)||'';return items.length+' sessions · '+failed+' failed · latest '+String(latest).slice(0,10)}function renderSessionRows(page){if(state.group==='none')return page.map(rowHtml).join('');const groups=new Map();for(const s of page){const key=state.group==='project'?s.projectId:state.group==='agent'?s.provider:state.group==='outcome'?s.outcome:'all';if(!groups.has(key))groups.set(key,[]);groups.get(key).push(s)}return [...groups.values()].map(items=>{const first=items[0];const title=state.group==='project'?first.project:state.group==='agent'?first.providerLabel:first.outcome;const path=state.group==='project'&&first.projectPath?'<div class="session-group-path">'+esc(first.projectPath)+'</div>':'';return '<section class="session-group"><header class="session-group-header"><h2>'+esc(title)+'</h2><p>'+esc(groupMeta(items))+'</p>'+path+'</header>'+items.map(rowHtml).join('')+'</section>'}).join('')}function renderRows(){if(!rowsEl)return;const out=filtered();const page=out.slice(0,state.shown);updateSummary(out);document.getElementById('emptyFilter')?.classList.toggle('show',out.length===0);rowsEl.innerHTML=renderSessionRows(page);rowsEl.querySelectorAll('.session').forEach(r=>r.addEventListener('click',()=>{state.selected=r.dataset.id;renderRows();renderPreview()}));if(out.length&&!out.some(s=>s.id===state.selected)){state.selected=out[0].id;renderRows();renderPreview()}}function renderPreview(){if(!previewEl)return;const s=sessions.find(x=>x.id===state.selected)||sessions[0];if(!s){previewEl.innerHTML='<p class="muted">No session selected.</p>';return}const files=(s.changedFiles||[]).slice(0,6);previewEl.innerHTML='<div><span class="provider-badge">'+esc(s.providerLabel)+'</span> <span class="outcome-pill '+tone(s.outcome)+'">'+esc(s.outcome)+'</span></div><div class="preview-title">'+esc(s.title)+'</div><p class="muted">'+esc(s.project)+' · '+esc(s.updated)+'</p>'+(s.failureSummary?'<p class="muted"><b>'+esc(s.failureSummary)+'</b></p>':'')+'<div class="preview-facts preview-stats"><div class="meta-item preview-stat"><b class="preview-stat-value">'+s.eventCount+'</b><span class="preview-stat-label">events</span></div><div class="meta-item preview-stat"><b class="preview-stat-value">'+s.failedCommandCount+'</b><span class="preview-stat-label">failed commands</span></div><div class="meta-item preview-stat"><b class="preview-stat-value">'+s.changedFileCount+'</b><span class="preview-stat-label">changed files</span></div><div class="meta-item preview-stat"><b class="preview-stat-value">'+esc(s.tokenLabel||'—')+'</b><span class="preview-stat-label">'+(s.tokenLabel?'tokens':'tokens unavailable')+'</span></div></div><div class="preview-meta-block"><div class="preview-meta-label">Model</div><div class="preview-meta-value">'+esc(s.model||'Not captured')+'</div></div>'+(files.length?'<div><h3>Changed files preview</h3><ul class="file-list safe-path-list">'+files.map(f=>'<li>'+esc(f)+'</li>').join('')+'</ul></div>':'')+'<div><h3>Replay freshness</h3><p class="muted">Replay generated for this browser snapshot.</p><p class="muted">Replay path: '+esc(s.replayHref)+'</p></div><div class="preview-meta-block session-source"><div class="preview-meta-label">Source path</div><div class="preview-meta-value">'+esc(s.sourcePath)+'</div></div><code class="copy-command">'+esc(s.replayCommand)+'</code><a class="transport" href="'+esc(s.replayHref)+'">Open Replay</a>'}function clearFilters(){agent='all';state.outcome='all';state.sort='updated-desc';state.project='all';state.group='none';if(searchEl)searchEl.value='';document.getElementById('outcomeFilter').value='all';document.getElementById('sort').value='updated-desc';document.getElementById('projectFilter').value='all';document.getElementById('groupBy').value='none';document.querySelectorAll('[data-agent]').forEach(x=>x.classList.toggle('active',x.dataset.agent==='all'));resetPaging();const out=filtered();state.selected=out[0]?.id||'';renderRows();renderPreview()}document.querySelectorAll('[data-agent]').forEach(b=>b.onclick=()=>{agent=b.dataset.agent;document.querySelectorAll('[data-agent]').forEach(x=>x.classList.toggle('active',x===b));resetPaging();renderRows();renderPreview()});document.getElementById('projectFilter')?.addEventListener('change',e=>{state.project=e.target.value;resetPaging();renderRows();renderPreview()});document.getElementById('groupBy')?.addEventListener('change',e=>{state.group=e.target.value;resetPaging();renderRows()});document.getElementById('outcomeFilter')?.addEventListener('change',e=>{state.outcome=e.target.value;resetPaging();renderRows();renderPreview()});document.getElementById('sort')?.addEventListener('change',e=>{state.sort=e.target.value;resetPaging();renderRows()});searchEl?.addEventListener('input',()=>{resetPaging();renderRows();renderPreview()});document.getElementById('showMore')?.addEventListener('click',()=>{state.shown+=PAGE_SIZE;renderRows()});document.getElementById('clearFilters')?.addEventListener('click',clearFilters);document.querySelectorAll('[data-clear-filters]').forEach(b=>b.addEventListener('click',clearFilters));window.__VFR_SESSIONS__=sessions;window.__VFR_PROJECTS__=projects;window.__VFR_BROWSER_STATE__=state;renderRows();renderPreview();</script></main></body></html>`;
}
