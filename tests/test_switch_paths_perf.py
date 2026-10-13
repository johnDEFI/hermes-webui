"""Hermetic regression tests; also runnable with stdlib unittest (no server).

Extract actual functions with AST, as existing source-harness tests do, to avoid
import-time home discovery and the integration suite's server fixture.
"""
import ast
import importlib.util
import json
import logging
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, mock_open, patch

ROOT = Path(__file__).resolve().parents[1]


def functions(path, names, namespace=None, source=None):
    namespace = dict(namespace or {})
    namespace.update(Path=Path, threading=threading, time=time, os=os,
                     json=json, sqlite3=sqlite3, closing=closing,
                     logger=logging.getLogger(__name__))
    tree = ast.parse(source or (ROOT / path).read_text(encoding='utf-8'))
    nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name in names]
    for node in nodes:
        node.decorator_list = []
    code = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), *nodes], type_ignores=[])
    exec(compile(ast.fix_missing_locations(code), path, 'exec'), namespace)
    return namespace


def agent_reader():
    spec = importlib.util.spec_from_file_location('switch_test_reader', ROOT / 'api/agent_sessions.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def database(home, sessions=3, messages=3):
    home.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(home / 'state.db')) as db, db:
        db.executescript('''CREATE TABLE sessions (
            id TEXT PRIMARY KEY, title TEXT, model TEXT, message_count INTEGER,
            started_at REAL, source TEXT, parent_session_id TEXT, ended_at REAL,
            end_reason TEXT, project_id TEXT);
            CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT,
                timestamp REAL, role TEXT, content TEXT);
            CREATE INDEX idx_messages_session ON messages(session_id, timestamp);
        ''')
        db.executemany('INSERT INTO sessions VALUES (?,?,?,?,?,?,NULL,NULL,NULL,NULL)',
                       [(f's{i}', f'Title {i}', 'model', messages, i+1, 'discord') for i in range(sessions)])
        db.executemany('INSERT INTO messages(session_id,timestamp,role,content) VALUES (?,?,?,?)',
                       [(f's{i}', j+100, 'user' if j % 2 == 0 else 'assistant', 'hello')
                        for i in range(sessions) for j in range(messages)])


def model_namespace(home):
    reader = agent_reader()
    ns = dict(CLAUDE_CODE_SOURCE='claude_code', CLI_VISIBLE_SESSION_LIMIT=20,
              PROJECT_ASSIGNED_CLI_LIMIT=20, CRON_PROJECT_CHIP_LIMIT=20,
              WEBHOOK_PROJECT_CHIP_LIMIT=20, KANBAN_PROJECT_CHIP_LIMIT=20,
              BACKGROUND_CLI_SOURCES=('cron', 'webhook', 'kanban'),
              UNASSIGNED_CLI_REFILL_SCAN_CEILING=200, UNASSIGNED_CLI_REFILL_MAX_QUERIES=4,
              SESSION_DIR=home / 'sidecars',
              read_importable_agent_session_rows=reader.read_importable_agent_session_rows,
              read_assigned_project_row_counts=reader.read_assigned_project_row_counts,
              normalize_agent_session_source=reader.normalize_agent_session_source,
              is_cli_session_row=reader.is_cli_session_row,
              _load_webui_deleted_session_tombstone=lambda: set(),
              get_last_workspace=lambda **kw: home,
              _state_projection_sidecar_metadata=lambda sid: {},
              profile_scoped_project_ids=lambda profile: frozenset(),
              _state_db_supports_project_ids=lambda path: False,
              get_claude_code_sessions=lambda: [],
              is_cron_session=lambda sid, source: source == 'cron',
              is_webhook_session=lambda sid, source: source == 'webhook',
              cast=lambda typ, value: value)
    return functions('api/models.py', {'_load_cli_sessions_uncached', 'get_cli_session_metadata',
                     '_sqlite_file_stat_cache_key', '_sqlite_content_fingerprint', '_path_stat_cache_key'}, ns)


def profile_modules(home):
    profile = types.ModuleType('api.profiles')
    profile.get_active_profile_name = lambda: 'default'
    profile.get_hermes_home_for_profile = lambda name: home
    config = types.ModuleType('api.config')
    config.STREAMS = {}
    config.STREAMS_LOCK = threading.Lock()
    config.reload_config = lambda: None
    config.DEFAULT_WORKSPACE = home
    yaml = types.ModuleType('api.yaml_compat')
    yaml.safe_load = lambda text: {}
    api = types.ModuleType('api')
    api.yaml_compat = yaml
    return {'api': api, 'api.profiles': profile, 'api.config': config, 'api.yaml_compat': yaml}


class SwitchPathsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        database(self.home)

    def test_direct_metadata_equals_list_without_list_call(self):
        ns = model_namespace(self.home)
        expected = ns['_load_cli_sessions_uncached'](self.home, self.home/'state.db', 'default', include_claude_code=False)
        ns['get_cli_sessions'] = Mock(side_effect=AssertionError('list called'))
        ns['get_claude_code_sessions'] = Mock(side_effect=AssertionError('Claude scan'))
        with patch.dict(sys.modules, profile_modules(self.home)):
            actual = ns['get_cli_session_metadata']('s1', profile='default')
        self.assertEqual(actual, next(row for row in expected if row['session_id'] == 's1'))

    def test_lineage_projection_parity_and_root_fallback(self):
        with closing(sqlite3.connect(self.home/'state.db')) as db, db:
            db.execute("UPDATE sessions SET end_reason='compression', ended_at=2 WHERE id='s0'")
            db.execute("UPDATE sessions SET parent_session_id='s0', started_at=3 WHERE id='s1'")
            db.execute("UPDATE messages SET timestamp=1000 WHERE session_id='s1'")
        ns = model_namespace(self.home)
        full = ns['_load_cli_sessions_uncached'](self.home, self.home/'state.db', 'default', include_claude_code=False)
        with patch.dict(sys.modules, profile_modules(self.home)):
            self.assertEqual(ns['get_cli_session_metadata']('s1'), next(row for row in full if row['session_id']=='s1'))
            self.assertEqual(ns['get_cli_session_metadata']('s0'), {})

    def test_renamed_root_alias_uses_active_identity(self):
        ns=model_namespace(self.home)
        modules=profile_modules(self.home)
        modules['api.profiles'].get_active_profile_name=lambda:'renamed-root'
        with patch.dict(sys.modules, modules):
            self.assertEqual(ns['get_cli_session_metadata']('s1', profile='default')['profile'], 'renamed-root')

    def test_metadata_fallback_and_profile_gate(self):
        modules = profile_modules(self.home)
        model = types.ModuleType('api.models')
        model.get_cli_session_metadata = Mock(return_value={})
        modules['api.models'] = model
        expected = {'session_id':'s1', 'profile':'other'}
        lookup = Mock(side_effect=[{}, expected])
        ns = functions('api/routes.py', {'_resolve_cli_import_metadata'},
                       dict(_lookup_cli_session_metadata=lookup, _profiles_match=lambda a,b:a==b,
                            _get_active_profile_name=lambda:'default'))
        with patch.dict(sys.modules, modules):
            self.assertEqual(ns['_resolve_cli_import_metadata']('s1', requested_profile='other', allow_all_profiles=True), expected)
            model.get_cli_session_metadata.assert_called_once()
            model.get_cli_session_metadata.reset_mock()
            lookup.reset_mock(side_effect=True)
            lookup.return_value = expected
            self.assertEqual(ns['_resolve_cli_import_metadata']('s1', requested_profile='other'), expected)
            model.get_cli_session_metadata.assert_not_called()

    def test_lightweight_preserves_default_response(self):
        listing = Mock(return_value=[{'name':'default'}])
        ns = functions('api/profiles.py', {'switch_profile'},
                       dict(_active_profile='default', _profile_lock=threading.Lock(),
                            _is_isolated_profile_mode=lambda:False, _is_root_profile=lambda n:n=='default',
                            _DEFAULT_HERMES_HOME=self.home, list_profiles_api=listing))
        with patch.dict(sys.modules, profile_modules(self.home)):
            light = ns['switch_profile']('default', process_wide=False, lightweight=True)
            listing.assert_not_called()
            normal = ns['switch_profile']('default', process_wide=False)
        self.assertEqual(normal.pop('profiles'), [{'name':'default'}])
        self.assertEqual(light, normal)

    def test_unchanged_import_skips_read_and_updates_invalidate(self):
        modules = profile_modules(self.home)
        models = model_namespace(self.home)
        module = types.ModuleType('api.models')
        module._sqlite_file_stat_cache_key = models['_sqlite_file_stat_cache_key']
        modules['api.models'] = module
        sidecars=self.home/'sidecars'
        sidecars.mkdir()
        (sidecars/'s1.json').write_text('{}', encoding='utf-8')
        session=types.SimpleNamespace(profile='default', messages=[{'role':'user', 'content':'hello'}],
                                     source_tag='discord', raw_source='discord', read_only=False,
                                     save=Mock(), compact=lambda:{'session_id':'s1'})
        read=Mock(return_value=list(session.messages))
        ns=functions('api/routes.py', {'_handle_session_import_cli', '_cli_import_refresh_stamp'},
                     dict(SESSION_DIR=sidecars, Session=types.SimpleNamespace(load=lambda sid:session),
                          require=lambda *args:None, _normalize_import_profile_value=lambda value:value,
                          _request_wants_all_profiles_import=lambda body:False,
                          _session_visible_to_active_profile=lambda *args:True,
                          _resolve_cli_import_metadata=lambda *args,**kw:{},
                          _is_cli_tool_metadata_enrichment=lambda *args:False,
                          _is_subagent_child_session_id=lambda sid:False,
                          get_cli_session_messages=read, _CLI_IMPORT_REFRESH_CACHE={},
                          _CLI_IMPORT_REFRESH_LOCK=threading.Lock(),
                          public_session_projection=lambda value:value,
                          j=lambda handler,value:value))
        with patch.dict(sys.modules, modules):
            first=ns['_handle_session_import_cli'](None, {'session_id':'s1'})
            second=ns['_handle_session_import_cli'](None, {'session_id':'s1'})
            self.assertEqual(first, second)
            self.assertEqual(read.call_count, 1)
            with closing(sqlite3.connect(self.home/'state.db')) as db, db:
                db.execute("UPDATE messages SET content='changed' WHERE session_id='s1'")
            ns['_handle_session_import_cli'](None, {'session_id':'s1'})
            self.assertEqual(read.call_count, 2)
            (sidecars/'s1.json').write_text('{"title":"changed"}', encoding='utf-8')
            ns['_handle_session_import_cli'](None, {'session_id':'s1'})
            self.assertEqual(read.call_count, 3)
            ns['_CLI_IMPORT_REFRESH_CACHE'].clear()
            ns['_cli_import_refresh_stamp']=lambda *args:None
            ns['_handle_session_import_cli'](None, {'session_id':'s1'})
            ns['_handle_session_import_cli'](None, {'session_id':'s1'})
            self.assertEqual(read.call_count, 5)
        session.save.assert_not_called()

    def test_client_promises_and_deferred_refresh(self):
        subprocess.run(['node', str(ROOT/'tests/switch_paths_client.cjs')], check=True, cwd=ROOT)

    def test_skill_stats_reads_only_first_4000_characters(self):
        skills_dir = self.home / 'skills'
        skills_dir.mkdir()
        skill_md = skills_dir / 'example' / 'SKILL.md'
        content = '---\nname: example\n---\n' + '\u00e9' * 5000
        skill_utils = types.ModuleType('agent.skill_utils')
        skill_utils.iter_skill_index_files = Mock(return_value=[skill_md])
        skill_utils.parse_frontmatter = Mock(return_value=({'name': 'example'}, ''))
        skill_utils.skill_matches_platform = Mock(return_value=True)
        agent = types.ModuleType('agent')
        agent.skill_utils = skill_utils
        ns = functions('api/profiles.py', {'_compute_profile_skills_stats'})
        opening = mock_open(read_data=content)
        with patch.dict(sys.modules, {'agent': agent, 'agent.skill_utils': skill_utils}), \
                patch.object(Path, 'open', opening):
            self.assertEqual(ns['_compute_profile_skills_stats'](self.home), (1, 1))
        opening.assert_called_once_with(encoding='utf-8')
        opening().read.assert_called_once_with(4000)
        skill_utils.parse_frontmatter.assert_called_once_with(content[:4000])
        skill_utils.skill_matches_platform.assert_called_once_with({'name': 'example'})

    def test_watcher_reuse_and_config_change(self):
        created = []
        class Watcher:
            def __init__(inner, *, profile_name, hermes_home):
                inner._hermes_home=hermes_home
                inner._config_identity=ns['_watcher_config_identity'](hermes_home)
                inner.alive=True
                inner.stopped=False
                created.append(inner)
            def start(inner): pass
            def is_alive(inner): return inner.alive
            def stop(inner): inner.stopped=True
        ns = functions('api/gateway_watcher.py', {'restart_watcher_for_profile', '_watcher_config_identity'},
                       dict(_watchers={}, _watcher_lock=threading.Lock(), GatewayWatcher=Watcher,
                            _watcher_registry_key=lambda name,home:str(home), _pop_idle_watchers_locked=lambda **kw:[]))
        with patch.dict(sys.modules, profile_modules(self.home)):
            first=ns['restart_watcher_for_profile']('default', reuse_if_unchanged=True)
            self.assertIs(ns['restart_watcher_for_profile']('default', reuse_if_unchanged=True), first)
            (self.home/'config.yaml').write_text('model: changed', encoding='utf-8')
            self.assertIsNot(ns['restart_watcher_for_profile']('default', reuse_if_unchanged=True), first)
            self.assertTrue(first.stopped)
            dead=created[-1]
            dead.alive=False
            self.assertIsNot(ns['restart_watcher_for_profile']('default', reuse_if_unchanged=True), dead)


if __name__ == '__main__':
    unittest.main()
