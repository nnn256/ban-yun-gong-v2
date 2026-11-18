import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import workbench_server as server


class LocalSetupTests(unittest.TestCase):
    def runtime(self, directory):
        project = Path(directory)
        root = project / 'runtime/AstrBot'
        (root / 'data/plugins').mkdir(parents=True)
        (root / '.astrbot').touch()
        shutil.copytree(server.ROOT / 'vendor', project / 'vendor')
        return project, root

    def test_fresh_setup_installs_real_connector_and_adds_onebot_without_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            project, root = self.runtime(directory)
            with patch.object(server, 'ROOT', project), patch.object(server, 'astrbot_root', return_value=root):
                result = server.prepare_local_setup()
            self.assertTrue(result['requires_restart'])
            self.assertTrue((root / 'data/plugins/pulids_snowluma_go_to_astrbot/main.py').is_file())
            self.assertTrue((root / 'data/plugins/pulids_snowluma_go_to_astrbot/LICENSE').is_file())
            config = json.loads((root / 'data/cmd_config.json').read_text(encoding='utf-8'))
            self.assertEqual(config['platform'][0], {'id': 'qq_bridge', 'type': 'aiocqhttp', 'enable': True,
                                                     'ws_reverse_host': '127.0.0.1', 'ws_reverse_port': 6199, 'ws_reverse_token': ''})
            snow = Path(result['snowluma_root'])
            network = json.loads((snow / 'config/onebot.json').read_text(encoding='utf-8'))
            self.assertEqual(network['networks']['wsClients'][0]['url'], 'ws://127.0.0.1:6199/ws')
            self.assertNotIn('accessToken', network['networks']['wsClients'][0])
            self.assertEqual(json.loads(server.rules_path(root).read_text(encoding='utf-8'))['rules'], [])
            self.assertFalse(list(project.rglob('credentials.json')))

    def test_repeat_setup_preserves_platform_options_and_existing_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            project, root = self.runtime(directory)
            with patch.object(server, 'ROOT', project), patch.object(server, 'astrbot_root', return_value=root):
                first = server.prepare_local_setup()
                config_path = root / 'data/cmd_config.json'
                config = json.loads(config_path.read_text(encoding='utf-8'))
                config['platform'].append({'id': 'another-project', 'type': 'other'})
                config['provider'] = [{'name': 'preserved'}]
                config_path.write_text(json.dumps(config), encoding='utf-8')
                rules = server.rules_path(root)
                rules.write_text('{"rules":[{"name":"keep existing"}]}', encoding='utf-8')
                before = {path: path.read_bytes() for path in [config_path, rules, Path(first['snowluma_root']) / 'config/onebot.json']}
                second = server.prepare_local_setup()
            self.assertFalse(second['requires_restart'])
            for path, content in before.items():
                self.assertEqual(path.read_bytes(), content)

    def test_corrupt_vendor_package_is_rejected_before_connector_install(self):
        with tempfile.TemporaryDirectory() as directory:
            project, root = self.runtime(directory)
            (project / 'vendor/snowluma-go-1.0.9.zip').write_bytes(b'bad bundle')
            with patch.object(server, 'ROOT', project), patch.object(server, 'astrbot_root', return_value=root):
                with self.assertRaisesRegex(ValueError, '校验失败'):
                    server.prepare_local_setup()
            self.assertFalse((root / 'data/plugins/pulids_snowluma_go_to_astrbot').exists())

    def test_upgrade_enables_owned_self_reports_and_requests_restart_without_changing_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            project, root = self.runtime(directory)
            with patch.object(server, 'ROOT', project), patch.object(server, 'astrbot_root', return_value=root):
                first = server.prepare_local_setup()
                path = Path(first['snowluma_root']) / 'config/onebot.json'
                config = json.loads(path.read_text(encoding='utf-8'))
                owned = config['networks']['wsClients'][0]
                owned['reportSelfMessage'] = False
                unrelated = {'name': 'other-project', 'url': 'ws://localhost:9000', 'reportSelfMessage': False}
                config['networks']['wsClients'].append(unrelated)
                path.write_text(json.dumps(config), encoding='utf-8')
                rules = server.rules_path(root)
                rules.write_bytes(b'{"rules":[{"include_self":false}]}')
                original = rules.read_bytes()
                second = server.prepare_local_setup()
                self.assertTrue(second['requires_restart'])
                updated = json.loads(path.read_text(encoding='utf-8'))
                self.assertTrue(updated['networks']['wsClients'][0]['reportSelfMessage'])
                self.assertEqual(updated['networks']['wsClients'][1], unrelated)
                self.assertEqual(rules.read_bytes(), original)
                self.assertFalse(server.prepare_local_setup()['requires_restart'])

    def test_account_snapshot_cannot_shadow_the_managed_self_report_setting(self):
        with tempfile.TemporaryDirectory() as directory:
            project, root = self.runtime(directory)
            with patch.object(server, 'ROOT', project), patch.object(server, 'astrbot_root', return_value=root):
                first = server.prepare_local_setup()
                config_dir = Path(first['snowluma_root']) / 'config'
                path = config_dir / 'onebot_123456.json'
                external = {'name': 'astrbot', 'url': 'ws://127.0.0.1:6199/ws/', 'reportSelfMessage': False}
                owned = {'name': 'mover-v2-astrbot', 'url': 'ws://127.0.0.1:6199/ws', 'reportSelfMessage': False}
                config = {'mode': 'snapshot', 'networks': {'wsClients': [external, owned]}}
                path.write_text(json.dumps(config), encoding='utf-8')
                self.assertTrue(server.prepare_local_setup()['requires_restart'])
                updated = json.loads(path.read_text(encoding='utf-8'))
                self.assertEqual(updated['mode'], 'snapshot')
                self.assertEqual(updated['networks']['wsClients'], [external, {**owned, 'reportSelfMessage': True}])
                self.assertEqual(len(list(config_dir.glob('onebot_123456.pre-self-report-*.json'))), 1)
                self.assertFalse(server.prepare_local_setup()['requires_restart'])

    def test_invalid_existing_platform_configuration_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            project, root = self.runtime(directory)
            config = root / 'data/cmd_config.json'
            config.write_text('{"platform":"invalid"}', encoding='utf-8')
            with patch.object(server, 'ROOT', project), patch.object(server, 'astrbot_root', return_value=root):
                with self.assertRaisesRegex(ValueError, '保留原文件'):
                    server.prepare_local_setup()
            self.assertEqual(config.read_text(encoding='utf-8'), '{"platform":"invalid"}')


if __name__ == '__main__':
    unittest.main()
