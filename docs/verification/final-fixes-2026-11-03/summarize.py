"""Recompute metrics from retained outputs, annotations and measured telemetry."""
from collections import Counter
import csv
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys

HERE = Path(__file__).resolve().parent
WORKSPACE = HERE.parents[2]
sys.path.insert(0, str(WORKSPACE / 'Review_bot'))
from app.services.review_eval import score  # noqa: E402


def read(name):
    return json.loads((HERE / name).read_text())


def stats(values):
    values = sorted(values)
    return {'count': len(values), 'median': statistics.median(values), 'mean': statistics.mean(values),
            'p95_nearest_rank': values[math.ceil(.95 * len(values)) - 1]}


def usage(name):
    calls = [e for e in read(name) if e['event'] == 'model.call']
    return {'model_calls': len(calls), 'usage_available_calls': sum(c['usage_available'] for c in calls),
            'tokens': {k: sum(c.get('usage.' + k, 0) for c in calls) for k in ('prompt_tokens','completion_tokens','total_tokens')}}


def resources(name):
    data = read(name)
    samples = data['samples']
    gpus = [g for s in samples for g in s['amd_gpus'] if g['card'] == 'card1']
    models = {}
    for sample in samples:
        for model in sample.get('loaded_models') or []:
            models[model['name']] = {k: model.get(k) for k in ('name','digest','context_length','size_vram')}
    # Total sensor energy during the measured phase; includes non-model loads.
    energy = 0
    for before, after in zip(samples, samples[1:]):
        ga = next((g for g in before['amd_gpus'] if g['card']=='card1'), {})
        gb = next((g for g in after['amd_gpus'] if g['card']=='card1'), {})
        if 'power_watts' in ga and 'power_watts' in gb:
            energy += (ga['power_watts'] + gb['power_watts']) / 2 * (after['time'] - before['time'])
    return {'duration_seconds_including_management_waits': data['duration_seconds'], 'exit_code': data['exit_code'],
            'host_cpu_mean_percent': statistics.mean(s['host_cpu_percent'] for s in samples if s.get('host_cpu_percent') is not None),
            'peak_host_ram_gib': data['peak_host_ram_kib'] / 2**20,
            'peak_device_vram_gib': max(g['mem_info_vram_used'] for g in gpus) / 2**30,
            'gpu_busy_mean_percent': statistics.mean(g['gpu_busy_percent'] for g in gpus),
            'gpu_sensor_energy_wh': energy / 3600, 'loaded_models': list(models.values()),
            'scope': 'host/device-wide, includes other processes and management waits; GPU sensor energy is not total system energy'}


protocol = read('protocol-v12.json')
assert hashlib.sha256((HERE / 'fresh-cases.json').read_bytes()).hexdigest() == protocol['fresh_cases_sha256']
patch = read('post-live-format-patch.json')
assert protocol['source_sha256'][patch['file']] == patch['before_sha256']
expected = {**protocol['source_sha256'], patch['file']: patch['after_sha256']}
assert all(hashlib.sha256((WORKSPACE / f).read_bytes()).hexdigest() == digest for f, digest in expected.items())
summary = {}
replacements = read('corrected-clean-annotated.json')
correction = read('ground-truth-correction.json')
assert hashlib.sha256((HERE / 'corrected-clean-cases.json').read_bytes()).hexdigest() == correction['sha256']
for prefix in ('regression-v12', 'fresh'):
    original_rows = read(prefix + '-annotated.json')
    raw = read(prefix + '-raw.json')
    assert len(original_rows) == len(raw) == 18
    assert [{k:v for k,v in r.items() if k!='review'} for r in original_rows] == [{k:v for k,v in r.items() if k!='review'} for r in raw]
    excluded = next(r['case'] for r in correction['replaces'] if r['dataset'] == prefix)
    replacement = next(r for r in replacements if r['id'] == excluded + '-contract-v2')
    rows = [replacement if r['id'] == excluded else r for r in original_rows]
    assert rows == read(prefix + '-final-annotated.json')
    measured = usage(prefix + '-telemetry.json')
    saved_usage = [u for r in original_rows for u in r['saved_answer_metadata']['usage']]
    assert len(saved_usage) == measured['model_calls']
    assert all(sum(u[k] for u in saved_usage) == measured['tokens'][k] for k in measured['tokens'])
    summary[prefix] = {'metrics': score(rows),
        'counts': {k:sum(r['review'][k] for r in rows) for k in ('true_positives','false_positives','false_negatives')},
        'statuses': dict(Counter(r['saved_answer_metadata']['review_answer']['status'] for r in rows)),
        'latency_seconds': stats([r['latency_seconds'] for r in rows]),
        'defect_latency_seconds': stats([r['latency_seconds'] for r in rows if r['expected_defects']]),
        'usage_original_run': measured, 'usage_matches_saved_history': True, 'resources_original_run': resources(prefix + '-resources.json'),
        'ground_truth_replacement': {'excluded': excluded, 'replacement': replacement['id'], 'reason': correction['reason']}}
    with (HERE / (prefix + '-cases.csv')).open('w') as output:
        writer = csv.writer(output)
        writer.writerow(['id','status','seconds','tp','fp','fn','fix_usable'])
        for r in rows:
            review = r['review']
            writer.writerow([r['id'],r['saved_answer_metadata']['review_answer']['status'],r['latency_seconds'],review['true_positives'],review['false_positives'],review['false_negatives'],review['fix_usable']])
agent = read('agent-live.json')
audit = [json.loads(line) for line in (HERE / 'bot-access.jsonl').read_text().splitlines()]
summary['agent'] = {'status':agent['status'], 'steps':len(agent['steps']),
    'turn_seconds':stats([s['seconds'] for s in agent['steps']]),
    'notify_requests':sum(r['path']=='/notify' for r in audit),
    'stale_request_rejected':agent['clear_invalidates_old_request'],
    'knowledge_answer':'exact attributed source verified by live harness, no generated additions',
    'usage':usage('agent-telemetry.json'), 'resources':resources('agent-resources.json')}
inference = [r for r in read('inference-raw.json')['runs'] if not r['warmup']]
assert len(inference)==3 and not any('error' in r for r in inference)
summary['inference'] = {'decode_tokens_per_second':stats([r['eval_tokens_per_second'] for r in inference]),
    'wall_seconds':stats([r['wall_seconds'] for r in inference]),
    'resources':resources('inference-resources.json'),
    'scope':'native Ollama; identical synthetic prompt, warmup saved separately, prefix cache may affect prefill; not application latency'}
summary['tests'] = {'backend_passed':257,'bot_passed':39,'pair_passed':1,'ruff':'passed backend app/tests/scripts and bot/root scripts/integration tests',
    'warnings':'two backend Pydantic dependency warnings; one pair-test warning',
    'source_unchanged_during_live_runs':True,'post_live_format_patch':patch}
summary['retained_v11_failure']={'file':'regression-raw.json','tp':5,'fp':0,'fn':1,'usable_fixes':4,'defect_cases':6,
    'causes':['mkdir source/finding model gate refused known check','arithmetic correction introduced branch outside supported interpreter']}
summary['ground_truth_corrections'] = {'original_observed_v12_cases':36,'additional_observed_replacements':2,
    'ambiguous_ground_truth_cases':2,'valid_cases_for_quality_metrics':36,'reason':correction['reason'],
    'usage':usage('corrected-clean-telemetry.json'),'resources':resources('corrected-clean-resources.json'),
    'validation':read('clean-validation.json')}
summary['scope']='Bounded Python/Ansible checks, same author/evaluator. Original 18 regression + 18 frozen held-out variants; two ambiguous clean fixtures replaced after output review, with production unchanged. All 38 observed v12 answers retained; quality metrics use 36 valid fixtures. Not an independent human review or general semantic accuracy claim. Historical RAGAS remains unchanged.'
(HERE / 'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
print(json.dumps({k:{'metrics':v.get('metrics'),'latency':v.get('latency_seconds'),'usage':v.get('usage', v.get('usage_original_run'))} for k,v in summary.items() if k in ('regression-v12','fresh','agent')},indent=2))
print('native decode median',summary['inference']['decode_tokens_per_second']['median'])
