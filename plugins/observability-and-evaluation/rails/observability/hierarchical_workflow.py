"""Export observed invocations using the existing hierarchical v2 contract."""
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import re

from .emit.spans import parse_otel_time
from .model.agent import AgentTrace
from .model.hierarchical import HierarchicalWorkflowTraceV2, InitializationExecution, SkippedAgentsArtifact
from .model.version import SCHEMA_VERSION
from .workflow import _renumber, _step_blocks
from .observability.json.hierarchical_json_handler import extract_query_from_otel_spans


def merge_hierarchical_workflow(agent_json_files, output_dir, *, task, plan,
                                provided_agents, run_nr=1, model_name=None,
                                scenario_meta=None, run_metadata=None):
    """Write OrchestratorObj files and agentic_trajectory.json; return its Path.

    provided_agents contains native cards for all available specialists, not the
    planner/supervisor/final-answer agents. Select by native card ID, never name.
    Supply complete observations for one team run: omitted files cannot always
    be distinguished from specialists that were never invoked.
    Original observer artifacts are not modified. Existing outputs are rejected.
    """
    if isinstance(agent_json_files, (str, Path)):
        raise TypeError('Pass an iterable of agent JSON paths')
    if not isinstance(run_nr, int) or isinstance(run_nr, bool) or run_nr < 1:
        raise ValueError('run_nr must be a positive integer')
    if not isinstance(task, str) or not isinstance(plan, str):
        raise TypeError('task and plan must be strings')
    roster = {}
    for card in provided_agents:
        card_id, name = getattr(card, 'id', None), getattr(card, 'name', None)
        if not isinstance(card_id, str) or not card_id or not isinstance(name, str) or not name:
            raise ValueError('Each provided agent must have a nonempty native card id and name')
        if card_id in roster or name in roster.values() or name == 'initialization':
            raise ValueError('Provided agent IDs/names must be unique; initialization is reserved')
        roster[card_id] = name
    paths = [Path(p).expanduser().resolve() for p in agent_json_files]
    if len(set(paths)) != len(paths):
        raise ValueError('Duplicate agent JSON paths')
    selected, seen, models = [], set(), set()
    for path in paths:
        data = json.loads(path.read_text(encoding='utf-8'))
        AgentTrace.model_validate(data)
        if data.get('schema_version') != SCHEMA_VERSION:
            raise ValueError(f'Expected schema_version {SCHEMA_VERSION}: {path}')
        meta = data['agent_metadata']
        if meta['agent_id'] not in roster:
            continue
        if meta['agent_name'] != roster[meta['agent_id']]:
            raise ValueError('Card ID matches but agent name differs')
        start, end = (parse_otel_time(meta[key]) for key in ('agent_timestamp_start', 'agent_timestamp_end'))
        if not start or not end or not start.tzinfo or not end.tzinfo or end < start:
            raise ValueError('Invocation timestamps must be timezone-aware and ordered')
        roots = [s for s in data['OTel_traces']['Traces']
                 if s.get('name') == f"invoke_agent {meta['agent_name']}"
                 and s.get('attributes', {}).get('gen_ai.agent.id') == meta['agent_id']]
        if len(roots) != 1:
            raise ValueError('Expected exactly one native invocation root per artifact')
        identity = tuple(roots[0].get('context', {}).get(k) for k in ('trace_id', 'span_id'))
        if not all(identity) or identity in seen:
            raise ValueError('Missing or duplicate invocation span identity')
        seen.add(identity)
        selected.append((start, end, identity, data))
        for block in _step_blocks(data):
            if block.get('model_name'):
                models.add(block['model_name'])
    selected.sort(key=lambda row: (row[0], row[2]))
    model = model_name if model_name is not None else ', '.join(sorted(models))
    directory = Path(output_dir).expanduser().resolve()
    destination = directory / 'agentic_trajectory.json'
    names = list(roster.values())
    counts = Counter(row[3]['agent_metadata']['agent_name'] for row in selected)
    init = {
        'schema_version': SCHEMA_VERSION,
        'initialization_metadata': {'run_id': str(run_nr), 'timestamp_utc': datetime.now(timezone.utc).isoformat(),
                                    'team_type': 'MagenticOneGroupChat', 'model': model},
        'orchestrator_setup': {'task': task, 'model_used': model},
        'team_composition': {'total_agents': len(names), 'agent_names': names},
        'orchestrator_response': plan,
    }
    InitializationExecution.model_validate(init)
    init_name = f'OrchestratorObj_0_initialization_{run_nr}.json'
    documents = {init_name: init}
    agents = {init_name: {'sequence': 0, 'agent_name': 'initialization', 'invocation_info': {}, 'execution': init}}
    offset, per_agent = 0, Counter()
    metadata = []
    for sequence, (_, _, _, original) in enumerate(selected, 1):
        data = deepcopy(original)
        meta = data['agent_metadata']
        name = meta['agent_name']
        metadata.append(meta)
        per_agent[name] += 1
        recent_query = extract_query_from_otel_spans(otel_spans=data['OTel_traces']['Traces'], agent_name=name)
        # Hierarchical executions use ordinal IDs; native identity remains in raw spans.
        meta['agent_id'] = f'agent_{sequence}'
        if 'task' in data:
            old = data.pop('task')
            data['query'] = {'step_id': old['step_id'], 'recent_query': old['user_prompt'], 'input_tokens': old['input_tokens']}
        elif 'Query' in data:
            data['query'] = data.pop('Query')
        if recent_query:
            data['query']['recent_query'] = recent_query
        # Match the original ordering: metadata, query, operations, result, traces.
        data = {'schema_version': data['schema_version'], 'agent_metadata': meta, 'query': data['query'],
                **{k: v for k, v in data.items() if k not in ('schema_version', 'agent_metadata', 'query')}}
        offset = _renumber(data, offset)
        AgentTrace.model_validate(data)
        safe_name = re.sub(r'[^A-Za-z0-9_-]', '_', name)
        filename = f'OrchestratorObj_{sequence}_{safe_name}_{run_nr}.json'
        documents[filename] = data
        agents[filename] = {'sequence': sequence, 'agent_name': name,
                           'invocation_info': {'invocation_number': per_agent[name], 'total_invocations_of_agent': counts[name],
                                               'query_preview': data['query']['recent_query'][:100]},
                           'execution': data}
    order = [m['agent_name'] for m in metadata]
    tools = sorted({tool for m in metadata for tool in m['tool_names']})
    statuses = [m['status'] for m in metadata]
    # Preserve the historical merger's status vocabulary and precedence.
    status = ('no_agents_executed' if not statuses else 'success' if all(s == 'success' for s in statuses)
              else 'failed' if 'failed' in statuses else 'partial')
    tokens = {key: sum(m[field] for m in metadata) for key, field in
              (('input', 'input_tokens'), ('output', 'output_tokens'), ('total', 'total_tokens'))}
    start = selected[0][0] if selected else None
    end = max(row[1] for row in selected) if selected else None
    scenario_meta, run_metadata = scenario_meta or {}, run_metadata or {}
    workflow = {
        'trace_metadata': {
            'schema_version': SCHEMA_VERSION, 'run_id_path': str(destination), 'framework': 'OJW',
            'UC': scenario_meta.get('uc', 'unknown_UC'), 'scenario': scenario_meta.get('scenario', 'hierarchical_workflow'),
            'run_nr': run_nr, 'model': model, 'system_architecture': 'hierarchical',
            'system_architecture_description': scenario_meta.get('system_architecture_description', 'Hierarchical specialist invocations.'),
            'timestamp_start': start.isoformat() if start else None, 'timestamp_end': end.isoformat() if end else None,
            'duration_seconds': round((end-start).total_seconds(), 4) if start and end else None,
            'research_topic': run_metadata.get('research_topic'), 'dataset': run_metadata.get('dataset'), 'index_num': run_metadata.get('index_num'),
            'total_tokens': tokens['total'], 'total_input_tokens': tokens['input'], 'total_output_tokens': tokens['output'],
            'number_of_agents': len(order), 'number_of_tools': len(tools), 'agent_sequence': order, 'tool_sequence': tools, 'status': status,
        },
        'team_configuration': {'team': 'MagenticOneGroupChat', 'provided_agent_names': names,
                               'invoked_agent_names': list(dict.fromkeys(order)), 'agent_invocation_count': len(order)},
        'workflow_summary': {'orchestrator_task': task, 'magneticOneOrchestrator_actions_count': len(order),
                             'agent_execution_order': order, 'llm_call_count': sum(m['llm_call_count'] for m in metadata),
                             'tool_call_count': sum(m['tool_call_count'] for m in metadata), 'tokens': tokens},
        'agents': agents,
    }
    HierarchicalWorkflowTraceV2.model_validate(workflow)
    skipped = [name for name in names if name not in counts]
    if skipped:
        artifact = {'schema_version': SCHEMA_VERSION, 'skipped_agents_metadata': {
            'total_agents_provided': len(names), 'total_agents_invoked': len(counts), 'total_agents_skipped': len(skipped),
            'agents': [{'agent_name': name, 'reason': 'orchestrator_decision',
                        'note': 'Not invoked by Magentic-One orchestrator in this run'} for name in skipped]}}
        SkippedAgentsArtifact.model_validate(artifact)
        documents[f'OrchestratorObj_skipped_agents_{run_nr}.json'] = artifact
    documents[destination.name] = workflow
    # Validate and check every destination before writing anything.
    for filename in documents:
        target = directory / filename
        if target in paths or target.exists():
            raise ValueError(f'Output already exists or would overwrite an input: {target}')
    directory.mkdir(parents=True, exist_ok=True)
    for filename, document in documents.items():
        with (directory / filename).open('x', encoding='utf-8') as stream:
            json.dump(document, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
    return destination
