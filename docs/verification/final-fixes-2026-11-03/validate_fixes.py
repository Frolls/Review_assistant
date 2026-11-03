"""Execute only inspected synthetic repairs, in an isolated read-only container.

All Python repairs must match a reference AST or one explicitly inspected
alternative below before execution. Ansible tasks must match a bounded edit
of their synthetic input. No arbitrary product input is executed here.
"""
import ast
import copy
import json
from pathlib import Path
import re
import subprocess

import yaml

ROOT = Path('/results')
report = {'python': [], 'ansible': []}

def python_behavior(family, code):
    namespace = {}
    exec(compile(code, 'inspected-synthetic-fixture', 'exec'), namespace)
    if family == 'event_queue':
        f = namespace['enqueue']
        a, b = f('a'), f('b')
        assert a == ['a'] and b == ['b'] and a is not b
        supplied = []
        assert f('c', queue=supplied) is supplied and supplied == ['c']
        return ['independent defaults', 'explicit argument identity']
    if family == 'cache_map':
        f = namespace['record']
        a, b = f('a'), f('b')
        assert a == {'a': True} and b == {'b': True} and a is not b
        supplied = {}
        assert f('c', cache=supplied) is supplied and supplied == {'c': True}
        return ['independent defaults', 'explicit argument identity']
    if family == 'json_import':
        f = namespace['decode_config']
        assert f('{"ready": true}') == {'ready': True}
        assert f('[1, 2]') == [1, 2]
        return ['JSON object', 'JSON array', 'no NameError']
    if family == 'math_import':
        f = namespace['root']
        assert f(0) == 0 and f(81) == 9 and f(2.25) == 1.5
        return ['three inputs', 'no NameError']
    if family in ('finally_override', 'cleanup_finally'):
        events = []
        f = namespace['reciprocal'] if family == 'finally_override' else lambda x: namespace['divide'](x, events)
        assert f(4) == (.25 if family == 'finally_override' else 2.5)
        try:
            f(0)
        except ZeroDivisionError:
            pass
        else:
            raise AssertionError('Exception suppressed')
        if family == 'cleanup_finally':
            assert events == ['done', 'done']
        return ['correct result', 'exception propagation', 'cleanup preserved']
    if family == 'mean_contract':
        f = namespace['average']
        for values, expected in [([2, 8], 5), ([7], 7), ([-6, 0, 12], 2), ([1, 2, 3, 4], 2.5)]:
            assert f(values) == expected
        return ['task example', 'three additional behavioral inputs']
    if family == 'temperature':
        f = namespace['fahrenheit']
        for value, expected in [(0, 32), (10, 50), (-10, 14), (-40, -40), (100, 212)]:
            assert f(value) == expected
        return ['three task examples', 'two additional behavioral inputs']
    raise AssertionError(family)


def run_ansible(tasks, name):
    playbook = [{'hosts': 'localhost', 'connection': 'local', 'gather_facts': False,
                 'vars': {'ansible_python_interpreter': '/usr/bin/python3',
                          'deploy_password': 'SYNTHETIC_SECRET_731', 'service_token': 'SYNTHETIC_SECRET_731'},
                 'tasks': tasks}]
    path = Path('/tmp') / (name + '.json')
    path.write_text(json.dumps(playbook))
    result = subprocess.run(['ansible-playbook', '-i', 'localhost,', str(path)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    return {'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}

for dataset in ('regression-v12', 'fresh'):
    for row in json.loads((ROOT / (dataset + '-raw.json')).read_text()):
        if not row['expected_defects']:
            continue
        blocks = re.findall(r'```(?:python|yaml)\s*\n(.*?)```', row['answer'], re.S)
        assert len(blocks) == 1, row['id']
        code = blocks[0]
        family = row['id'].rsplit('-', 1)[0]
        if row['language'] == 'python':
            allowed = [row['reference_fix']]
            if family == 'temperature':
                allowed.append('def fahrenheit(celsius):\n    return celsius * 9 / 5 + 32\n')
            assert ast.dump(ast.parse(code)) in [ast.dump(ast.parse(ref)) for ref in allowed], row['id']
            checks = python_behavior(family, code)
            report['python'].append({'dataset': dataset, 'case': row['id'], 'passed': checks})
        else:
            original = yaml.safe_load(row['code'])
            expected = copy.deepcopy(original)
            if family in ('archive_directory', 'cache_directory'):
                directory = '/tmp/review-acceptance-archive' if family == 'archive_directory' else '/tmp/review-final-cache'
                expected[0].setdefault('args', {})['creates'] = directory
                assert yaml.safe_load(code) == expected
                runs = [run_ansible(expected, family), run_ansible(expected, family)]
                for i, run in enumerate(runs):
                    assert re.search(r'changed=' + ('1' if i == 0 else '0') + r'\b', run['stdout'])
                checks = ['first run creates directory', 'second run succeeds with zero changes', 'no duplicated cmd']
            elif family in ('nested_secret', 'override_secret'):
                expected[0]['block'][0]['no_log'] = True
                assert yaml.safe_load(code) == expected
                runs = [run_ansible(original, family + '-original'), run_ansible(expected, family + '-fixed')]
                assert 'SYNTHETIC_SECRET_731' in runs[0]['stdout']
                assert 'SYNTHETIC_SECRET_731' not in runs[1]['stdout']
                checks = ['original exposes synthetic secret', 'fixed output censors secret', 'block/task precedence']
            else:
                raise AssertionError(family)
            report['ansible'].append({'dataset': dataset, 'case': row['id'], 'passed': checks, 'runs': runs})
print(json.dumps(report, ensure_ascii=False, indent=2))
