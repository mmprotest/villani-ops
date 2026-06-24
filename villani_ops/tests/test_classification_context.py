from villani_ops.classification.context import collect_relevant_file_snippets
from villani_ops.classification.classifier import TaskClassifier
from villani_ops.core.backend import Backend
from villani_ops.core.task import Task
from villani_ops.llm.client import LLMCallResult
import json


def test_collect_relevant_file_snippets_includes_test_and_source(tmp_path):
    repo=tmp_path
    (repo/'src/pkg').mkdir(parents=True)
    (repo/'tests').mkdir()
    (repo/'src/pkg/foo.py').write_text('def foo():\n    return 1\n')
    (repo/'tests/test_foo.py').write_text('from pkg.foo import foo\ndef test_foo(): assert foo() == 2\n')
    tree=['src/pkg/foo.py','tests/test_foo.py']
    snippets=collect_relevant_file_snippets(repo, 'Fix failing tests in tests/test_foo.py', tree)
    paths=[s.path for s in snippets]
    assert 'tests/test_foo.py' in paths
    assert 'src/pkg/foo.py' in paths


def test_collect_relevant_file_snippets_skips_generated_cache(tmp_path):
    repo=tmp_path
    (repo/'src/pkg/__pycache__').mkdir(parents=True)
    (repo/'.pytest_cache').mkdir()
    (repo/'src/pkg/__pycache__/foo.pyc').write_bytes(b'\0bad')
    (repo/'.pytest_cache/README.md').write_text('cache')
    tree=['src/pkg/__pycache__/foo.pyc','.pytest_cache/README.md']
    snippets=collect_relevant_file_snippets(repo, 'Fix failing tests for foo.pyc', tree)
    assert snippets == []


def test_classifier_context_includes_relevant_file_snippets(tmp_path):
    repo=tmp_path
    (repo/'src/pkg').mkdir(parents=True)
    (repo/'tests').mkdir()
    (repo/'src/pkg/foo.py').write_text('def foo(): return 1\n')
    (repo/'tests/test_foo.py').write_text('def test_foo(): assert False\n')
    class Client:
        def complete_json(self, backend, system, user, schema):
            data=json.loads(user.split('\n\n')[-1])
            paths=[x['path'] for x in data['relevant_files']]
            assert 'tests/test_foo.py' in paths
            return LLMCallResult(parsed_json={'difficulty':'medium','risk':'medium','category':'bug_fix','confidence':.9}, raw_text='{}', backend_name='cls', model='cls')
    backend=Backend(name='cls', provider='openai-compatible', base_url='http://x', model='m', api_key='x', roles=['classification'])
    cls,_=TaskClassifier(Client()).classify(Task(repo_path=str(repo), objective='Fix failing tests in tests/test_foo.py'), {'cls':backend})
    assert 'tests/test_foo.py' in cls.relevant_file_paths
