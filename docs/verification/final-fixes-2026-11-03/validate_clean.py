import json
from pathlib import Path

rows = json.loads(Path('/results/corrected-clean-cases.json').read_text())
report = []
for row in rows:
    namespace = {}
    exec(compile(row['code'], 'authored-clean-fixture', 'exec'), namespace)
    if row['id'].startswith('event_queue'):
        f = namespace['enqueue']
        a, b = f('a'), f('b')
        assert a == ['a'] and b == ['b'] and a is not b
        supplied = []
        assert f('c', queue=supplied) is supplied and supplied == ['c']
    else:
        f = namespace['record']
        a, b = f('a'), f('b')
        assert a == {'a': True} and b == {'b': True} and a is not b
        supplied = {}
        assert f('c', cache=supplied) is supplied and supplied == {'c': True}
    report.append({'case': row['id'], 'passed': ['independent defaults', 'explicit argument accepted and mutated in place']})
print(json.dumps(report, indent=2))
