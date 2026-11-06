/* Evaluation setup for completed workflows and saved plugin dialogues. */
(() => {
  const hash = new URLSearchParams(location.hash.slice(1));
  const token = hash.get('token') || '';
  window.evaluationToken = token;
  const url = new URL(location.href);
  for (const key of ['context', 'invocation']) {
    if (hash.has(key)) { url.searchParams.set(key, hash.get(key)); hash.delete(key); }
  }
  url.hash = hash.toString(); window.history.replaceState(null, '', url);
  const contextId = url.searchParams.get('context');
  // Terminal workflow links stay pinned to the completed run that opened them.
  // Dialogue contexts drop this pin only after their kind is known.
  let invocationId = url.searchParams.get('invocation');
  let runId = url.searchParams.get('evaluation');
  const $ = id => document.getElementById(id);
  const form = $('evaluation-form'), fields = $('eval-fields'), run = $('eval-run');
  const mode = $('eval-input-source'), fileInput = $('eval-trajectory');
  const previewButton = $('eval-view-trajectory'), previewPanel = $('eval-trajectory-preview');
  let previewVersion = 0;
  let config, selected = null, uploaded = null, inspected = null;
  let selectedFileSignature = null;
  let selectionVersion = 0;
  let busy = false, tracking = false, pollVersion = 0, initialized = false;
  let latestJob = null;
  let selectedOutputPath = '';
  let pendingSubmission = null;

  function setQuery(name, value) {
    const address = new URL(location.href);
    if (value) address.searchParams.set(name, value); else address.searchParams.delete(name);
    window.history.replaceState(null, '', address);
  }
  async function api(path, options = {}) {
    const response = await fetch(path, {...options, signal: AbortSignal.timeout(15000), headers: {
      'Content-Type': 'application/json', Authorization: `Bearer ${token}`,
    }});
    const value = await response.json();
    if (!response.ok) { const error = new Error(value.error || `Request failed (${response.status})`); error.httpStatus = response.status; throw error; }
    return value;
  }
  function queryContext() { return contextId ? '?context=' + encodeURIComponent(contextId) : ''; }
  function configPath() {
    const params = new URLSearchParams();
    if (contextId) params.set('context', contextId);
    if (invocationId) params.set('invocation', invocationId);
    return '/api/config?' + params;
  }
  // Results visualization: run summary, invocation timeline, team structure, and step detail.
  // Everything shown is read from the trajectory; findings come from the selected evaluation.
  // ---- Views: "Set up & run" and "Review", switched by the header tabs ----
  let currentView = 'setup';
  const reviewOpen = () => currentView === 'review';
  function updateReviewEmpty() {
    const empty = !runId && !inspected;
    $('view-review').dataset.empty = String(empty);
    $('review-empty').hidden = !empty;
    // Without a selected evaluation there is no status or output to show.
    $('eval-card').hidden = !runId;
  }
  function showView(view, {focus = false} = {}) {
    currentView = view === 'review' ? 'review' : 'setup';
    for (const tab of document.querySelectorAll('[role=tab]')) {
      const on = tab.dataset.view === currentView;
      tab.setAttribute('aria-selected', String(on)); tab.tabIndex = on ? 0 : -1;
      if (on && focus) tab.focus();
    }
    document.body.dataset.view = currentView;
    $('view-setup').hidden = currentView !== 'setup';
    $('view-review').hidden = currentView !== 'review';
    setQuery('view', reviewOpen() ? 'review' : null);
    updateReviewEmpty();
    window.scrollTo({top: 0});
    if (reviewOpen()) loadViz();
  }
  for (const control of document.querySelectorAll('[data-view]')) {
    control.addEventListener('click', () => showView(control.dataset.view));
  }
  document.querySelector('[role=tablist]').addEventListener('keydown', event => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    showView(['ArrowRight', 'End'].includes(event.key) ? 'review' : 'setup', {focus: true});
  });
  // The docked detail panel sits just below the sticky header, whose height varies.
  const appHeader = document.querySelector('.app-header');
  new ResizeObserver(() => document.documentElement.style.setProperty('--header-h', `${appHeader.offsetHeight}px`)).observe(appHeader);
  // ---- Help side panel (data model and detection methods) ----
  let helpReturn = null;
  function openHelp(sectionId, trigger) {
    helpReturn = trigger || document.activeElement;
    $('help-drawer').hidden = false; $('help-backdrop').hidden = false;
    document.body.classList.add('drawer-open');
    // Show only the topic that was asked for; its heading becomes the panel title.
    const body = $('help-body');
    for (const section of body.children) section.hidden = section.id !== sectionId;
    const heading = $(sectionId)?.querySelector('h3');
    $('help-title').textContent = heading ? heading.textContent : 'Help';
    body.scrollTop = 0;
    $('help-close').focus();
  }
  function closeHelp() {
    if ($('help-drawer').hidden) return;
    $('help-drawer').hidden = true; $('help-backdrop').hidden = true;
    document.body.classList.remove('drawer-open');
    helpReturn?.focus?.();
  }
  for (const link of document.querySelectorAll('[data-help]')) {
    link.addEventListener('click', event => { event.preventDefault(); openHelp(link.dataset.help, link); });
  }
  $('help-close').addEventListener('click', closeHelp);
  $('help-backdrop').addEventListener('click', closeHelp);
  document.addEventListener('keydown', event => {
    if (event.key !== 'Escape' || $('help-drawer').hidden) return;
    event.preventDefault(); closeHelp();
  });
  // Keep keyboard focus inside the open panel.
  $('help-drawer').addEventListener('keydown', event => {
    if (event.key !== 'Tab') return;
    const items = [...$('help-drawer').querySelectorAll('button, summary, a[href], [tabindex="0"]')].filter(el => el.offsetParent);
    const first = items[0], last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  });
  function setContextTrajectory(info, pending = false) {
    const value = $('ctx-trajectory');
    const full = info ? (info.source_path || info.name || '') : '';
    value.textContent = pending ? 'Checking…' : full || 'None selected';
    value.title = full;
  }
  let vizSource = 'current';            // 'current' input or the selected 'evaluation' snapshot
  let vizVersion = 0, vizKey = null, vizPending = null;
  let vizRun = null, vizFindings = null, vizSelection = null, vizHighlight = null;
  let vizScale = 'equal';
  const svgNS = 'http://www.w3.org/2000/svg';
  const AGENT_COLORS = ['#4e79a7', '#59a14f', '#b07aa1', '#76b7b2', '#d4a72c', '#9c755f', '#e17ca0', '#7f8b99'];
  const CATEGORY_COLORS = ['#145cc5', '#8743a7', '#16736b', '#ae531a', '#ab365f', '#5366a7', '#677423'];
  const KIND_LABELS = {input: 'Input', model: 'Model call', tool: 'Tool activity', 'tool-request': 'Tool request',
    'tool-result': 'Tool result', reflection: 'Tool reflection', output: 'Output', telemetry: 'Telemetry', other: 'Recorded step'};
  const ARCH_LABELS = {sequential: 'Sequential', shared_context: 'Shared context', looping: 'Looping', hierarchical: 'Hierarchical', single: 'Single agent'};

  function graphElement(tag, attributes = {}, text) {
    const element = document.createElementNS(svgNS, tag);
    for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, value);
    if (text !== undefined) element.textContent = text;
    return element;
  }
  function node(tag, text, className) {
    const element = document.createElement(tag);
    if (text != null) element.textContent = text;
    if (className) element.className = className;
    return element;
  }
  function hashColor(label, palette) {
    let hash = 0;
    for (const char of String(label)) hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
    return palette[hash % palette.length];
  }
  const categoryColor = label => hashColor(label, CATEGORY_COLORS);
  const pretty = name => String(name ?? '').replaceAll('_', ' ');
  const plural = (n, label) => `${n} ${label}${n === 1 ? '' : 's'}`;
  const number = n => Number(n).toLocaleString();
  function seconds(value) {
    const s = Number(value);
    if (!Number.isFinite(s)) return null;
    if (s < 60) return `${s < 10 ? s.toFixed(1) : Math.round(s)} s`;
    return `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`;
  }
  function timeOf(value) {
    if (typeof value !== 'string') return NaN;
    // Trim sub-millisecond digits so every browser parses the timestamp.
    return Date.parse(value.replace(/(\.\d{3})\d+/, '$1'));
  }
  function readableText(value) {
    if (value == null || value === '') return '';
    if (typeof value === 'string') {
      const trimmed = value.trim();
      if (trimmed.startsWith('[')) {
        try {
          const parsed = JSON.parse(trimmed);
          if (Array.isArray(parsed) && parsed.every(item => typeof item === 'string')) return parsed.map(s => s.trim()).join('\n\n');
        } catch (_) { /* Not JSON: show as recorded. */ }
      }
      return trimmed;
    }
    return JSON.stringify(value, null, 2);
  }
  const pointerToken = key => String(key).replaceAll('~', '~0').replaceAll('/', '~1');
  function compareStepIds(a, b) {
    const x = String(a).split('.').map(Number), y = String(b).split('.').map(Number);
    for (let i = 0; i < Math.max(x.length, y.length); i++) {
      const d = (x[i] ?? -1) - (y[i] ?? -1);
      if (d) return Number.isNaN(d) ? String(a).localeCompare(String(b)) : d;
    }
    return 0;
  }
  function stepKind(block) {
    if (/^tool_llm_request/.test(block)) return 'reflection';
    if (/^llm_request/.test(block)) return 'model';
    if (/^tool_execution_activity/.test(block)) return 'tool';
    return {tool_calling: 'tool-request', tool_call_result: 'tool-result', task: 'input', query: 'input',
      Query: 'input', result: 'output', OTel_traces: 'telemetry'}[block] || 'other';
  }

  // ---- Trajectory model ----
  function collectSteps(execution, base, byPath) {
    const top = [];
    const make = (block, value, path, children = []) => ({id: String(value.step_id), block, kind: stepKind(block),
      value, path, record: byPath.get(path), children, nested: String(value.step_id).includes('.')});
    function nested(value, path, block, out) {
      const items = Array.isArray(value) ? value.map((v, i) => [block, v, `${path}/${i}`])
        : Object.entries(value).map(([k, v]) => [k, v, `${path}/${pointerToken(k)}`]);
      for (const [name, child, childPath] of items) {
        if (!child || typeof child !== 'object' || name === 'OTel_traces') continue;
        if (!Array.isArray(child) && child.step_id != null) out.push(make(name, child, childPath));
        nested(child, childPath, name, out);
      }
    }
    for (const [key, value] of Object.entries(execution)) {
      if (!value || typeof value !== 'object' || key === 'agent_metadata') continue;
      const path = `${base}/${pointerToken(key)}`;
      const children = [];
      // Telemetry spans are kept whole; only the block's own step is shown.
      if (key !== 'OTel_traces') nested(value, path, key, children);
      children.sort((a, b) => compareStepIds(a.id, b.id));
      if (!Array.isArray(value) && value.step_id != null) top.push(make(key, value, path, children));
      else top.push(...children);
    }
    return top.sort((a, b) => compareStepIds(a.id, b.id));
  }
  function buildRun(trajectory, catalog) {
    const meta = trajectory.trace_metadata || {};
    const arch = meta.system_architecture || (trajectory.agent_metadata ? 'single' : 'unknown');
    let entries = [];
    if (trajectory.agents && typeof trajectory.agents === 'object') {
      entries = Object.entries(trajectory.agents).map(([key, wrapper]) => {
        const inner = wrapper && typeof wrapper.execution === 'object' ? wrapper.execution : null;
        return {key, wrapper: wrapper || {}, execution: inner || wrapper || {},
          path: `/agents/${pointerToken(key)}${inner ? '/execution' : ''}`};
      });
    } else if (trajectory.agent_metadata) entries = [{key: 'agent', wrapper: trajectory, execution: trajectory, path: ''}];
    if (arch === 'hierarchical') entries.sort((a, b) => Number(a.wrapper.sequence) - Number(b.wrapper.sequence));
    const byPath = new Map(catalog.map(record => [record.path, record]));
    const setup = entries.find(e => !e.execution.agent_metadata && (e.execution.initialization_metadata || e.execution.orchestrator_setup));
    const lanes = [], counts = new Map();
    for (const entry of entries) {
      const m = entry.execution.agent_metadata;
      if (!m || typeof m !== 'object') continue;
      const name = m.agent_name || entry.wrapper.agent_name || entry.key;
      counts.set(name, (counts.get(name) || 0) + 1);
      lanes.push({index: lanes.length, name, attempt: counts.get(name), meta: m, execution: entry.execution,
        wrapper: entry.wrapper, path: entry.path, steps: collectSteps(entry.execution, entry.path, byPath)});
    }
    for (const lane of lanes) lane.attempts = counts.get(lane.name);
    // Steps outside any agent execution stay visible in their own lane.
    const placed = new Set(lanes.flatMap(lane => lane.steps.flatMap(s => [s, ...s.children])).map(s => s.path));
    const extra = catalog.filter(record => !placed.has(record.path)).map(record => {
      let value = {};
      try { value = JSON.parse(record.json); } catch (_) { /* Keep the original text in the JSON view. */ }
      return {id: record.step_id, block: record.block_name, kind: stepKind(record.block_name), value, path: record.path,
        record, children: [], nested: record.step_id.includes('.')};
    }).sort((a, b) => compareStepIds(a.id, b.id));
    if (extra.length) lanes.push({index: lanes.length, name: 'Workflow-level steps', pseudo: true, attempt: 1, attempts: 1,
      meta: {}, execution: {}, wrapper: {}, path: '', steps: extra});
    const provided = trajectory.team_configuration?.provided_agent_names;
    const roster = Array.isArray(provided) ? provided.filter(n => typeof n === 'string') : null;
    const agentNames = [...new Set([...lanes.filter(l => !l.pseudo).map(l => l.name), ...(roster || [])])];
    const colors = new Map(agentNames.map((name, i) => [name, AGENT_COLORS[i % AGENT_COLORS.length]]));
    const allSteps = lanes.flatMap(lane => lane.steps.flatMap(step => [step, ...step.children]).map(step => ({...step, lane})));
    const declared = meta.agent_sequence, recorded = lanes.filter(l => !l.pseudo).map(l => l.name);
    const sequenceMismatch = Array.isArray(declared) && JSON.stringify(declared) !== JSON.stringify(recorded);
    const start = timeOf(meta.timestamp_start);
    return {trajectory, meta, arch, lanes, roster, colors, setup, allSteps, sequenceMismatch, start,
      summary: trajectory.workflow_summary || null, team: trajectory.team_configuration || null};
  }

  // ---- Findings overlay ----
  function overlayActive() {
    if (!vizRun || !vizFindings) return false;
    if (vizRun.evaluationId) return vizRun.evaluationId === vizFindings.jobId;
    return !!vizFindings.sha256 && vizFindings.sha256 === vizRun.sha256;
  }
  const findingsAt = id => overlayActive() ? (vizFindings.steps.get(id)?.failures || []) : [];
  // ---- Taxonomy: place each finding in its category, sub-category and failure type ----
  const TAXONOMY_LABELS = {ours: 'RAMS failures taxonomy', mast: 'MAST failures taxonomy'};
  const LEVEL_NAMES = {1: ['Category'], 2: ['Category', 'Failure type'], 3: ['Category', 'Sub-category', 'Failure type']};
  function buildTaxonomy(key) {
    const roots = config?.taxonomies?.[key];
    if (!key || !Array.isArray(roots) || !roots.length) return null;
    const index = new Map();
    let depth = 1;
    const walk = (item, path) => {
      const next = [...path, item]; index.set(item.code, next); depth = Math.max(depth, next.length);
      for (const child of item.children || []) walk(child, next);
    };
    for (const root of roots) walk(root, []);
    const levels = LEVEL_NAMES[depth] || [...LEVEL_NAMES[3], ...Array(depth - 3).fill('Failure type')];
    return {key, label: TAXONOMY_LABELS[key] || key, roots, index, depth, levels};
  }
  // "5.1. Protocol Failures" -> {code: '5.1', name: 'Protocol Failures'}
  function parseLabel(text) {
    const match = /^\s*(\d+(?:\.\d+)*)\.?\s+(.+)$/.exec(String(text || ''));
    return match ? {code: match[1], name: match[2].trim().replace(/\.$/, '')} : null;
  }
  // The finding's path from category down to failure type, with definitions where known.
  function classify(failure, taxonomy = vizFindings?.taxonomy) {
    if (!taxonomy) return null;
    const code = String(failure.code || '').replace(/\.$/, '');
    const known = code && taxonomy.index.get(code);
    if (known) return known.map((item, i) => ({level: taxonomy.levels[i], code: item.code, name: item.name, description: item.description}));
    // Code missing or unknown: fall back to the labels the judge returned.
    const path = [];
    for (const part of [parseLabel(failure.category), parseLabel(failure.sub_category)]) {
      if (part && !path.some(p => p.code === part.code)) path.push({...part, level: taxonomy.levels[path.length], description: ''});
    }
    if (failure.failure_type && !path.some(p => p.name === failure.failure_type)) {
      path.push({level: 'Failure type', code, name: failure.failure_type, description: ''});
    }
    return path.length ? path : null;
  }
  const codeLabel = item => item.code ? `${item.code} ${item.name}` : item.name;
  function matchesHighlight(failure) {
    if (!vizHighlight) return true;
    if (vizHighlight.kind === 'code') {
      const code = String(failure.code || '');
      return code === vizHighlight.code || code.startsWith(`${vizHighlight.code}.`);
    }
    if (vizHighlight.kind === 'group') return vizFindings.groupName(failure) === vizHighlight.label;
    return (failure.failure_type || 'Unlabelled failure') === vizHighlight.label;
  }
  const highlightedAt = id => findingsAt(id).some(matchesHighlight);
  function laneFindings(lane) {
    return lane.steps.flatMap(s => [s, ...s.children]).reduce((n, s) => n + findingsAt(s.id).length, 0);
  }

  // ---- Source and loading ----
  // Does the evaluated snapshot differ from the trajectory selected in “Set up & run”?
  // true / false when both checksums are known, null when it cannot be told.
  function snapshotDiffers() {
    if (!runId || !inspected || latestJob?.id !== runId) return false;
    const evaluated = latestJob.input?.sha256, selected = inspected.sha256;
    return evaluated && selected ? evaluated !== selected : null;
  }
  function updateSourceControls() {
    const differs = snapshotDiffers();
    // Identical files: the snapshot is the same trajectory and also carries the findings.
    if (differs === false && runId && latestJob?.id === runId) vizSource = 'evaluation';
    if (vizSource === 'evaluation' && !runId && inspected) vizSource = 'current';
    if (vizSource === 'current' && !inspected && runId) vizSource = 'evaluation';
    const button = $('viz-source-switch');
    button.hidden = differs === false;
    button.textContent = vizSource === 'evaluation' ? 'View the selected trajectory (without findings)' : 'Back to the evaluated trajectory';
  }
  function clearViz(message) {
    vizVersion++; vizKey = null; vizPending = null; vizRun = null; vizSelection = null; vizHighlight = null;
    $('viz-content').hidden = true;
    $('viz-status').textContent = message || 'Select a trajectory in “Set up & run” or one of the saved evaluations in the header to view the run.';
    $('viz-source').textContent = '';
    renderOverview();
    updateSourceControls();
  }
  $('viz-source-switch').addEventListener('click', () => {
    vizSource = vizSource === 'evaluation' ? 'current' : 'evaluation';
    clearViz(); loadViz();
  });
  for (const button of document.querySelectorAll('[data-viz-scale]')) {
    button.addEventListener('click', () => {
      vizScale = button.dataset.vizScale;
      for (const b of document.querySelectorAll('[data-viz-scale]')) b.setAttribute('aria-pressed', String(b === button));
      if (vizRun) { renderTimeline(); markSelection(); }
    });
  }
  function loadViz(force = false) {
    if (!initialized || !reviewOpen()) return Promise.resolve();
    updateSourceControls();
    const evaluationId = vizSource === 'evaluation' ? runId : null;
    if (!evaluationId && !inspected) {
      $('viz-content').hidden = true;
      $('viz-status').textContent = 'Select a trajectory in “Set up & run” or one of the saved evaluations in the header to view the run.';
      return Promise.resolve();
    }
    const key = evaluationId ? `evaluation:${evaluationId}` : `${selectionVersion}:${inspected.sha256}`;
    if (!force && vizKey === key) return vizPending || Promise.resolve();
    vizKey = key;
    const version = ++vizVersion, selection = selectionVersion, input = inspected;
    $('viz-content').hidden = true;
    $('viz-status').textContent = 'Loading the recorded run…';
    vizPending = (async () => {
      try {
        const response = evaluationId ? await api(`/api/runs/${evaluationId}/trajectory`)
          : await api('/api/trajectory', {method: 'POST', body: JSON.stringify({...sourcePayload(), expected_sha256: input.sha256})});
        if (version !== vizVersion || (!evaluationId && selection !== selectionVersion)) return;
        const runModel = buildRun(JSON.parse(response.trajectory_json), response.steps || []);
        if (!runModel.lanes.length && !runModel.setup) throw new Error('No agent records are available in this trajectory');
        runModel.evaluationId = evaluationId;
        runModel.sha256 = evaluationId ? latestJob?.input?.sha256 : input.sha256;
        runModel.sourceName = evaluationId ? null : (input.source_path || input.name);
        vizRun = runModel; vizSelection = null; vizHighlight = null;
        renderViz();
        $('viz-status').textContent = '';
      } catch (error) {
        if (version !== vizVersion) return;
        $('viz-content').hidden = true;
        $('viz-status').textContent = `Run view unavailable: ${error.message}. Close and reopen Results Visualization to retry.`;
        vizKey = null;
      }
    })();
    return vizPending;
  }

  // ---- Rendering ----
  function renderViz() {
    const differs = snapshotDiffers();
    $('viz-source').textContent = vizRun.evaluationId
      ? (differs ? 'This evaluation used a different trajectory than the one selected in “Set up & run” (another file, or an earlier version of it). Shown here is the trajectory the evaluation used, so the findings line up with its steps.'
        : differs === null ? `Showing the trajectory saved with evaluation ${vizRun.evaluationId}. It may differ from the one selected in “Set up & run”.`
        : '')
      : `Showing the trajectory selected in “Set up & run”: ${vizRun.sourceName}.` + (runId && !overlayActive()
        ? (differs === null ? ' Findings are not shown because it cannot be confirmed that the selected evaluation used this trajectory.'
          : ' Findings are not shown because the selected evaluation used a different trajectory.') : '');
    renderSummary(); renderOverview(); renderStructure(); renderTimeline();
    $('viz-content').hidden = false;
    if (!vizSelection) {
      const flagged = overlayActive() && vizRun.allSteps.find(step => findingsAt(step.id).length);
      vizSelection = flagged ? {kind: 'step', path: flagged.path}
        : vizRun.lanes.length ? {kind: 'invocation', index: 0} : {kind: 'orchestrator'};
    }
    markSelection(); renderDetail();
  }
  function renderSummary() {
    const {meta, arch, lanes, roster, team} = vizRun;
    const agents = lanes.filter(l => !l.pseudo);
    $('run-pattern').textContent = (ARCH_LABELS[arch] || 'Architecture unspecified') +
      (team?.team ? ` · ${team.team}` : '');
    const items = [
      ['Agents', roster ? `${new Set(agents.map(l => l.name)).size} of ${roster.length} invoked` : String(new Set(agents.map(l => l.name)).size)],
      ['Invocations', String(agents.length)],
      ['Steps', String(vizRun.allSteps.length)],
      ...evaluationFacts(),
      ['Scenario', [meta.UC, pretty(meta.scenario || ''), meta.research_topic].filter(Boolean).join(' · ')],
    ];
    const list = $('run-facts'); list.replaceChildren();
    for (const [label, value] of items) {
      if (!value) continue;
      const item = node('div', null, 'fact');
      item.append(node('dt', label), node('dd', value));
      list.append(item);
    }
    const notes = [
      'Each lane is one recorded agent invocation, in recorded order. Repeated invocations of the same agent appear as separate attempts.',
      'Lane order shows the recorded sequence, not verified data dependencies.',
      roster ? 'The team roster comes from team_configuration; agents that were available but not invoked are shown in the workflow structure.'
        : 'This file lists the agents that ran; agents that were registered but never invoked cannot be shown.',
      arch === 'shared_context' ? 'Agents share one conversation history instead of passing outputs to each other; turn numbers show the recorded order.' : null,
      vizRun.sequenceMismatch ? 'The declared agent_sequence differs from the recorded invocations; lanes follow the recorded invocations.' : null,
      !['sequential', 'shared_context', 'looping', 'hierarchical', 'single'].includes(arch) ? 'The architecture label is not recognized; invocations are shown in recorded order.' : null,
      'Agent statuses are execution statuses recorded by the framework, not evaluation findings.',
      evaluationFacts().length ? 'Evaluation duration and tokens describe the selected evaluation’s judge, not the agent run.' : null,
    ].filter(Boolean);
    const notesList = $('run-notes-list'); notesList.replaceChildren(...notes.map(text => node('li', text)));
  }
  // Cost of the selected evaluation (the judge), not of the recorded agent run.
  function evaluationFacts() {
    const job = latestJob, facts = [];
    if (!job || job.status === 'running') return facts;
    // Show only what was recorded; older and terminal evaluations may lack either value.
    const elapsed = Number(job.finished_at) - Number(job.created_at);
    if (Number.isFinite(elapsed) && elapsed >= 0) facts.push(['Evaluation duration', seconds(elapsed)]);
    const usage = job.judge_usage;
    if (usage?.calls_with_usage) {
      let tokens = `${number(usage.total_tokens)} (${number(usage.input_tokens)} in / ${number(usage.output_tokens)} out)`;
      if (usage.calls_with_usage < usage.calls) tokens += ` · ${usage.calls_with_usage} of ${usage.calls} calls reported usage`;
      facts.push(['Evaluation tokens', tokens]);
    }
    return facts;
  }
  let showAllRecurring = false;
  function renderOverview() {
    const panel = $('eval-visualization');
    panel.hidden = !vizFindings;
    if (!vizFindings) return;
    const active = overlayActive();
    $('eval-overlay-note').textContent = active ? '' : vizRun
      ? 'These findings belong to a different trajectory than the one shown below, so they are not placed on the timeline.' : '';
    const chart = $('eval-category-chart'); chart.replaceChildren();
    const total = vizFindings.failures.length;
    $('eval-overview-count').textContent = `· ${plural(total, 'failure')}`;
    const toggle = (kind, label, code) => {
      const same = vizHighlight?.kind === kind && vizHighlight.label === label;
      vizHighlight = same ? null : {kind, label, code};
      renderOverview(); renderTimeline(); markSelection();
    };
    const taxonomy = vizFindings.taxonomy;
    $('eval-taxonomy-link').hidden = !taxonomy;
    if (taxonomy) {
      $('eval-breakdown-title').textContent = `Failures in the ${taxonomy.label}`;
      $('eval-breakdown-help').textContent = `All ${taxonomy.roots.length} categories are listed. Expand one (▸) to see its ` +
        `${taxonomy.depth > 2 ? 'sub-categories and failure types' : 'failure types'}; select a row to highlight its steps in the timeline.`;
      renderTaxonomyTree(chart, taxonomy, active, total, toggle);
    } else {
      $('eval-breakdown-help').textContent = 'Zero-shot findings use free-text labels, not taxonomy codes. Select a row to highlight its steps in the timeline.';
      for (const [label, count] of vizFindings.groups) {
        const row = node('button', null, 'failure-category'); row.type = 'button';
        row.disabled = !active;
        row.setAttribute('aria-pressed', String(vizHighlight?.kind === 'group' && vizHighlight.label === label));
        row.append(node('span', label), node('strong', String(count)));
        const track = node('span', null, 'failure-bar');
        const fill = node('span'); fill.style.width = `${100 * count / total}%`; fill.style.backgroundColor = categoryColor(label);
        track.append(fill); row.append(track);
        row.addEventListener('click', () => toggle('group', label));
        chart.append(row);
      }
      if (!vizFindings.groups.length) chart.append(node('p', 'No failure occurrences to chart.', 'help'));
    }
    const recurring = $('eval-recurring'); recurring.replaceChildren();
    $('eval-recurring-section').hidden = !vizFindings.recurring.length;
    const shown = showAllRecurring ? vizFindings.recurring : vizFindings.recurring.slice(0, 8);
    for (const item of shown) {
      const entry = node('button', null, 'recurring-item'); entry.type = 'button'; entry.disabled = !active;
      entry.setAttribute('aria-pressed', String(vizHighlight?.kind === item.kind && vizHighlight.label === item.label));
      entry.append(node('span', item.code, 'code'), node('span', item.name, 'recurring-name'), node('span', plural(item.ids.length, 'step'), 'recurring-count'));
      entry.title = `Steps ${item.ids.join(', ')}`;
      entry.addEventListener('click', () => toggle(item.kind, item.label, item.code));
      recurring.append(entry);
    }
    if (vizFindings.recurring.length > 8) {
      const more = node('button', showAllRecurring ? 'Show fewer' : `Show all ${vizFindings.recurring.length}`, 'recurring-more'); more.type = 'button';
      more.addEventListener('click', () => { showAllRecurring = !showAllRecurring; renderOverview(); });
      recurring.append(more);
    }
    renderTaxonomyReference();
    $('viz-highlight').hidden = !vizHighlight;
    $('viz-highlight-label').textContent = vizHighlight ? `Highlighting steps with “${vizHighlight.label}”` : '';
  }
  // Tree of categories (all of them); each expands to its reported sub-categories and failure types.
  const expandedCategories = new Set();
  function renderTaxonomyTree(chart, taxonomy, active, total, toggle) {
    const counts = vizFindings.counts;
    const addRow = (item, depth, root, parent) => {
      const count = counts.get(item.code) || 0;
      const line = node('div', null, 'taxonomy-line');
      line.dataset.depth = String(depth);
      const children = (item.children || []).filter(child => counts.get(child.code));
      if (depth === 0) {
        const expander = node('button', children.length ? (expandedCategories.has(item.code) ? '▾' : '▸') : '', 'taxonomy-expander');
        expander.type = 'button'; expander.disabled = !children.length;
        expander.setAttribute('aria-expanded', String(expandedCategories.has(item.code)));
        expander.setAttribute('aria-label', `${expandedCategories.has(item.code) ? 'Collapse' : 'Expand'} ${codeLabel(item)}`);
        expander.addEventListener('click', () => {
          if (expandedCategories.has(item.code)) expandedCategories.delete(item.code); else expandedCategories.add(item.code);
          renderOverview();
        });
        line.append(expander);
      }
      const row = node('button', null, 'failure-category taxonomy-row'); row.type = 'button';
      row.dataset.depth = String(depth); row.dataset.empty = String(!count);
      row.disabled = !active || !count;
      const label = codeLabel(item);
      row.setAttribute('aria-pressed', String(vizHighlight?.kind === 'code' && vizHighlight.code === item.code));
      const name = node('span', null, 'taxonomy-name');
      name.append(node('span', item.code, 'code'), node('span', item.name));
      row.append(name, node('strong', count ? String(count) : 'none'));
      if (count) {
        const track = node('span', null, 'failure-bar');
        const fill = node('span'); fill.style.width = `${100 * count / total}%`; fill.style.backgroundColor = categoryColor(codeLabel(root));
        track.append(fill); row.append(track);
      }
      row.title = [`${taxonomy.levels[depth]} ${label}`, item.description].filter(Boolean).join(' — ');
      row.setAttribute('aria-label', `${taxonomy.levels[depth]} ${label}: ${count ? plural(count, 'failure') : 'no failures reported'}`);
      row.addEventListener('click', () => toggle('code', label, item.code));
      line.append(row); chart.append(line);
      const open = depth > 0 || expandedCategories.has(item.code);
      if (count && open) for (const child of children) addRow(child, depth + 1, root);
    };
    for (const root of taxonomy.roots) addRow(root, 0, root);
    if (vizFindings.unmapped) {
      chart.append(node('p', `${plural(vizFindings.unmapped, 'failure')} could not be placed in the taxonomy (missing or unknown code).`, 'failure-warning'));
    }
  }
  // Full taxonomy in the help panel, with the reported codes marked.
  function renderTaxonomyReference() {
    const taxonomy = vizFindings?.taxonomy, tree = $('help-taxonomy-tree');
    tree.replaceChildren();
    if (!taxonomy) return;
    $('help-taxonomy-heading').textContent = taxonomy.label;
    $('help-taxonomy-intro').textContent = `${taxonomy.roots.length} categories · ${taxonomy.levels.join(' → ')}. ` +
      'Failure types reported in the selected evaluation are marked.';
    const counts = vizFindings.counts;
    const build = (item, depth) => {
      const count = counts.get(item.code) || 0;
      const leaf = !(item.children || []).length;
      const head = node('span', null, 'taxonomy-ref-head');
      head.append(node('span', item.code, 'code'), node('span', item.name, 'taxonomy-ref-name'));
      if (count) head.append(node('span', `${count} reported`, 'reported-tag'));
      const description = item.description ? node('p', item.description, 'taxonomy-ref-desc') : null;
      if (leaf) {
        const entry = node('div', null, count ? 'taxonomy-ref-leaf reported' : 'taxonomy-ref-leaf');
        entry.append(head); if (description) entry.append(description);
        return entry;
      }
      const group = node('details', null, `taxonomy-ref-group depth-${depth}`);
      group.open = count > 0;
      const summary = node('summary'); summary.append(head); group.append(summary);
      if (description) group.append(description);
      for (const child of item.children) group.append(build(child, depth + 1));
      return group;
    };
    for (const root of taxonomy.roots) tree.append(build(root, 0));
  }
  $('viz-highlight-clear').addEventListener('click', () => { vizHighlight = null; renderOverview(); renderTimeline(); markSelection(); });

  function structureSvg(width, height) {
    const svg = graphElement('svg', {viewBox: `0 0 ${width} ${height}`, role: 'group', 'aria-label': 'Workflow structure'});
    svg.style.maxWidth = `${width}px`;
    const defs = graphElement('defs'), marker = graphElement('marker', {id: 'structure-arrow', viewBox: '0 0 10 10', refX: 9, refY: 5,
      markerWidth: 7, markerHeight: 7, orient: 'auto-start-reverse'});
    marker.append(graphElement('path', {d: 'M 0 0 L 10 5 L 0 10 z', class: 'edge-head'}));
    defs.append(marker); svg.append(defs);
    return svg;
  }
  // One agent box, shared by every architecture so the views stay consistent.
  function structureNode(x, y, {name, title, sub, flagged, idle, attrs, onSelect}) {
    const g = graphElement('g', {class: `structure-node${idle ? ' idle' : ''}`, role: 'button', tabindex: 0, ...attrs});
    g.append(graphElement('rect', {x: x - 90, y, width: 180, height: 62, rx: 10}));
    g.append(graphElement('rect', {x: x - 90, y, width: 6, height: 62, rx: 3, fill: vizRun.colors.get(name), class: 'swatch'}));
    // Centre text in the area right of the colour bar; the full name is in the tooltip.
    g.append(graphElement('text', {x: x + 3, y: y + 26, 'text-anchor': 'middle', class: 'node-title'}, title.length > 19 ? title.slice(0, 18) + '…' : title));
    g.append(graphElement('text', {x: x + 3, y: y + 46, 'text-anchor': 'middle', class: flagged ? 'node-sub flagged' : 'node-sub'}, sub));
    g.append(graphElement('title', {}, name));
    g.addEventListener('click', onSelect);
    g.addEventListener('keydown', e => { if (['Enter', ' '].includes(e.key)) { e.preventDefault(); onSelect(); } });
    return g;
  }
  function renderStructure() {
    const section = $('run-structure'), box = $('structure-diagram');
    box.replaceChildren();
    const lanes = vizRun.lanes.filter(l => !l.pseudo);
    section.hidden = !lanes.length;
    if (section.hidden) return;
    const chain = ['sequential', 'shared_context', 'looping'].includes(vizRun.arch);
    $('run-structure-help').textContent = vizRun.arch === 'hierarchical'
      ? 'The orchestrator delegates to the team. Edge labels give the invocation numbers in the run timeline. Select the orchestrator to see its task and plan.'
      : vizRun.arch === 'shared_context' ? 'Agents do not pass outputs to each other: each turn writes to one shared conversation history and reads all earlier turns. Numbers give the recorded turn order. ' +
        (lanes.some(laneContextTokens) ? 'Bars show the input tokens of each agent’s first model call, which include the history read so far. ' : '') +
        'Hover over an agent to see which earlier turns it could read.'
      : chain ? 'Arrows show the recorded order of agent invocations, not verified data dependencies.' +
        (lanes.some(l => l.attempts > 1) ? ' An agent invoked again appears once per attempt.' : '')
      : vizRun.arch === 'single' ? 'A single agent ran.'
      : 'The architecture is not recognized, so no arrows are drawn. Boxes follow the recorded invocation order.';
    if (vizRun.arch === 'hierarchical') renderHierarchy(box);
    else if (vizRun.arch === 'shared_context') renderSharedContext(box, lanes);
    else renderChain(box, lanes, chain);
  }
  function renderChain(box, lanes, arrows) {
    // Wrap long chains into rows; a row break continues with an elbow arrow.
    const perRow = lanes.length <= 5 ? lanes.length : 4, pitch = 220, rowHeight = 112;
    const rows = Math.ceil(lanes.length / perRow);
    const width = Math.max(640, perRow * pitch + 40), height = rows * rowHeight - 20;
    const svg = structureSvg(width, height);
    const left = width / 2 - (perRow - 1) * pitch / 2;
    const at = i => ({x: left + (i % perRow) * pitch, y: 16 + Math.floor(i / perRow) * rowHeight});
    if (arrows) for (let i = 1; i < lanes.length; i++) {
      const a = at(i - 1), b = at(i);
      const d = a.y === b.y ? `M ${a.x + 92} ${a.y + 31} L ${b.x - 94} ${b.y + 31}`
        : `M ${a.x} ${a.y + 64} L ${a.x} ${a.y + 80} L ${b.x} ${a.y + 80} L ${b.x} ${b.y - 4}`;
      svg.append(graphElement('path', {d, class: 'edge', 'marker-end': 'url(#structure-arrow)'}));
    }
    lanes.forEach((lane, i) => {
      const {x, y} = at(i), found = laneFindings(lane);
      const sub = [`#${lane.index + 1}`, lane.attempts > 1 ? `attempt ${lane.attempt}` : null,
        found ? plural(found, 'finding') : plural(lane.steps.length, 'step')].filter(Boolean).join(' · ');
      svg.append(structureNode(x, y, {name: lane.name, title: pretty(lane.name), sub, flagged: found > 0,
        attrs: {'data-index': lane.index, 'aria-label': `Invocation ${lane.index + 1}: ${pretty(lane.name)}`},
        onSelect: () => select({kind: 'invocation', index: lane.index})}));
    });
    box.append(svg);
  }
  // Input tokens of an invocation's first model call: in a shared context this includes the history read so far.
  function laneContextTokens(lane) {
    const call = lane.steps.find(step => /^llm_request/.test(step.block));
    const input = Number(call?.value?.tokens?.input ?? call?.value?.input_tokens);
    return Number.isFinite(input) && input > 0 ? input : null;
  }
  function renderSharedContext(box, lanes) {
    // Agents hang off one shared history bus instead of pointing at each other.
    const perRow = lanes.length <= 5 ? lanes.length : 4, pitch = 220, rowHeight = 222;
    const rows = Math.ceil(lanes.length / perRow);
    const width = Math.max(640, perRow * pitch + 40), height = rows * rowHeight - 6;
    const svg = structureSvg(width, height);
    svg.classList.add('shared-context');
    const left = width / 2 - (perRow - 1) * pitch / 2;
    const at = i => ({x: left + (i % perRow) * pitch, y: 26 + Math.floor(i / perRow) * rowHeight});
    const tokens = lanes.map(laneContextTokens), maxTokens = Math.max(0, ...tokens.filter(Boolean));
    for (let row = 0; row < rows; row++) {
      const count = Math.min(perRow, lanes.length - row * perRow), y = 26 + row * rowHeight + 98;
      const x0 = left - 100, x1 = left + (count - 1) * pitch + 100;
      svg.append(graphElement('rect', {x: x0, y, width: x1 - x0, height: 30, rx: 15, class: 'history-bus'}));
      svg.append(graphElement('text', {x: (x0 + x1) / 2, y: y + 20, 'text-anchor': 'middle', class: 'history-label'},
        row ? 'Shared conversation history (continued)' : 'Shared conversation history'));
    }
    const nodes = [];
    lanes.forEach((lane, i) => {
      const {x, y} = at(i), found = laneFindings(lane);
      // Writes its turn to the history and reads all earlier turns from it.
      svg.append(graphElement('path', {d: `M ${x} ${y + 66} L ${x} ${y + 94}`, class: 'edge',
        'marker-start': 'url(#structure-arrow)', 'marker-end': 'url(#structure-arrow)'}));
      const sub = [lane.attempts > 1 ? `attempt ${lane.attempt}` : null,
        found ? plural(found, 'finding') : plural(lane.steps.length, 'step')].filter(Boolean).join(' · ');
      const g = structureNode(x, y, {name: lane.name, title: pretty(lane.name), sub, flagged: found > 0,
        attrs: {'data-index': lane.index, 'aria-label': `Turn ${i + 1}: ${pretty(lane.name)}, reads turns 1 to ${i}`},
        onSelect: () => select({kind: 'invocation', index: lane.index})});
      g.append(graphElement('circle', {cx: x - 90, cy: y, r: 13, class: 'turn-badge'}));
      g.append(graphElement('text', {x: x - 90, y: y + 4.5, 'text-anchor': 'middle', class: 'turn-number'}, String(i + 1)));
      const reach = () => nodes.forEach((n, j) => n.classList.toggle('context-source', j < i));
      const clear = () => nodes.forEach(n => n.classList.remove('context-source'));
      g.addEventListener('mouseenter', reach); g.addEventListener('focus', reach);
      g.addEventListener('mouseleave', clear); g.addEventListener('blur', clear);
      nodes.push(g); svg.append(g);
      if (tokens[i]) {
        const barWidth = Math.max(4, 180 * tokens[i] / maxTokens), barY = y + 136;
        svg.append(graphElement('rect', {x: x - 90, y: barY, width: 180, height: 8, rx: 4, class: 'context-track'}));
        svg.append(graphElement('rect', {x: x - 90, y: barY, width: barWidth, height: 8, rx: 4, class: 'context-fill'}));
        const label = graphElement('text', {x, y: barY + 24, 'text-anchor': 'middle', class: 'edge-label'},
          `reads ${number(tokens[i])} input tokens`);
        label.append(graphElement('title', {}, 'Input tokens of this agent’s first model call, including the shared history so far'));
        svg.append(label);
      }
    });
    box.append(svg);
  }
  function renderHierarchy(box) {
    const names = [...vizRun.colors.keys()];
    const width = Math.max(640, names.length * 210 + 60), height = 250;
    const svg = structureSvg(width, height);
    const cx = width / 2;
    names.forEach((name, i) => {
      const x = cx - (names.length - 1) * 105 + i * 210;
      const lanes = vizRun.lanes.filter(l => l.name === name && !l.pseudo);
      svg.append(graphElement('path', {d: `M ${cx} 82 C ${cx} 125 ${x} 120 ${x} 160`, class: lanes.length ? 'edge' : 'edge idle',
        'marker-end': 'url(#structure-arrow)'}));
      svg.append(graphElement('text', {x: x + (x < cx ? -12 : 12), y: 148,
        'text-anchor': x < cx ? 'end' : 'start', class: 'edge-label'},
        lanes.length ? lanes.map(l => `#${l.index + 1}`).join(', ') : 'not invoked'));
      const found = lanes.reduce((n, l) => n + laneFindings(l), 0);
      svg.append(structureNode(x, 162, {name, title: pretty(name), flagged: found > 0, idle: !lanes.length,
        sub: lanes.length ? `${plural(lanes.length, 'invocation')} · ` +
          (found ? plural(found, 'finding') : plural(lanes.reduce((n, l) => n + l.steps.length, 0), 'step')) : 'Available · not invoked',
        attrs: {'data-agent': name, 'aria-label': `${pretty(name)}, ${plural(lanes.length, 'invocation')}`},
        onSelect: () => { if (lanes.length) select({kind: 'invocation', index: lanes[0].index}); else select({kind: 'agent', name}); }}));
    });
    const orchestrator = graphElement('g', {class: 'structure-node orchestrator', role: 'button', tabindex: 0, 'data-orchestrator': 'true',
      'aria-label': 'Orchestrator setup and task'});
    orchestrator.append(graphElement('rect', {x: cx - 130, y: 16, width: 260, height: 64, rx: 30}));
    orchestrator.append(graphElement('text', {x: cx, y: 44, 'text-anchor': 'middle', class: 'node-title'}, 'Orchestrator'));
    orchestrator.append(graphElement('text', {x: cx, y: 64, 'text-anchor': 'middle', class: 'node-sub'},
      vizRun.team?.team || vizRun.setup?.execution.initialization_metadata?.team_type || 'Coordination role'));
    const chooseOrchestrator = () => select({kind: 'orchestrator'});
    orchestrator.addEventListener('click', chooseOrchestrator);
    orchestrator.addEventListener('keydown', e => { if (['Enter', ' '].includes(e.key)) { e.preventDefault(); chooseOrchestrator(); } });
    svg.append(orchestrator);
    box.append(svg);
  }

  function renderTimeline() {
    const box = $('run-timeline'); box.replaceChildren();
    box.dataset.scale = vizScale;
    $('run-scale-note').textContent = vizScale === 'duration'
      ? 'Lane widths follow each invocation’s recorded duration (a minimum width keeps short invocations readable).'
      : 'Lanes have equal width and follow the recorded invocation order.';
    const active = overlayActive();
    for (const lane of vizRun.lanes) {
      const element = node('section', null, lane.pseudo ? 'lane pseudo' : 'lane');
      element.dataset.index = lane.index;
      element.style.setProperty('--agent', lane.pseudo ? '#94a3b8' : vizRun.colors.get(lane.name));
      const duration = Number(lane.meta.duration_seconds);
      if (vizScale === 'duration') element.style.flexGrow = String(Number.isFinite(duration) && duration > 0 ? duration : 1);
      const head = node('button', null, 'lane-head'); head.type = 'button';
      head.setAttribute('aria-pressed', 'false');
      const title = node('span', null, 'lane-title');
      title.append(node('span', lane.pseudo ? '·' : String(lane.index + 1), 'lane-number'), node('strong', pretty(lane.name)));
      const found = laneFindings(lane);
      if (found) title.append(node('span', String(found), 'finding-badge'));
      head.append(title);
      if (lane.attempts > 1) head.append(node('span', `Attempt ${lane.attempt} of ${lane.attempts}`, 'lane-attempt'));
      const meta = lane.meta;
      const offset = timeOf(meta.agent_timestamp_start) - vizRun.start;
      const facts = [seconds(meta.duration_seconds),
        meta.llm_call_count != null ? plural(Number(meta.llm_call_count) || 0, 'model call') : null,
        Number(meta.tool_call_count) ? plural(Number(meta.tool_call_count), 'tool call') : null,
        vizScale === 'duration' && Number.isFinite(offset) ? `starts +${seconds(offset / 1000)}` : null].filter(Boolean);
      if (facts.length) head.append(node('span', facts.join(' · '), 'lane-meta'));
      if (meta.status && meta.status !== 'success') head.append(node('span', `Status: ${meta.status}`, 'lane-status'));
      const tools = Array.isArray(meta.tool_names) ? meta.tool_names : [];
      if (tools.length) {
        const chips = node('span', null, 'lane-tools');
        for (const tool of tools) chips.append(node('span', tool, 'tool-chip'));
        head.append(chips);
      }
      head.setAttribute('aria-label', `Invocation ${lane.index + 1}: ${pretty(lane.name)}${found ? `, ${plural(found, 'finding')}` : ''}`);
      head.addEventListener('click', () => select({kind: 'invocation', index: lane.index}));
      element.append(head);
      const steps = node('div', null, 'lane-steps');
      steps.setAttribute('role', 'group'); steps.setAttribute('aria-label', `${pretty(lane.name)} steps`);
      for (const step of lane.steps) {
        const group = node('span', null, 'step-group');
        for (const item of [step, ...step.children]) group.append(stepMarker(item, active));
        steps.append(group);
      }
      if (!lane.steps.length) steps.append(node('span', 'No steps recorded', 'help'));
      element.append(steps);
      if (vizHighlight && active && !lane.steps.some(s => [s, ...s.children].some(x => highlightedAt(x.id)))) element.dataset.dim = 'true';
      box.append(element);
    }
    const unknown = $('viz-unmatched'); unknown.replaceChildren();
    if (active) {
      const ids = new Set(vizRun.allSteps.map(s => s.id));
      const missing = [...vizFindings.steps.values()].filter(s => !ids.has(s.id) && s.failures.length);
      if (missing.length) {
        unknown.append(node('span', 'Findings on step IDs not found in this trajectory: '));
        for (const step of missing) {
          const b = node('button', `Step ${step.id}`, 'chip'); b.type = 'button';
          b.addEventListener('click', () => select({kind: 'unknown', id: step.id}));
          unknown.append(b);
        }
      }
    }
  }
  function stepMarker(step, active) {
    const failures = active ? findingsAt(step.id) : [];
    const button = node('button', null, 'step-marker'); button.type = 'button';
    button.dataset.kind = step.kind; button.dataset.path = step.path;
    button.dataset.nested = String(step.nested);
    button.dataset.flagged = String(failures.length > 0);
    if (vizHighlight && active) button.dataset.dim = String(!highlightedAt(step.id));
    const label = `Step ${step.id} · ${KIND_LABELS[step.kind]}${step.kind === 'other' ? ` (${step.block})` : ''}` +
      (step.value?.tool_name ? ` · ${step.value.tool_name}` : '') +
      (active ? ` · ${failures.length ? plural(failures.length, 'finding') +
        (failures.some(f => f.code) ? ` (${[...new Set(failures.map(f => f.code).filter(Boolean))].join(', ')})` : '') : 'none reported'}` : '');
    button.title = label; button.setAttribute('aria-label', label); button.setAttribute('aria-pressed', 'false');
    button.append(node('span', null, 'shape'), node('span', step.id, 'step-id'));
    button.addEventListener('click', () => select({kind: 'step', path: step.path}));
    return button;
  }

  function select(selection) {
    vizSelection = selection;
    markSelection(); renderDetail();
  }
  function markSelection() {
    const s = vizSelection || {};
    const laneIndex = s.kind === 'invocation' ? s.index
      : s.kind === 'step' ? vizRun.allSteps.find(x => x.path === s.path)?.lane.index : null;
    for (const lane of $('run-timeline').querySelectorAll('.lane')) {
      const on = Number(lane.dataset.index) === laneIndex;
      lane.dataset.selected = String(on);
      lane.querySelector('.lane-head').setAttribute('aria-pressed', String(on && s.kind === 'invocation'));
    }
    for (const marker of $('run-timeline').querySelectorAll('.step-marker')) {
      const on = s.kind === 'step' && marker.dataset.path === s.path;
      marker.setAttribute('aria-pressed', String(on));
      if (on) marker.scrollIntoView({block: 'nearest', inline: 'nearest'});
    }
    const agent = s.kind === 'invocation' || s.kind === 'step' ? vizRun.lanes[laneIndex]?.name : s.kind === 'agent' ? s.name : null;
    for (const g of $('structure-diagram').querySelectorAll('.structure-node')) {
      g.setAttribute('aria-pressed', String(g.dataset.orchestrator ? s.kind === 'orchestrator'
        : g.dataset.index !== undefined ? Number(g.dataset.index) === laneIndex : g.dataset.agent === agent));
    }
  }

  // ---- Detail panel ----
  function factList(pairs) {
    const list = node('dl', null, 'detail-facts');
    for (const [label, value] of pairs) {
      if (value == null || value === '') continue;
      const item = node('div');
      const dd = node('dd');
      if (value instanceof Node) dd.append(value); else dd.textContent = String(value);
      item.append(node('dt', label), dd); list.append(item);
    }
    return list.children.length ? list : null;
  }
  function textBlock(label, value, open = true) {
    const text = readableText(value);
    if (!text) return null;
    const block = node('details', null, 'detail-text'); block.open = open;
    block.append(node('summary', label), node('pre', text));
    return block;
  }
  function tokenText(input, output) {
    if (input == null && output == null) return null;
    return `${input != null ? number(input) : '—'} in / ${output != null ? number(output) : '—'} out`;
  }
  function findingCards(failures) {
    const fragment = document.createDocumentFragment();
    for (const failure of failures) {
      const card = node('article', null, 'failure-card');
      card.style.borderLeftColor = categoryColor(vizFindings.groupName(failure));
      const path = classify(failure), type = path?.[path.length - 1];
      const head = node('div', null, 'failure-head');
      const code = type?.code || failure.code;
      if (code) head.append(node('span', code, 'code-badge'));
      head.append(node('h4', type?.name || failure.failure_type || 'Unlabelled failure'));
      if (failure.ambiguous) head.append(node('span', 'Ambiguous — review', 'ambiguous-tag'));
      card.append(head);
      if (type?.description) card.append(node('p', type.description, 'failure-definition'));
      if (path && path.length > 1) {
        // Where the failure type sits: category, then sub-category where the taxonomy has one.
        const list = node('ol', null, 'taxonomy-path');
        list.setAttribute('aria-label', 'Position in the taxonomy');
        for (const item of path.slice(0, -1)) {
          const entry = node('li');
          const title = node('span', null, 'taxonomy-entry');
          title.append(node('span', item.code, 'code'), node('strong', item.name));
          entry.append(node('span', item.level, 'level-tag'), title);
          if (item.description) entry.append(node('span', item.description, 'taxonomy-desc'));
          list.append(entry);
        }
        card.append(list);
      } else if (!vizFindings.taxonomy) {
        card.append(node('p', 'Free-text label from a zero-shot evaluation; not mapped to a taxonomy.', 'failure-meta'));
      }
      for (const [label, text] of [['Evidence — what the agent said', failure.evidence], ['Why it was flagged', failure.rationale]]) {
        if (text) card.append(node('h5', label), node('p', text, 'failure-text'));
      }
      fragment.append(card);
    }
    return fragment;
  }
  function findingsSection(id) {
    if (!vizFindings) return null;
    const section = node('section', null, 'detail-findings');
    if (!overlayActive()) {
      section.append(node('p', 'Findings are not shown: the selected evaluation used a different trajectory.', 'help'));
      return section;
    }
    const entry = vizFindings.steps.get(id), failures = entry?.failures || [];
    section.append(node('h4', failures.length ? `${plural(failures.length, 'reported failure')}` : 'Findings'));
    if (!failures.length) section.append(node('p', entry?.annotated ? 'None reported for this step.' : 'No annotation returned for this step.', 'help'));
    else section.append(findingCards(failures));
    return section;
  }
  function renderDetail() {
    const panel = $('run-detail'); panel.replaceChildren();
    const s = vizSelection;
    if (!s) { panel.append(node('p', 'Select an invocation or a step to inspect it.', 'help')); return; }
    if (s.kind === 'step') return renderStepDetail(panel, vizRun.allSteps.find(x => x.path === s.path));
    if (s.kind === 'invocation') return renderInvocationDetail(panel, vizRun.lanes[s.index]);
    if (s.kind === 'orchestrator') return renderOrchestratorDetail(panel);
    if (s.kind === 'agent') {
      panel.append(node('h3', pretty(s.name)), node('p', 'Listed in the available team. No invocation was recorded in this trajectory.', 'help'));
      return;
    }
    if (s.kind === 'unknown') {
      panel.append(node('h3', `Step ${s.id}`), node('p', 'The evaluation reported this step ID, but it does not appear in the trajectory. Original content is unavailable.', 'failure-warning'));
      panel.append(findingCards(vizFindings.steps.get(s.id)?.failures || []));
    }
  }
  function detailHeading(panel, title, subtitle, color) {
    const head = node('header', null, 'detail-head');
    if (color) head.style.setProperty('--agent', color);
    head.append(node('h3', title));
    if (subtitle) head.append(node('p', subtitle, 'detail-sub'));
    panel.append(head);
    return head;
  }
  function renderInvocationDetail(panel, lane) {
    const m = lane.meta;
    detailHeading(panel, pretty(lane.name), lane.pseudo ? 'Steps recorded outside an agent execution'
      : `Invocation ${lane.index + 1}${lane.attempts > 1 ? ` · attempt ${lane.attempt} of ${lane.attempts}` : ''}` +
        (lane.steps.length ? ` · steps ${lane.steps[0].id}–${lane.steps[lane.steps.length - 1].id}` : ''),
      lane.pseudo ? null : vizRun.colors.get(lane.name));
    const offset = timeOf(m.agent_timestamp_start) - vizRun.start;
    const facts = factList([
      ['Duration', seconds(m.duration_seconds)],
      ['Started', Number.isFinite(offset) ? `+${seconds(offset / 1000)} after run start` : null],
      ['Model calls', m.llm_call_count], ['Tool calls', m.tool_call_count],
      ['Tokens', tokenText(m.input_tokens, m.output_tokens)],
      ['Tools', Array.isArray(m.tool_names) && m.tool_names.length ? m.tool_names.join(', ') : null],
      ['Execution status', m.status],
    ]);
    if (facts) panel.append(facts);
    if (overlayActive()) {
      const flagged = lane.steps.flatMap(s => [s, ...s.children]).filter(s => findingsAt(s.id).length);
      const row = node('p', null, 'detail-flagged');
      row.append(node('strong', flagged.length ? `${plural(flagged.reduce((n, s) => n + findingsAt(s.id).length, 0), 'reported failure')} on ` : 'No failures reported in this invocation.'));
      for (const step of flagged) {
        const b = node('button', `Step ${step.id}`, 'chip flagged'); b.type = 'button';
        b.addEventListener('click', () => select({kind: 'step', path: step.path}));
        row.append(b);
      }
      panel.append(row);
    }
    const ex = lane.execution;
    const input = ex.task?.user_prompt ?? ex.query?.recent_query ?? ex.Query?.recent_query;
    for (const block of [textBlock('Input received', input), textBlock('Output', ex.result?.agent_output),
      textBlock('System prompt', m.system_prompt, false)]) if (block) panel.append(block);
  }
  function renderOrchestratorDetail(panel) {
    detailHeading(panel, 'Orchestrator', 'Coordination role in a hierarchical workflow');
    const init = vizRun.setup?.execution || {}, summary = vizRun.summary || {};
    const im = init.initialization_metadata || {};
    const facts = factList([
      ['Team', vizRun.team?.team || im.team_type], ['Model', im.model],
      ['Delegations recorded', vizRun.team?.agent_invocation_count ?? vizRun.lanes.filter(l => !l.pseudo).length],
      ['Orchestrator actions', summary.magneticOneOrchestrator_actions_count],
      ['Model calls', summary.llm_call_count], ['Tool calls', summary.tool_call_count],
      ['Execution order', Array.isArray(summary.agent_execution_order) ? summary.agent_execution_order.map(pretty).join(' → ') : null],
    ]);
    if (facts) panel.append(facts);
    if (!vizRun.setup && !vizRun.summary) panel.append(node('p', 'This trajectory records no orchestrator setup or workflow summary.', 'help'));
    for (const block of [textBlock('Orchestrator task', summary.orchestrator_task ?? init.orchestrator_setup?.task),
      textBlock('Initial plan', init.orchestrator_response),
      textBlock('Team composition', init.team_composition, false)]) if (block) panel.append(block);
    if (vizRun.setup) {
      const raw = node('details', null, 'detail-json');
      raw.append(node('summary', 'Original JSON · initialization'), node('pre', JSON.stringify(vizRun.setup.execution, null, 2)));
      panel.append(raw);
    }
  }
  function renderStepDetail(panel, step) {
    if (!step) { panel.append(node('p', 'This step is no longer available.', 'help')); return; }
    const v = step.value || {}, lane = step.lane;
    const head = detailHeading(panel, `Step ${step.id} · ${KIND_LABELS[step.kind]}`,
      `${pretty(lane.name)}${lane.pseudo ? '' : ` · invocation ${lane.index + 1}`}${lane.attempts > 1 ? ` · attempt ${lane.attempt}` : ''} · ${step.block}`,
      lane.pseudo ? null : vizRun.colors.get(lane.name));
    const position = vizRun.allSteps.indexOf(step);
    const nav = node('div', null, 'detail-nav');
    for (const [label, target] of [['← Previous step', vizRun.allSteps[position - 1]], ['Next step →', vizRun.allSteps[position + 1]]]) {
      const b = node('button', label); b.type = 'button'; b.disabled = !target;
      if (target) b.addEventListener('click', () => select({kind: 'step', path: target.path}));
      nav.append(b);
    }
    head.append(nav);
    const tokens = v.tokens || {};
    const exit = v.exit_code ?? v.parsed_output?.exit_code ?? v.parsed_output?.returncode;
    let exitBadge = null;
    if (exit != null) { exitBadge = node('span', `exit code ${exit}`, Number(exit) === 0 ? 'badge ok' : 'badge fail'); }
    const duration = v.duration_seconds ?? v.tool_call_duration?.duration_seconds;
    const pairs = {
      model: [['Model', [v.model_name, v.model_provider].filter(Boolean).join(' · ')], ['Duration', seconds(duration)],
        ['Tokens', tokenText(tokens.input, tokens.output)], ['Ended with', v.llm_call_post_condition],
        ['Reflects on', Array.isArray(v.reflects_on_activities) ? v.reflects_on_activities.join(', ') : null]],
      tool: [['Tools', Array.isArray(v.tool_names) ? v.tool_names.join(', ') : null], ['Tool calls', v.tool_call_count], ['Duration', seconds(duration)]],
      'tool-request': [['Tool', v.tool_name], ['Status', v.status], ['Succeeded', v.tool_success == null ? null : v.tool_success ? 'yes' : 'no'],
        ['HTTP', [v.http_method, v.http_status_code].filter(x => x != null).join(' ')], ['Call ID', v.call_id]],
      'tool-result': [['Tool', v.tool_name], ['Duration', seconds(duration)], ['Exit code', exitBadge], ['Call ID', v.call_id]],
      input: [['Input tokens', v.input_tokens != null ? number(v.input_tokens) : null]],
      output: [['Output tokens', v.output_tokens != null ? number(v.output_tokens) : null]],
      telemetry: [['Traces of agent', v.Traces_of_agent], ['Spans', Array.isArray(v.Traces) ? v.Traces.length : null]],
    };
    pairs.reflection = pairs.model;
    let facts = pairs[step.kind];
    if (!facts) facts = Object.entries(v).filter(([k, x]) => k !== 'step_id' && x != null && typeof x !== 'object').slice(0, 8)
      .map(([k, x]) => [k, String(x).length > 120 ? String(x).slice(0, 117) + '…' : x]);
    const list = factList(facts);
    if (list) panel.append(list);
    const findings = findingsSection(step.id);
    if (findings) panel.append(findings);
    const texts = {
      input: [['Input', v.user_prompt ?? v.recent_query]],
      output: [['Output', v.agent_output]],
      'tool-request': [['Arguments', v.tool_arguments]],
      'tool-result': [['Result', v.result_preview ?? v.raw_output], ['Error', v.error]],
    }[step.kind] || [];
    for (const [label, value] of texts) { const block = textBlock(label, value); if (block) panel.append(block); }
    if (step.kind === 'tool' && step.children.length) {
      const row = node('p', null, 'detail-flagged');
      row.append(node('strong', 'Nested steps: '));
      for (const child of step.children) {
        const b = node('button', `${child.id} · ${KIND_LABELS[child.kind]}`, 'chip'); b.type = 'button';
        b.addEventListener('click', () => select({kind: 'step', path: child.path}));
        row.append(b);
      }
      panel.append(row);
    }
    const raw = node('details', null, 'detail-json');
    raw.append(node('summary', 'Original JSON'), node('code', `JSON Pointer: ${step.path || '/'}`, 'json-pointer'));
    // Prefer the server-formatted block: it preserves large integer values.
    const pre = node('pre', step.record?.json ?? JSON.stringify(v, null, 2)); pre.tabIndex = 0;
    raw.append(pre); panel.append(raw);
  }
  function updateReady() {
    run.disabled = !initialized || !inspected || busy || tracking;
    previewButton.disabled = !initialized || !inspected;
    updateReviewEmpty();
    if (reviewOpen()) loadViz();
  }
  function clearTrajectoryPreview() {
    previewVersion++;
    previewPanel.hidden = true;
    previewButton.textContent = 'View trajectory';
    previewButton.setAttribute('aria-expanded', 'false');
    $('eval-trajectory-preview-status').textContent = '';
    $('eval-trajectory-json').textContent = '';
    $('eval-trajectory-json').hidden = true;
  }
  previewButton.addEventListener('click', async () => {
    if (!previewPanel.hidden) { clearTrajectoryPreview(); return; }
    if (!inspected) return;
    const version = ++previewVersion, selection = selectionVersion, input = inspected;
    previewPanel.hidden = false;
    previewButton.textContent = 'Hide trajectory';
    previewButton.setAttribute('aria-expanded', 'true');
    $('eval-trajectory-preview-status').textContent = 'Loading trajectory…';
    try {
      const value = await api('/api/trajectory', {method: 'POST', body: JSON.stringify({
        ...sourcePayload(), expected_sha256: input.sha256,
      })});
      if (version !== previewVersion || selection !== selectionVersion) return;
      // Keep server-side integer precision (for example, nanosecond timestamps).
      $('eval-trajectory-json').textContent = value.trajectory_json;
      $('eval-trajectory-json').hidden = false;
      $('eval-trajectory-preview-status').textContent = `Read-only preview · ${input.name}`;
    } catch (error) {
      if (version !== previewVersion || selection !== selectionVersion) return;
      $('eval-trajectory-preview-status').textContent = error.message;
    }
  });
  function lock(value) {
    busy = value; fields.disabled = value;
    run.textContent = value ? 'Evaluation in progress…' : 'Run evaluation';
    updateReady();
  }
  function parameters() {
    const guided = form.elements.strategy.value === 'taxonomy-guided';
    form.elements.taxonomy.disabled = !guided;
    form.elements.dedup.disabled = !guided;
    const fixed = /^(gpt-5|o[1-9])/i.test(form.elements.model.value.split('/').pop());
    for (const key of ['temperature', 'top_p']) form.elements[key].disabled = fixed;
    $('eval-parameter-note').textContent = [
      guided ? 'Taxonomy and deduplication apply.' : 'Zero-shot uses neither taxonomy nor deduplication.',
      fixed ? 'This model uses default sampling; temperature and Top P will not be sent.' : 'Blank sampling fields use model defaults.',
    ].join(' ');
  }
  form.elements.strategy.addEventListener('change', parameters);
  form.elements.model.addEventListener('input', parameters);
  form.elements.client.addEventListener('change', () => {
    const profile = config.providers[form.elements.client.value];
    form.elements.model.value = profile.model; form.elements.base_url.value = profile.base_url;
    form.elements.api_key.value = ''; parameters();
  });
  function sourcePanels() {
    const server = mode.value === 'server';
    $('eval-upload-panel').hidden = server;
    $('eval-dialogues-label').hidden = !server || config?.context?.kind === 'workflow';
    $('eval-current-context').hidden = !server;
    $('eval-dialogue-status').hidden = !server || !config?.dialogue;
  }
  function clearInput() {
    clearTrajectoryPreview();
    if (vizSource === 'current') clearViz();
    selectionVersion++; inspected = null; setContextTrajectory(null); updateReady();
    $('eval-input-info').textContent = 'Choose a trajectory to check its format and steps.';
    $('eval-output-preview').textContent = 'Select an input to see the result folder.';
  }
  function sourcePayload() {
    const payload = {context_id: contextId, invocation_id: invocationId};
    if (mode.value === 'server') {
      if (!selected) throw new Error(config?.context?.kind === 'workflow'
        ? 'The workflow trajectory is unavailable' : 'Select a saved WorkSwarm dialogue');
      payload.server_file = selected;
    } else {
      if (!uploaded) throw new Error('Select a valid trajectory JSON');
      payload.trajectory = uploaded.value; payload.upload_name = uploaded.name;
    }
    return payload;
  }
  async function inspect() {
    clearTrajectoryPreview();
    if (vizSource === 'current') clearViz();
    const version = ++selectionVersion;
    inspected = null; setContextTrajectory(null, true); updateReady();
    $('eval-input-info').textContent = 'Checking trajectory and result folder…';
    try {
      const info = await api('/api/inspect', {method: 'POST', body: JSON.stringify(sourcePayload())});
      if (version !== selectionVersion) return;
      inspected = info; setContextTrajectory(info);
      $('eval-input-info').textContent = `${info.source_path || info.name} · ${info.format} · ${info.step_count} steps` +
        (info.dialogue ? ` · Revision ${info.dialogue.revision}, ${info.dialogue.turn_count} recorded turns` : '');
      $('eval-output-preview').textContent = `${info.output_parent}/YYYY-MM-DD_HH-MM-SS_ui/ (UTC)`;
      $('eval-form-status').textContent = '';
    } catch (error) {
      if (version !== selectionVersion) return;
      setContextTrajectory(null);
      $('eval-input-info').textContent = error.message;
      $('eval-output-preview').textContent = 'Input or result folder is not ready.';
    }
    updateReady();
  }
  async function chooseTrace(selection, signature) {
    if (!selection) return;
    selected = selection;
    selectedFileSignature = signature;
    await inspect();
  }
  mode.addEventListener('change', async () => {
    selected = null; uploaded = null; fileInput.value = ''; clearInput(); sourcePanels();
    if (mode.value === 'server') {
      try { await refreshContext(); }
      catch (error) { $('eval-input-info').textContent = error.message; }
    }
  });
  fileInput.addEventListener('change', async () => {
    clearInput(); uploaded = null;
    const version = selectionVersion;
    try {
      const file = fileInput.files[0];
      if (!file || file.size > 10 * 1024 * 1024) throw new Error('Select a JSON file smaller than 10 MiB');
      const value = JSON.parse(await file.text());
      if (version !== selectionVersion || mode.value !== 'upload') return;
      uploaded = {name: file.name, value}; await inspect();
    } catch (error) { if (version === selectionVersion) $('eval-input-info').textContent = error.message; }
  });

  function showProgress(state) {
    const states = {
      starting: ['Starting evaluation', 'Preparing the selected trajectory for evaluation.', '…'],
      loading: ['Loading evaluation', 'Retrieving the latest evaluation status.', '…'],
      running: ['Evaluation running', 'The judge is reviewing the trajectory. Progress percentage is not available yet.', '…'],
      reconnecting: ['Reconnecting to evaluation', 'The evaluation may still be running. Waiting for a status update.', '…'],
      completed: ['Evaluation completed', 'Open Review to see the findings and download the annotations.', '✓'],
      partial: ['Evaluation finished with partial results', 'Some categories could not be evaluated. Review the available findings and details below.', '!'],
      failed: ['Evaluation failed', 'Check the error message below before trying again.', '!'],
      interrupted: ['Evaluation interrupted', 'The evaluation did not finish. Review the details before starting again.', '!'],
      cancelled: ['Evaluation cancelled', 'The evaluation was stopped before completion.', '!'],
      unavailable: ['Evaluation unavailable', 'Select a previous evaluation or start a new one.', '!'],
    };
    const [title, note, icon] = states[state] || states.unavailable;
    const panel = $('eval-progress-panel'), bar = $('eval-progress');
    panel.hidden = false; panel.dataset.state = states[state] ? state : 'unavailable';
    $('eval-progress-title').textContent = title;
    $('eval-progress-note').textContent = note;
    $('eval-progress-icon').textContent = icon;
    const pill = $('run-pill');
    pill.hidden = state === 'unavailable' || !states[state];
    pill.dataset.state = panel.dataset.state;
    $('run-pill-text').textContent = title;
    pill.title = note;
    const pending = ['starting', 'loading', 'running', 'reconnecting'].includes(state);
    bar.hidden = !pending && state !== 'completed';
    if (state === 'completed') bar.setAttribute('aria-valuenow', '100');
    else bar.removeAttribute('aria-valuenow');
  }
  function showOutputFolder(path) {
    const next = typeof path === 'string' ? path : '';
    if (selectedOutputPath !== next) $('eval-folder-feedback').textContent = '';
    selectedOutputPath = next;
    $('eval-history-folder').hidden = !next;
    $('eval-folder-path').textContent = next;
  }
  // A running evaluation is followed in Set up & run; a finished one is reviewed.
  $('run-pill').addEventListener('click', () => showView(
    ['starting', 'running', 'reconnecting', 'failed'].includes($('run-pill').dataset.state) ? 'setup' : 'review'));
  $('eval-copy-folder').addEventListener('click', async () => {
    const path = selectedOutputPath;
    if (!path) return;
    try {
      await navigator.clipboard.writeText(path);
      if (path === selectedOutputPath) $('eval-folder-feedback').textContent = 'Folder path copied.';
    } catch (_) {
      if (path === selectedOutputPath) $('eval-folder-feedback').textContent = 'Copy unavailable. Select and copy the path above.';
    }
  });
  function renderFailures(job) {
    const raw = job.annotations;
    const rows = Array.isArray(raw) ? raw : raw?.annotations;
    if (!Array.isArray(rows) || !['completed', 'partial'].includes(job.status)) {
      vizFindings = null; vizHighlight = null; renderOverview();
      if (vizRun) renderViz();
      return;
    }
    const params = job.parameters || {};
    const method = {'zero-shot': 'Zero-shot', 'taxonomy-guided': 'Taxonomy-guided'}[params.strategy] || params.strategy || 'Unavailable';
    const taxonomy = params.strategy === 'taxonomy-guided'
      ? ({ours: 'RAMS failures taxonomy', mast: 'MAST failures taxonomy'}[params.taxonomy] || params.taxonomy || 'Unavailable') : null;
    $('eval-visual-settings').textContent = [
      `Evaluation ${job.id}`, `Model: ${params.model || 'Unavailable'}`, `Method: ${method}`,
      taxonomy ? `Taxonomy: ${taxonomy}` : null,
    ].filter(Boolean).join(' · ');
    const steps = new Map();
    const known = Array.isArray(job.trajectory_step_ids) ? job.trajectory_step_ids.map(String) : null;
    for (const id of known || []) steps.set(id, {id, failures: [], annotated: false, known: true});
    for (const row of rows) {
      if (row?.step_id == null) continue;
      const id = String(row.step_id);
      if (!steps.has(id)) steps.set(id, {id, failures: [], annotated: false, known: known === null});
      const step = steps.get(id);
      step.annotated = true;
      step.failures.push(...(Array.isArray(row.failures) ? row.failures.filter(f => f && typeof f === 'object') : []));
    }
    const list = [...steps.values()];
    const failures = list.flatMap(step => step.failures);
    const affected = list.filter(step => step.known && step.failures.length).length;
    $('eval-failure-count').textContent = failures.length;
    $('eval-affected-count').textContent = known ? `${affected} / ${known.length}` : `${affected} / ${list.length}`;
    $('eval-affected-label').textContent = known ? 'Affected trajectory steps' : 'Affected reported steps';
    $('eval-ambiguous-count').textContent = failures.filter(f => f.ambiguous).length;
    $('eval-visual-note').textContent = [
      job.status === 'partial' ? 'Partial results — some categories could not be evaluated.' : '',
      failures.length ? 'Counts reflect reported failure occurrences, including ambiguous findings.' : 'No failures detected in the returned annotations.',
    ].filter(Boolean).join(' ');
    const guided = params.strategy !== 'zero-shot';
    const taxonomyModel = guided ? buildTaxonomy(params.taxonomy) : null;
    $('eval-breakdown-title').textContent = guided ? 'Failures by category' : 'Failures by label';
    const groupName = f => {
      if (!guided) return f.failure_type || 'Unlabelled failure';
      const root = classify(f, taxonomyModel)?.[0];
      return root ? codeLabel(root) : (f.category || f.source_category || 'Uncategorized');
    };
    const groups = new Map(), counts = new Map(), types = new Map();
    let unmapped = 0;
    for (const step of list) for (const failure of step.failures) {
      groups.set(groupName(failure), (groups.get(groupName(failure)) || 0) + 1);
      const path = classify(failure, taxonomyModel);
      if (taxonomyModel && !taxonomyModel.index.has(String(failure.code || '').replace(/\.$/, ''))) unmapped++;
      for (const item of path || []) if (item.code) counts.set(item.code, (counts.get(item.code) || 0) + 1);
      const leaf = path?.[path.length - 1];
      const key = leaf?.code ? `code:${leaf.code}` : `type:${failure.failure_type || 'Unlabelled failure'}`;
      if (!types.has(key)) types.set(key, {kind: leaf?.code ? 'code' : 'type', code: leaf?.code || '',
        name: leaf?.name || failure.failure_type || 'Unlabelled failure', ids: new Set()});
      types.get(key).ids.add(step.id);
    }
    // A failure type reported at several steps may mark one issue propagating; causation is not established.
    const recurring = [...types.values()].filter(t => t.ids.size > 1)
      .map(t => ({...t, label: t.code ? `${t.code} ${t.name}` : t.name, ids: [...t.ids].sort(compareStepIds)}))
      .sort((a, b) => b.ids.length - a.ids.length);
    if (vizFindings?.jobId !== job.id) vizHighlight = null;
    vizFindings = {jobId: job.id, sha256: job.input?.sha256, steps, failures, guided, groupName, recurring,
      taxonomy: taxonomyModel, counts, unmapped, groups: [...groups].sort((a, b) => b[1] - a[1])};
    renderOverview();
    if (vizRun) renderViz();
  }

  function renderJob(job) {
    const changedJob = latestJob?.id !== job.id;
    const changedStatus = latestJob?.status !== job.status;
    const justFinished = !changedJob && latestJob?.status === 'running' && ['completed', 'partial'].includes(job.status);
    latestJob = job;
    if (changedJob) vizSource = 'evaluation';
    if (vizSource === 'evaluation' && (changedJob || changedStatus)) clearViz();
    renderFailures(job);
    if (reviewOpen()) loadViz();
    showProgress(job.status);
    showOutputFolder(job.output_dir);
    const input = job.input || {}, params = job.parameters || {}, ctx = input.context;
    $('eval-job-summary').textContent = [
      `Evaluation: ${job.id}`,
      `Input: ${input.source_path || input.name || 'Uploaded trajectory'}`,
      ctx ? `${ctx.kind === 'dialogue' ? 'Dialogue: ' + ctx.session_id : ctx.kind === 'workflow' ? 'Workflow: ' + ctx.name : 'Agent: ' + ctx.agent_name} · Session: ${ctx.session_id}` : '',
      input.dialogue ? `Evaluated revision ${input.dialogue.revision} · ${input.dialogue.turn_count} recorded turns · ${input.dialogue.trace_count} traces` : `Selected file · Invocation: ${input.invocation_id || 'manual input'}`,
      `Judge: ${params.client} / ${params.model} · ${params.base_url}`,
      `Strategy: ${params.strategy} · Taxonomy: ${params.taxonomy || 'not used'} · Deduplication: ${params.dedup || 'none'}`,
      `Sampling: temperature ${params.temperature ?? 'default'}, Top P ${params.top_p ?? 'default'}`,
      `Output: ${job.output_dir}`,
    ].filter(Boolean).join('\n');
    $('eval-status').textContent = `${job.status}: ${job.error || job.message || 'Evaluation running'}`;
    if (job.annotations_file && job.output_dir) $('eval-status').textContent += ` · Generated JSON: ${job.output_dir}/failure_detector_result.json`;
    $('eval-result').textContent = job.annotations == null ? '' : JSON.stringify(job.annotations, null, 2);
    $('eval-download').hidden = !job.annotations_file || !['completed', 'partial'].includes(job.status);
    $('eval-download').textContent = job.status === 'partial' ? 'Download partial failure_detector_results.json' : 'Download failure_detector_results.json';
    if (justFinished) showView('review');
  }
  async function refreshHistory() {
    const data = await api('/api/runs' + queryContext());
    const select = $('eval-history'); select.replaceChildren();
    const placeholder = document.createElement('option'); placeholder.value = ''; placeholder.textContent = data.runs.length ? `Choose one to review (${data.runs.length} saved)` : 'No saved evaluations yet'; select.append(placeholder);
    for (const job of data.runs) {
      const option = document.createElement('option'); option.value = job.id;
      const folder = (job.output_dir || '').split(/[\\/]/).filter(Boolean).pop();
      const params = job.parameters || {};
      const method = {'zero-shot': 'Zero-shot', 'taxonomy-guided': 'Taxonomy-guided'}[params.strategy] || params.strategy || 'Method unavailable';
      const taxonomy = params.strategy === 'taxonomy-guided' && params.taxonomy
        ? ({ours: 'RAMS failures taxonomy', mast: 'MAST failures taxonomy'}[params.taxonomy] || params.taxonomy) : null;
      option.textContent = [folder || job.id, job.status, params.model || job.input?.name, method, taxonomy].filter(Boolean).join(' · ');
      option.title = job.output_dir || '';
      select.append(option);
    }
    if (runId) select.value = runId;
    return data.runs;
  }
  async function track(id) {
    if (latestJob?.id !== id) {
      showOutputFolder(null);
      vizFindings = null;
    }
    runId = id; setQuery('evaluation', id); updateReviewEmpty();
    if (latestJob?.id !== id) {
      latestJob = null; vizSource = 'evaluation'; clearViz();
    }
    const version = ++pollVersion; tracking = true; lock(true);
    showProgress('loading');
    let failures = 0;
    while (version === pollVersion) {
      try {
        const job = await api(`/api/runs/${id}`);
        if (version !== pollVersion) return;
        if (pendingSubmission?.id === job.id) {
          form.elements.api_key.value = '';
          if ($('eval-form-status').textContent === pendingSubmission.error) $('eval-form-status').textContent = '';
          pendingSubmission = null;
        }
        failures = 0; renderJob(job); $('eval-reconnect').hidden = true;
        if (job.status !== 'running') {
          tracking = false; lock(false); parameters();
          await refreshHistory().catch(() => {}); return;
        }
      } catch (error) {
        if (version !== pollVersion) return;
        if (error.httpStatus === 404) {
          if (pendingSubmission?.id === id) pendingSubmission = null;
          tracking = false; lock(false); parameters(); setQuery('evaluation', null); runId = null;
          showOutputFolder(null);
          vizFindings = null; latestJob = null; clearViz(); updateReviewEmpty();
          showProgress('unavailable');
          $('eval-status').textContent = 'This evaluation is unavailable. Choose one from Recent evaluations or start a new evaluation.';
          return;
        }
        failures++;
        showProgress('reconnecting');
        $('eval-status').textContent = `Connection interrupted. Retrying; evaluation ${id} may still be running. ${error.message}`;
        $('eval-reconnect').hidden = false;
      }
      await new Promise(resolve => setTimeout(resolve, Math.min(1000 * 2 ** Math.min(failures, 4), 15000)));
    }
  }
  $('eval-reconnect').addEventListener('click', () => { if (runId) track(runId); });
  // Choosing the placeholder deselects the evaluation; nothing of it stays on screen.
  function clearEvaluationSelection() {
    pollVersion++;
    runId = null; setQuery('evaluation', null);
    latestJob = null; vizFindings = null; vizHighlight = null;
    for (const id of ['run-pill', 'eval-progress-panel', 'eval-reconnect', 'eval-download']) $(id).hidden = true;
    showOutputFolder(null);
    $('eval-status').textContent = 'No evaluation selected. Start one in “Set up & run” or choose one under “Saved evaluations” in the header.';
    $('eval-result').textContent = ''; $('eval-job-summary').textContent = '';
    vizSource = 'current'; clearViz(); updateReviewEmpty();
    if (reviewOpen()) loadViz();
  }
  $('eval-history').addEventListener('change', () => {
    const id = $('eval-history').value;
    if (id) { track(id); showView('review'); return; }
    // A running evaluation stays selected so its progress remains visible.
    if (tracking) { $('eval-history').value = runId || ''; return; }
    clearEvaluationSelection();
  });
  $('eval-history-refresh').addEventListener('click', () => refreshHistory().catch(e => { $('eval-status').textContent = e.message; }));
  form.addEventListener('submit', async event => {
    event.preventDefault(); if (!inspected || busy || tracking) return;
    // Capture before disabling the fieldset: disabled controls are omitted by FormData.
    const payload = Object.fromEntries(new FormData(form));
    for (const key of ['temperature', 'top_p']) payload[key] = payload[key] ? Number(payload[key]) : null;
    Object.assign(payload, sourcePayload(), {expected_sha256: inspected.sha256, request_id: crypto.randomUUID().replaceAll('-', '')});
    lock(true); showProgress('starting');
    $('eval-form-status').textContent = 'Starting evaluation…';
    try {
      const job = await api('/api/runs', {method: 'POST', body: JSON.stringify(payload)});
      form.elements.api_key.value = ''; payload.api_key = '';
      runId = job.id; setQuery('evaluation', job.id);
      $('eval-form-status').textContent = ''; renderJob(job); await refreshHistory().catch(() => {}); await track(job.id);
    } catch (error) {
      $('eval-form-status').textContent = error.message;
      // Keep the known request ID even if the accepted POST response was lost.
      if (!error.httpStatus) {
        pendingSubmission = {id: payload.request_id, error: error.message};
        await track(payload.request_id); return;
      }
      // Another tab may already have started a job for this context.
      try {
        const jobs = await refreshHistory(); const active = jobs.find(j => j.status === 'running' && j.origin !== 'terminal');
        if (active) { form.elements.api_key.value = ''; await track(active.id); return; }
      } catch (_) { /* Keep the original error visible. */ }
      showProgress('failed'); lock(false); parameters();
    } finally { payload.api_key = ''; }
  });
  $('eval-download').addEventListener('click', async () => {
    if (!latestJob?.annotations_file) return;
    $('eval-download').disabled = true;
    try {
      const response = await fetch(`/api/runs/${latestJob.id}/annotations`, {headers: {Authorization: `Bearer ${token}`}});
      if (!response.ok) throw new Error('Generated annotations are unavailable');
      const address = URL.createObjectURL(await response.blob()); const link = document.createElement('a');
      link.href = address; link.download = 'failure_detector_results.json'; link.click(); setTimeout(() => URL.revokeObjectURL(address), 1000);
    } catch (error) { $('eval-status').textContent = error.message; }
    finally { $('eval-download').disabled = false; }
  });
  async function refreshContext(initial = false) {
    let next;
    try { next = await api(configPath()); }
    catch (error) {
      if (mode.value === 'server' && !busy && !tracking) {
        selected = null; clearInput();
        $('eval-input-info').textContent = 'Cannot refresh the selected trajectory. Waiting for the connection to recover.';
      }
      throw error;
    }
    // Run may have started while this timer request was in flight.
    if (!initial && (busy || tracking)) return;
    config = next;
    const workflow = next.context?.kind === 'workflow';
    if (next.context?.kind === 'dialogue' && invocationId) {
      // Plugin evaluation follows the complete dialogue as new turns arrive.
      invocationId = null; setQuery('invocation', null);
    }
    mode.querySelector('option[value=server]').textContent = workflow
      ? 'Workflow trajectory' : 'WorkSwarm dialogue';
    const chooser = $('eval-dialogues'), choices = next.dialogues || [];
    const signature = JSON.stringify(choices.map(d => [d.id, d.name]));
    if (chooser.dataset.choices !== signature) {
      chooser.replaceChildren(new Option('Select a saved dialogue', ''));
      for (const item of choices) chooser.append(new Option(item.name, item.id));
      chooser.dataset.choices = signature;
    }
    chooser.value = next.dialogue ? contextId : '';
    $('eval-connection').textContent = `Connected · Session: ${next.session_id}`;
    $('eval-current-context').textContent = next.context
      ? `${next.dialogue ? 'Dialogue: ' + next.context.name : next.context.kind === 'workflow' ? 'Workflow: ' + next.context.name : 'Agent: ' + next.context.agent_name + ' (' + next.context.agent_id + ')'} · Session: ${next.context.session_id}`
      : 'Select a saved WorkSwarm dialogue or upload a trajectory from your computer.';
    const dialogue = next.dialogue;
    $('eval-dialogue-status').textContent = dialogue ?
      `Revision ${dialogue.revision} · ${dialogue.turn_count} recorded turns · ${dialogue.trace_count} traces · ${dialogue.status}` +
      (dialogue.pending_turn_count ? ` · ${dialogue.pending_turn_count} pending` : '') +
      (dialogue.error ? ` · ${dialogue.error}` : '') : '';
    sourcePanels();
    if (mode.value === 'server' && workflow) {
      // Use the pinned file, never the latest file from another invocation.
      const file = next.default_file;
      const signature = JSON.stringify(file) === JSON.stringify(next.latest_file)
        ? next.latest_file_signature : null;
      if (!file) {
        selected = null; clearInput();
        $('eval-input-info').textContent = 'The workflow trajectory is unavailable. Restore the file or upload a trajectory.';
      } else if (!selected || !inspected || JSON.stringify(file) !== JSON.stringify(selected) ||
                 signature !== selectedFileSignature) {
        await chooseTrace(file, signature);
      }
    } else if (mode.value === 'server') {
      if (!dialogue || dialogue.status !== 'ready' || !next.latest_file) {
        selected = null; clearInput();
        $('eval-input-info').textContent = dialogue
          ? dialogue.error || 'Waiting for the first completed trajectory in this dialogue.'
          : choices.length ? 'Select a saved WorkSwarm dialogue.' : 'No saved dialogues yet. Send a message with the plugin enabled, or upload a trajectory.';
      } else if (!selected || !inspected || dialogue.revision !== inspected.dialogue?.revision ||
                 next.latest_file_signature !== selectedFileSignature) {
        await chooseTrace(next.latest_file, next.latest_file_signature);
      }
    }
  }
  $('eval-dialogues').addEventListener('change', () => {
    const id = $('eval-dialogues').value;
    if (!id || id === contextId) return;
    const address = new URL(location.href);
    address.searchParams.set('context', id);
    address.searchParams.delete('invocation'); address.searchParams.delete('evaluation');
    location.assign(address);
  });
  showView(url.searchParams.get('view') === 'review' || runId ? 'review' : 'setup');
  (async () => {
    try {
      await refreshContext(true);
      for (const [key, value] of Object.entries(config.defaults)) if (form.elements[key]) form.elements[key].value = value ?? '';
      parameters(); initialized = true; updateReady();
      const jobs = await refreshHistory(); const active = jobs.find(j => j.status === 'running' && j.origin !== 'terminal');
      if (runId || active) track(runId || active.id);
      setInterval(() => { if (!busy && !tracking) refreshContext().catch(() => { $('eval-connection').textContent = 'Connection unavailable. Reconnecting…'; }); }, 5000);
    } catch (error) {
      $('eval-connection').textContent = `Cannot connect: ${error.message}. Open the full URL supplied by the evaluation service.`;
    }
  })();
})();
