import os
import select
import sys
import threading
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet

PANEL_DIR = Path(__file__).resolve().parents[1] / "panel"
sys.path.insert(0, str(PANEL_DIR))
import app as panel_app
from bot_registry import empty_registry, save_registry, set_bot_credentials
import terminal as terminal_module
from terminal import (
    consume_terminal_token,
    close_terminal_session,
    issue_terminal_token,
    is_same_origin,
    parse_terminal_message,
    _start_shell,
    _stop_shell,
    _write_terminal_input,
)


class BotRegistryApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.original_base_dir = panel_app.BASE_DIR
        self.original_registry_path = panel_app.BOT_REGISTRY_PATH
        self.original_credentials_key = panel_app.BOT_CREDENTIALS_KEY
        panel_app.BASE_DIR = str(self.root)
        panel_app.BOT_REGISTRY_PATH = str(self.root / "bot-registry.json")
        panel_app.BOT_CREDENTIALS_KEY = Fernet.generate_key().decode("ascii")
        self.addCleanup(setattr, panel_app, "BASE_DIR", self.original_base_dir)
        self.addCleanup(setattr, panel_app, "BOT_REGISTRY_PATH", self.original_registry_path)
        self.addCleanup(setattr, panel_app, "BOT_CREDENTIALS_KEY", self.original_credentials_key)
        panel_app.app.config["TESTING"] = True
        self.client = panel_app.app.test_client()
        with panel_app.BOT_INSTALL_JOBS_LOCK:
            original_jobs = dict(panel_app.BOT_INSTALL_JOBS)
            panel_app.BOT_INSTALL_JOBS.clear()
        self.addCleanup(self._restore_jobs, original_jobs)
        original_inputs = dict(panel_app.BOT_INSTALL_PAYLOADS)
        panel_app.BOT_INSTALL_PAYLOADS.clear()
        self.addCleanup(setattr, panel_app, "BOT_INSTALL_PAYLOADS", original_inputs)
        with self.client.session_transaction() as session:
            session["logged_in"] = True

    @staticmethod
    def _restore_jobs(jobs):
        with panel_app.BOT_INSTALL_JOBS_LOCK:
            panel_app.BOT_INSTALL_JOBS.clear()
            panel_app.BOT_INSTALL_JOBS.update(jobs)

    def add_bot(self):
        definition = {
            "id": "a" * 32,
            "name": "Example Bot",
            "repository": "https://github.com/example/telegram-bot.git",
            "ref": "main",
            "entrypoint": "src/bot.py",
            "enabled": False,
        }
        registry = empty_registry()
        registry["bots"].append(definition)
        return registry, definition

    def test_status_includes_available_ram_in_gigabytes(self):
        memory = SimpleNamespace(
            used=8 * 1024**3,
            total=16 * 1024**3,
            available=6 * 1024**3,
            percent=50.0,
        )
        disk = SimpleNamespace(
            percent=50.0,
            used=100 * 1024**3,
            total=200 * 1024**3,
            free=100 * 1024**3,
        )
        with patch.object(panel_app.psutil, "virtual_memory", return_value=memory), patch.object(
            panel_app.psutil, "disk_usage", return_value=disk
        ), patch.object(panel_app.psutil, "cpu_percent", return_value=10.0), patch.object(
            panel_app, "get_panel_url", return_value="Unavailable"
        ), patch.object(panel_app, "get_ssh_cmd", return_value="Unavailable"), patch.object(
            panel_app, "get_bot_statuses", return_value={}
        ):
            response = self.client.get("/api/status")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["system"]["ram_available_gb"], 6.0)

    def test_memory_card_shows_available_ram_instead_of_duplicate_utilization_meter(self):
        response = self.client.get("/")
        page = response.get_data(as_text=True)

        self.assertIn('id="ram-available"', page)
        self.assertNotIn('id="ram-meter"', page)
        self.assertIn("system.ram_available_gb", page)

    def test_metric_charts_show_percent_axes_and_minute_time_rails(self):
        response = self.client.get("/")
        page = response.get_data(as_text=True)

        self.assertIn('id="cpu-chart" data-scale-step="25" data-window-seconds="180"', page)
        self.assertIn('id="ram-chart" data-scale-step="25" data-window-seconds="60"', page)
        self.assertIn('id="disk-chart" data-scale-step="25" data-window-seconds="60"', page)
        self.assertIn('class="chart-y-axis"', page)
        self.assertIn('class="chart-time-rail"', page)
        self.assertIn("--chart-grid-color: #263237;", page)
        self.assertIn(".chart-y-axis, .chart-time-rail { color:var(--chart-grid-color);", page)
        self.assertIn(".chart-grid { stroke:var(--chart-grid-color);", page)
        self.assertIn(".metric:not(.primary) .chart { height:150px; margin-top:auto; }", page)
        self.assertIn("margin:8px 0; padding-top:0; border-top:0;", page)
        self.assertIn("metrics: { cpu: [], ram: [], disk: [], maxPoints: 61 }", page)

    def test_ssh_copy_button_is_centered_and_has_a_copy_icon(self):
        response = self.client.get("/")
        page = response.get_data(as_text=True)

        self.assertIn(
            '<div class="section-kicker" id="ssh-title">SSH to the server</div>\n      <div class="utility">',
            page,
        )
        self.assertIn("#ssh-title { margin-top:22px; margin-bottom:8px; }", page)
        self.assertIn(
            ".utility { display:grid; grid-template-columns:1fr auto; gap:1px; margin-top:0;",
            page,
        )
        self.assertIn(".utility-copy-block { display:flex; align-items:center; }", page)
        self.assertIn(
            '<div class="utility-block utility-copy-block"><button class="btn btn-compact" id="copy-ssh-btn" type="button"><span data-lucide="copy"></span><span>Copy command</span></button></div>',
            page,
        )

    def test_clear_logs_truncates_only_the_selected_log_file(self):
        selected_log = self.root / "panel.log"
        other_log = self.root / "hermes.log"
        selected_log.write_text("first line\nsecond line\n", encoding="utf-8")
        other_log.write_text("keep this log\n", encoding="utf-8")

        with patch.dict(
            panel_app.SERVICE_LOG_PATHS,
            {"panel": str(selected_log), "hermes": str(other_log)},
        ):
            response = self.client.post("/api/logs/panel/clear")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(selected_log.read_text(encoding="utf-8"), "")
        self.assertEqual(other_log.read_text(encoding="utf-8"), "keep this log\n")

    def test_clear_logs_rejects_an_unknown_target(self):
        response = self.client.post("/api/logs/not-a-target/clear")

        self.assertEqual(response.status_code, 404)

    def test_logs_toolbar_exposes_and_wires_the_clear_action(self):
        response = self.client.get("/")
        page = response.get_data(as_text=True)

        self.assertIn('id="clear-logs"', page)
        self.assertIn("`/api/logs/${encodeURIComponent(key)}/clear`", page)
        self.assertIn("$('clear-logs').addEventListener('click', clearSelectedLogs)", page)

    def test_file_search_returns_matching_files_and_folders_below_current_path(self):
        current = self.root / "workspace"
        nested = current / "nested"
        match_folder = current / "Needle Folder"
        nested.mkdir(parents=True)
        match_folder.mkdir()
        root_match = current / "needle.txt"
        nested_match = nested / "needles.py"
        outside_match = self.root / "outside-needle.txt"
        root_match.write_text("root", encoding="utf-8")
        nested_match.write_text("nested", encoding="utf-8")
        outside_match.write_text("outside", encoding="utf-8")

        response = self.client.get(
            "/api/files/search",
            query_string={"path": str(current), "q": "needle"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            {entry["path"] for entry in response.json["entries"]},
            {str(root_match), str(nested_match), str(match_folder)},
        )
        self.assertFalse(response.json["truncated"])

    def test_file_listing_marks_symlink_entries_for_recursive_search(self):
        current = self.root / "workspace"
        current.mkdir()
        (current / "file.txt").write_text("content", encoding="utf-8")

        response = self.client.get("/api/files", query_string={"path": str(current)})

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json["entries"][0]["is_symlink"])

    def test_file_search_marks_recursive_results_for_older_frontends(self):
        current = self.root / "workspace"
        (current / "nested").mkdir(parents=True)

        response = self.client.get(
            "/api/files/search",
            query_string={"path": str(current), "q": "needle"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["recursive"])

    def test_file_search_falls_back_to_recursive_listing_for_older_backends(self):
        response = self.client.get("/")
        page = response.get_data(as_text=True)

        self.assertIn("data.recursive === true", page)
        self.assertIn("collectFileSearchEntries(rootPath, query, requestId)", page)

    def test_file_search_returns_duplicate_filenames_with_distinct_relative_paths(self):
        current = self.root / "workspace"
        bot_directories = ("orbit-alerts", "market-watch", "docs-sync", "queue-worker")
        for directory in bot_directories:
            bot_directory = current / "bots" / directory
            bot_directory.mkdir(parents=True)
            (bot_directory / "bot.py").write_text("pass\n", encoding="utf-8")

        response = self.client.get(
            "/api/files/search",
            query_string={"path": str(current), "q": "bot.py"},
        )

        self.assertEqual(response.status_code, 200)
        entries = response.json["entries"]
        self.assertEqual(len(entries), 4)
        self.assertEqual({entry["name"] for entry in entries}, {"bot.py"})
        self.assertEqual(
            {entry["relative_path"] for entry in entries},
            {f"bots/{directory}/bot.py" for directory in bot_directories},
        )

    def test_file_search_results_render_path_as_a_separate_subtitle(self):
        response = self.client.get("/")
        page = response.get_data(as_text=True)

        self.assertIn("class: 'file-item-path'", page)
        self.assertIn("class: 'file-item-name', text: entry.name", page)
        self.assertIn("function getFileEntrySubtitle(entry, searchResult)", page)

    def test_delete_file_explorer_entry_removes_file_or_folder(self):
        current = self.root / "workspace"
        current.mkdir()
        file_path = current / "remove.txt"
        folder_path = current / "remove-folder"
        nested_path = folder_path / "nested.txt"
        file_path.write_text("remove", encoding="utf-8")
        folder_path.mkdir()
        nested_path.write_text("remove", encoding="utf-8")

        for target in (file_path, nested_path, folder_path):
            response = self.client.post(
                "/api/files/delete",
                json={"path": str(target), "current_path": str(current)},
            )
            self.assertEqual(response.status_code, 200)
            self.assertFalse(target.exists())

    def test_file_explorer_delete_uses_a_styled_accessible_dialog(self):
        response = self.client.get("/")
        page = response.get_data(as_text=True)

        self.assertIn('id="file-delete-dialog"', page)
        self.assertIn('aria-labelledby="file-delete-title"', page)
        self.assertIn('id="file-delete-description"', page)
        self.assertIn('id="file-delete-cancel"', page)
        self.assertIn('id="file-delete-confirm"', page)
        self.assertNotIn("if (!confirm(`Delete ${entry.name}", page)

    def test_bot_delete_dialog_has_destructive_action_styling_and_icons(self):
        response = self.client.get("/")
        page = response.get_data(as_text=True)

        self.assertIn('class="bot-delete-dialog-icon"><span data-lucide="trash"></span></span>', page)
        self.assertIn('class="bot-delete-dialog-kicker">Permanent action</p>', page)
        self.assertIn('<dialog aria-describedby="bot-delete-description bot-delete-warning" aria-labelledby="bot-delete-title" class="bot-delete-dialog" id="bot-delete-dialog" role="alertdialog">', page)
        self.assertIn('id="bot-delete-title">Permanently delete this bot?</h2>', page)
        self.assertIn('id="bot-delete-description"></p>', page)
        self.assertIn('id="bot-delete-warning">This action cannot be undone.</p>', page)
        self.assertIn('autofocus class="btn" id="bot-delete-cancel" type="button">Cancel</button>', page)
        self.assertIn('id="bot-delete-confirm" type="submit"><span data-lucide="trash"></span><span id="bot-delete-confirm-label">Delete bot</span>', page)
        self.assertIn("$('bot-delete-title').textContent = `Permanently delete ${botName}?`;", page)
        self.assertIn("$('bot-delete-confirm-label').textContent = `Delete ${botName}`;", page)
        self.assertIn(".btn-delete-bot { min-height:36px;", page)
        self.assertIn("background:#a83f3b", page)

    def test_bot_update_uses_a_themed_confirmation_dialog(self):
        response = self.client.get("/")
        page = response.get_data(as_text=True)

        self.assertIn('<dialog aria-describedby="bot-update-description bot-update-state" aria-labelledby="bot-update-title" class="bot-update-dialog" id="bot-update-dialog" role="dialog">', page)
        self.assertIn('data-lucide="refreshCw"', page)
        self.assertIn('id="bot-update-confirm-label">Update bot</span>', page)
        self.assertIn("This bot is running. It will stop during the update and restart if the update succeeds.", page)
        self.assertIn("This bot is stopped. It will stay stopped after a successful update.", page)
        self.assertIn("$('bot-update-form').addEventListener('submit', confirmUpdateBot)", page)
        self.assertNotIn("if (bot.running && !confirm(", page)

    def test_file_preview_includes_line_number_gutter(self):
        response = self.client.get("/")
        page = response.get_data(as_text=True)

        self.assertIn('id="file-preview-lines"', page)
        self.assertIn('id="file-preview-content"', page)
        self.assertIn("function renderFilePreview(content, path, activate = true)", page)
        self.assertIn("lines.map((_, index) => index + 1).join('\\n')", page)

    def test_file_preview_has_a_filename_tab(self):
        response = self.client.get("/")
        page = response.get_data(as_text=True)

        self.assertIn("class: 'file-preview-tab'", page)
        self.assertIn("function renderFilePreview(content, path, activate = true)", page)
        self.assertIn("class: 'file-preview-name'", page)

    def test_file_preview_supports_multiple_closable_tabs(self):
        response = self.client.get("/")
        page = response.get_data(as_text=True)

        self.assertIn('role="tablist" aria-label="Open files" id="file-preview-tabs"', page)
        self.assertIn("function renderFileTabs()", page)
        self.assertIn("function closeFilePreview(path)", page)
        self.assertIn("'data-file-preview-close': preview.path", page)
        self.assertIn("'aria-label': `Close ${fileName}`", page)
        self.assertIn("event.key === 'ArrowRight'", page)

    def test_file_preview_remains_visible_when_files_tab_is_reloaded(self):
        response = self.client.get("/")
        page = response.get_data(as_text=True)
        load_files = page.split("async function loadFiles(", 1)[1].split(
            "\nasync function collectFileSearchEntries", 1
        )[0]

        self.assertIn("renderActiveFilePreview();", load_files)
        self.assertNotIn("$('file-preview-box').hidden = true;", load_files)

    def test_connection_indicators_reflect_live_health_and_pulse_by_state(self):
        response = self.client.get("/")
        page = response.get_data(as_text=True)
        refresh_status = page.split("async function refreshStatus()", 1)[1].split(
            "\n/* 5. Console tabs", 1
        )[0]

        self.assertIn('id="rail-connection-status"', page)
        self.assertIn("function setConnectionState(status, hint = '')", page)
        self.assertIn("['rail-connection-status', 'rail-connection-label']", page)
        self.assertNotIn("setConnectionState('busy')", refresh_status)
        self.assertIn("setConnectionState('connected')", refresh_status)
        self.assertIn("setConnectionState('error'", refresh_status)
        self.assertIn("state.logs.health = 'busy'", page)
        self.assertIn("state.logs.health = 'connected'", page)
        self.assertIn("state.logs.health = 'error'", page)
        self.assertIn("setModelStatus('connected'", page)
        self.assertIn("setModelStatus('error'", page)
        self.assertIn(".connection[data-state=connected]", page)
        self.assertIn(".connection[data-state=busy]", page)
        self.assertIn(".connection[data-state=error]", page)
        self.assertIn("@keyframes status-pulse", page)
        self.assertIn("@media (prefers-reduced-motion: reduce)", page)

    def test_collapsed_assistant_providers_button_sits_at_rail_bottom_with_sliders_icon(self):
        response = self.client.get("/")
        page = response.get_data(as_text=True)

        self.assertIn("--assistant-bottom-rail:64px;", page)
        self.assertIn(".assistant-sidebar-footer { margin-top:auto; flex:0 0 var(--assistant-bottom-rail);", page)
        self.assertIn(".assistant-composer { min-height:var(--assistant-bottom-rail);", page)
        self.assertIn(".assistant-sidebar.collapsed .assistant-sidebar-footer { justify-content:center;", page)
        self.assertIn('id="rail-providers-btn" title="Providers" type="button"><span data-lucide="slidersHorizontal">', page)
        self.assertIn("slidersHorizontal:", page)

    def test_provider_settings_panel_uses_transitionable_visibility(self):
        response = self.client.get("/")
        page = response.get_data(as_text=True)

        self.assertIn(".assistant-settings-panel { max-height:0;", page)
        self.assertIn("transition:max-height .28s ease", page)
        self.assertIn(".assistant-sidebar.settings-open .assistant-settings-panel { max-height:480px;", page)
        self.assertIn(".assistant-settings-panel { max-height:0; padding:0 11px;", page)
        self.assertIn("@media (prefers-reduced-motion: reduce)", page)

    def test_providers_button_toggles_settings_panel_open_and_closed(self):
        response = self.client.get("/")
        page = response.get_data(as_text=True)
        sidebar_handlers = page.split("// Assistant: sidebar", 1)[1].split(
            "// Assistant: provider settings", 1
        )[0]

        self.assertIn("$('providers-btn').addEventListener('click', () => {", sidebar_handlers)
        self.assertIn("setProviderPanelOpen(!sidebar().classList.contains('settings-open'));", sidebar_handlers)

    def test_terminal_is_the_fifth_operations_tab(self):
        response = self.client.get("/")
        page = response.get_data(as_text=True)

        self.assertTrue(
            '<button class="tab-btn" data-tab="terminal" type="button"><span class="tab-icon">05</span><span class="tab-span">Terminal</span></button>'
            in page,
            "expected the Terminal item in the Operations navigation",
        )
        self.assertTrue('<div hidden id="tab-terminal">' in page, "expected the Terminal panel")
        self.assertIn('id="terminal-clear"', page)
        self.assertIn('id="terminal-copy"', page)
        self.assertIn('data-lucide="keyboard"', page)
        self.assertIn('data-lucide="trash"', page)
        self.assertIn('data-lucide="copy"', page)
        self.assertIn("const TERMINAL_PROMPT = 'runner@runnervm48cm3:~$';", page)
        self.assertIn("$('terminal-clear').addEventListener('click', clearTerminalHistory);", page)
        self.assertIn("$('terminal-copy').addEventListener('click', copyTerminalContents);", page)
        self.assertNotIn('id="terminal-connect"', page)
        self.assertIn(".terminal-toolbar #terminal-interrupt { display:none; }", page)
        mobile_styles = page.split("@media (max-width:760px)", 1)[1].split(
            "@media (max-width:520px)", 1
        )[0]
        self.assertIn(
            ".terminal-toolbar #terminal-interrupt { display:inline-flex; }",
            mobile_styles,
        )
        self.assertTrue(
            "const TAB_IDS = ['assistant', 'logs', 'env', 'files', 'terminal'];" in page,
            "expected terminal to be registered as a tab",
        )
        self.assertIn(
            "if (data.supported === false) throw new Error('Interactive terminals require a Linux host.');",
            page,
        )

    def test_terminal_token_is_only_available_to_authenticated_sessions(self):
        response = self.client.get("/api/terminal/token")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["token"])
        self.assertEqual(response.json["supported"], os.name == "posix")

        unauthenticated_client = panel_app.app.test_client()
        response = unauthenticated_client.get("/api/terminal/token")
        self.assertEqual(response.status_code, 401)

    def test_terminal_tokens_are_session_bound_and_single_use(self):
        token = issue_terminal_token("browser-session")

        self.assertFalse(consume_terminal_token(token, "different-session"))
        self.assertTrue(consume_terminal_token(token, "browser-session"))
        self.assertFalse(consume_terminal_token(token, "browser-session"))

    def test_logging_out_stops_the_active_terminal_session(self):
        terminal_session_id = "logout-session"
        stop_event = threading.Event()
        with patch.dict(
            terminal_module._active_terminal_sessions,
            {terminal_session_id: stop_event},
            clear=True,
        ):
            with self.client.session_transaction() as session:
                session["terminal_session_id"] = terminal_session_id

            response = self.client.get("/logout")

        self.assertEqual(response.status_code, 302)
        self.assertTrue(stop_event.is_set())

    def test_terminal_websocket_requires_a_same_origin(self):
        self.assertTrue(is_same_origin("https://panel.example", "panel.example"))
        self.assertFalse(is_same_origin("https://attacker.example", "panel.example"))
        self.assertFalse(is_same_origin("null", "panel.example"))
        self.assertFalse(is_same_origin("https://panel.example/path", "panel.example"))

    def test_terminal_protocol_accepts_input_and_bounded_dimensions(self):
        self.assertEqual(
            parse_terminal_message('{"type":"input","data":"ls\\r"}'),
            ("input", "ls\r"),
        )
        self.assertEqual(
            parse_terminal_message('{"type":"resize","cols":240,"rows":100}'),
            ("resize", 200, 80),
        )

    def test_terminal_protocol_rejects_malformed_or_oversized_input(self):
        for message in (
            "not json",
            '{"type":"input","data":7}',
            '{"type":"resize","cols":true,"rows":24}',
            "x" * 65537,
        ):
            with self.subTest(message=message[:24]):
                with self.assertRaises(ValueError):
                    parse_terminal_message(message)

    @unittest.skipUnless(os.name == "posix", "interactive PTY requires a POSIX host")
    def test_terminal_pty_runs_input_and_returns_shell_output(self):
        pid, master_fd = _start_shell()
        stopped = threading.Event()
        try:
            _write_terminal_input(master_fd, "printf '\\n__panel_terminal_smoke__\\n'\n")
            output = bytearray()
            deadline = time.monotonic() + 3
            while b"__panel_terminal_smoke__" not in output and time.monotonic() < deadline:
                readable, _, _ = select.select([master_fd], [], [], 0.1)
                if readable:
                    output.extend(os.read(master_fd, 4096))

            self.assertIn(b"__panel_terminal_smoke__", output)
        finally:
            _stop_shell(pid, master_fd, stopped)

    def test_fetched_model_list_escapes_provider_settings_clipping(self):
        response = self.client.get("/")
        page = response.get_data(as_text=True)

        self.assertIn(".assistant-sidebar.model-list-open { overflow:visible; }", page)
        self.assertIn(".assistant-sidebar.model-list-open .assistant-settings-panel { overflow:visible; }", page)
        self.assertIn("sidebar().classList.add('model-list-open')", page)
        self.assertIn("sidebar().classList.remove('model-list-open')", page)

    def test_provider_model_dropdown_icon_tracks_open_state(self):
        response = self.client.get("/")
        page = response.get_data(as_text=True)

        self.assertIn(".model-combo-toggle .lucide-icon { width:14px; height:14px; stroke-linecap:round;", page)
        self.assertIn(".model-combo.open .model-combo-toggle .lucide-icon { transform:rotate(180deg); }", page)
        self.assertIn("combo.classList.add('open')", page)
        self.assertIn("combo.classList.remove('open')", page)

    def test_provider_fields_reveal_long_values_on_hover_and_focus(self):
        response = self.client.get("/")
        page = response.get_data(as_text=True)

        self.assertIn("function revealProviderFieldText(field)", page)
        self.assertIn("const PROVIDER_FIELD_SCROLL_SPEED = 24;", page)
        self.assertIn("requestAnimationFrame(animate)", page)
        self.assertIn("field.scrollTo({ left: 0, behavior });", page)
        self.assertIn("field.addEventListener('mouseenter', reveal)", page)
        self.assertIn("field.addEventListener('focus', reveal)", page)
        self.assertIn("field.addEventListener('mouseleave', () => resetProviderFieldText(field, true))", page)
        self.assertIn("field.addEventListener('blur', reset)", page)
        self.assertIn("field.title = field.selectedOptions[0]?.textContent?.trim() || '';", page)

    def test_provider_select_uses_the_model_picker_chevron(self):
        response = self.client.get("/")
        page = response.get_data(as_text=True)

        self.assertIn('<div class="provider-select-wrap"><select aria-label="Provider" class="field" id="assistant-provider-select"></select><span aria-hidden="true" class="provider-select-chevron"><span data-lucide="chevronDown"></span></span></div>', page)
        self.assertIn(".provider-select-wrap .field { appearance:none;", page)
        self.assertIn(".provider-select-chevron .lucide-icon { stroke-linecap:round; stroke-linejoin:round; }", page)

    def test_assistant_chat_list_renders_newest_sessions_first(self):
        response = self.client.get("/")
        page = response.get_data(as_text=True)

        self.assertIn("const newestFirst = [...sessions].reverse();", page)
        self.assertIn("...newestFirst.map(session =>", page)

    def test_assistant_chat_history_actions_are_revealed_on_hover_and_focus(self):
        response = self.client.get("/")
        page = response.get_data(as_text=True)

        self.assertIn(".session-actions { display:flex; gap:1px; opacity:0; visibility:hidden;", page)
        self.assertIn(".assistant-session-item:hover .session-actions", page)
        self.assertIn(".assistant-session-item:focus-within .session-actions", page)
        self.assertIn("@media (hover:none)", page)

    def test_assistant_actions_fade_and_chat_uses_vazirmatn_with_auto_direction(self):
        response = self.client.get("/")
        page = response.get_data(as_text=True)

        self.assertIn("family=Vazirmatn", page)
        self.assertIn('--assistant-font: "Vazirmatn", var(--sans);', page)
        self.assertIn(".assistant-chat { height:620px;", page)
        self.assertIn("font-family:var(--assistant-font);", page)
        self.assertIn(".assistant-chat .message-code-block, .assistant-chat .message-code-inline { font-family:var(--mono); }", page)
        self.assertIn("transition:opacity .16s ease,visibility 0s linear .16s;", page)
        self.assertIn(".assistant-message-group:hover .assistant-message-action", page)
        self.assertIn("@media (prefers-reduced-motion: reduce)", page)
        self.assertIn(".assistant-message-action { transition:none; }", page)
        self.assertIn("class: `assistant-message ${message.role}`, dir: 'auto'", page)
        self.assertIn('<textarea aria-label="Message Hermes" class="assistant-input field" dir="auto"', page)
        self.assertIn("h('p', { dir: 'auto' })", page)

    def test_assistant_send_button_is_icon_only_and_inside_composer_input(self):
        response = self.client.get("/")
        page = response.get_data(as_text=True)

        self.assertIn('<div class="assistant-input-wrap"><textarea aria-label="Message Hermes"', page)
        self.assertIn('id="assistant-input" placeholder="Ask Hermes to inspect or manage the server…" rows="2"></textarea><button aria-label="Send message" class="btn btn-start assistant-send" id="assistant-send" title="Send message" type="button"><span data-lucide="sendHorizontal"></span></button></div></div>', page)
        self.assertIn("sendHorizontal:", page)
        self.assertIn(".assistant-composer { min-height:var(--assistant-bottom-rail); max-height:200px; flex:0 0 auto; display:flex; align-items:center;", page)
        self.assertIn("border-top:0", page)
        self.assertIn(".assistant-input-wrap { flex:1; min-width:0; max-height:184px; display:flex; align-items:center;", page)
        self.assertIn("height:34px; min-height:34px; max-height:174px", page)
        self.assertIn("assistantInput.addEventListener('input', resizeAssistantInput)", page)
        self.assertIn("input.style.height = `${Math.max(34, Math.min(input.scrollHeight, 174))}px`", page)
        self.assertIn(".assistant-send { flex:0 0 30px;", page)
        self.assertNotIn("position:absolute; right:7px; bottom:7px", page)
        self.assertIn("button.setAttribute('aria-label', busy ? 'Stop response' : 'Send message')", page)

    def test_bot_grid_empty_cells_use_page_background(self):
        response = self.client.get("/")
        page = response.get_data(as_text=True)

        self.assertIn(".bot-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); gap:1px; background:var(--bg); }", page)

    def test_bot_cards_show_pid_uptime_and_repository_for_empty_and_configured_fleets(self):
        response = self.client.get("/")
        page = response.get_data(as_text=True)
        render_cards = page.split("function renderBotCards()", 1)[1].split(
            "\nfunction dismissInstallJob", 1
        )[0]

        self.assertIn("h('div', { class: 'stat-label', text: 'PID' })", render_cards)
        self.assertIn("h('div', { class: 'stat-label', text: 'UPTIME' })", render_cards)
        self.assertIn("h('div', { class: 'stat-label', text: 'REPO' })", render_cards)
        self.assertIn("displayBotRepository(bot.repository)", render_cards)
        self.assertIn("data-bot-field': 'repository'", render_cards)
        self.assertIn("function displayBotRepository(repository)", page)
        self.assertIn("'https://github.com/'", page)
        self.assertNotIn("'data-bot-field': 'restarts'", render_cards)

    def test_provider_handlers_do_not_reference_removed_settings_status(self):
        response = self.client.get("/")
        page = response.get_data(as_text=True)

        self.assertNotIn("assistant-settings-status", page)

    def test_top_actions_use_lucide_icons_for_redeploy_and_logout(self):
        response = self.client.get("/")
        page = response.get_data(as_text=True)

        self.assertIn('aria-label="Redeploy server" class="btn btn-restart" id="redeploy-btn" type="button"><span data-lucide="rotateCw"></span><span>Redeploy</span>', page)
        self.assertIn('aria-label="Log out" href="/logout" class="btn btn-delete"><span data-lucide="logOut"></span><span>Logout</span>', page)
        self.assertIn("logOut:", page)

    def test_mobile_controls_keep_top_actions_visible_and_fit_their_content(self):
        response = self.client.get("/")
        page = response.get_data(as_text=True)

        self.assertIn(".top-actions .btn { display:inline-flex; width:32px; min-width:32px;", page)
        self.assertIn(".top-actions .btn > span:last-child { display:none; }", page)
        self.assertIn(".bot-stats { grid-template-columns:minmax(0,1fr) minmax(0,1fr) minmax(0,2fr); }", page)
        self.assertIn(".assistant-model-fetch-row .model-combo { flex:1 1 auto; }", page)
        self.assertIn(".assistant-model-fetch-row #assistant-fetch-btn { flex:0 0 auto; }", page)
        self.assertIn('id="assistant-fetch-btn" type="button"><span data-lucide="refreshCw"></span><span>Fetch</span>', page)
        self.assertIn(".section-header #toggle-add-bot { flex:0 0 auto; width:max-content; white-space:nowrap; }", page)

    def test_mobile_logs_hide_line_count_and_visually_hide_status_text(self):
        response = self.client.get("/")
        page = response.get_data(as_text=True)

        self.assertIn(".console-toolbar #log-count { display:none; }", page)
        self.assertIn(".console-toolbar #log-state-text {", page)
        self.assertIn("clip:rect(0,0,0,0);", page)
        self.assertIn(".log-state-dot, .model-status-dot { width:6px; height:6px;", page)

    def test_mobile_file_toolbar_separates_path_from_navigation_and_search(self):
        response = self.client.get("/")
        page = response.get_data(as_text=True)

        self.assertIn(".file-toolbar {\n    display:grid;\n    grid-template-columns:auto minmax(0,1fr);", page)
        self.assertIn(".file-toolbar #current-file-path {\n    grid-column:1 / -1;", page)
        self.assertIn("text-overflow:ellipsis;", page)
        self.assertIn('aria-label="Go to parent directory"', page)
        self.assertIn('class="file-parent-mobile-label">Up</span>', page)
        self.assertIn('class="file-parent-desktop-label">Parent directory</span>', page)
        self.assertIn('placeholder="Search files…"', page)
        self.assertIn("$('current-file-path').title = `Path: ${data.current_path}`;", page)

    def test_delete_file_explorer_entry_rejects_paths_outside_current_directory(self):
        current = self.root / "workspace"
        current.mkdir()
        sibling = self.root / "keep.txt"
        sibling.write_text("keep", encoding="utf-8")

        response = self.client.post(
            "/api/files/delete",
            json={"path": str(sibling), "current_path": str(current)},
        )

        self.assertEqual(response.status_code, 403)
        self.assertTrue(sibling.exists())

    def test_status_reports_empty_fleet_without_default_bots(self):
        with (
            patch.object(panel_app.psutil, "cpu_percent", return_value=0),
            patch.object(panel_app.psutil, "virtual_memory", return_value=SimpleNamespace(
                used=0, total=1, available=1, percent=0
            )),
            patch.object(panel_app.psutil, "disk_usage", return_value=SimpleNamespace(
                percent=25,
                used=2 * 1024**3,
                total=8 * 1024**3,
                free=6 * 1024**3,
            )),
            patch.object(panel_app, "get_panel_url", return_value="unavailable"),
            patch.object(panel_app, "get_ssh_cmd", return_value="unavailable"),
        ):
            response = self.client.get("/api/status")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["system"]["disk_used_gb"], 2)
        self.assertEqual(response.json["system"]["disk_total_gb"], 8)
        self.assertEqual(response.json["system"]["disk_free_gb"], 6)
        self.assertEqual(response.json["fleet"], {"online": 0, "total": 0})
        self.assertEqual(response.json["bots"], {})

    def test_bot_status_reports_error_when_enabled_process_has_exited(self):
        _, definition = self.add_bot()
        definition["enabled"] = True

        with patch.object(panel_app, "get_bot_proc", return_value=None):
            status = panel_app.get_bot_statuses({"bots": [definition]})[definition["id"]]

        self.assertFalse(status["running"])
        self.assertTrue(status["error"])

    def test_bot_status_does_not_report_error_when_intentionally_stopped(self):
        _, definition = self.add_bot()

        with patch.object(panel_app, "get_bot_proc", return_value=None):
            status = panel_app.get_bot_statuses({"bots": [definition]})[definition["id"]]

        self.assertFalse(status["running"])
        self.assertFalse(status["error"])
        self.assertEqual(status["repository"], definition["repository"])

    def test_environment_parser_ignores_full_line_comments(self):
        environment = panel_app.parse_environment_assignments(
            "# credentials for the bot\nBOT_TOKEN = one two\n   # ignored too\nOTHER = value"
        )

        self.assertEqual(environment, {"BOT_TOKEN": "one two", "OTHER": "value"})

    def test_repository_metadata_returns_branches_and_default_branch_files(self):
        responses = [
            Mock(status_code=200, json=Mock(return_value={"default_branch": "main"})),
            Mock(status_code=200, json=Mock(return_value=[{"name": "main"}, {"name": "feature/ui"}])),
            Mock(status_code=200, json=Mock(return_value={
                "truncated": False,
                "tree": [
                    {"path": "src", "type": "tree"},
                    {"path": "src/bot.py", "type": "blob"},
                    {"path": "README.md", "type": "blob"},
                ],
            })),
        ]
        with patch.object(panel_app.requests, "get", side_effect=responses) as github_get:
            response = self.client.post(
                "/api/github/repository-metadata",
                json={"repository": "https://github.com/example/bot.git", "pat": "synthetic-pat"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["default_branch"], "main")
        self.assertEqual(response.json["branches"], ["main", "feature/ui"])
        self.assertEqual(response.json["files"], ["src/bot.py", "README.md"])
        self.assertFalse(response.json["files_truncated"])
        self.assertEqual(github_get.call_count, 3)
        self.assertEqual(github_get.call_args_list[0].kwargs["headers"]["Authorization"], "Bearer synthetic-pat")
        self.assertNotIn("synthetic-pat", response.get_data(as_text=True))

    def test_repository_metadata_uses_saved_pat_for_editing_bot(self):
        registry, definition = self.add_bot()
        set_bot_credentials(
            registry,
            definition["id"],
            {"pat": "synthetic-saved-pat", "environment": {"BOT_TOKEN": "synthetic-bot-token"}},
            panel_app.BOT_CREDENTIALS_KEY,
        )
        save_registry(panel_app.BOT_REGISTRY_PATH, registry)
        responses = [
            Mock(status_code=200, json=Mock(return_value={"default_branch": "main"})),
            Mock(status_code=200, json=Mock(return_value=[{"name": "main"}])),
            Mock(status_code=200, json=Mock(return_value={"truncated": False, "tree": [{"path": "src/bot.py", "type": "blob"}]})),
        ]
        with patch.object(panel_app.requests, "get", side_effect=responses) as github_get:
            response = self.client.post(
                "/api/github/repository-metadata",
                json={
                    "repository": definition["repository"],
                    "bot_id": definition["id"],
                    "pat": "",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(github_get.call_args_list[0].kwargs["headers"]["Authorization"], "Bearer synthetic-saved-pat")
        self.assertNotIn("synthetic-saved-pat", response.get_data(as_text=True))
        self.assertNotIn("synthetic-bot-token", response.get_data(as_text=True))

    def test_repository_metadata_rejects_non_github_hosts(self):
        with patch.object(panel_app.requests, "get") as github_get:
            response = self.client.post(
                "/api/github/repository-metadata",
                json={"repository": "https://example.com/org/repo", "pat": "synthetic-pat"},
            )

        self.assertEqual(response.status_code, 400)
        github_get.assert_not_called()

    def test_repository_metadata_loads_files_for_selected_branch(self):
        responses = [
            Mock(status_code=200, json=Mock(return_value={"default_branch": "main"})),
            Mock(status_code=200, json=Mock(return_value={
                "truncated": False,
                "tree": [{"path": "src/feature_bot.py", "type": "blob"}],
            })),
        ]
        with patch.object(panel_app.requests, "get", side_effect=responses) as github_get:
            response = self.client.post(
                "/api/github/repository-metadata",
                json={
                    "repository": "https://github.com/example/bot.git",
                    "ref": "feature/ui",
                    "include_branches": False,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["ref"], "feature/ui")
        self.assertEqual(response.json["branches"], [])
        self.assertEqual(response.json["files"], ["src/feature_bot.py"])
        self.assertEqual(github_get.call_count, 2)
        self.assertIn("/git/trees/feature/ui", github_get.call_args_list[1].args[0])

    def test_environment_endpoint_returns_env_values_but_not_pat(self):
        registry, definition = self.add_bot()
        set_bot_credentials(
            registry,
            definition["id"],
            {
                "pat": "synthetic-private-token",
                "environment": {"BOT_TOKEN": "synthetic-bot-token"},
            },
            panel_app.BOT_CREDENTIALS_KEY,
        )
        save_registry(panel_app.BOT_REGISTRY_PATH, registry)

        response = self.client.get(f"/api/env/{definition['id']}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["env"], {"BOT_TOKEN": "synthetic-bot-token"})
        self.assertNotIn("synthetic-private-token", response.get_data(as_text=True))

    def test_server_secrets_environment_lists_requested_keys_when_unset(self):
        expected_keys = {
            "SERVER_USERNAME",
            "SERVER_PASSWORD",
            "HERMES_API_SERVER_KEY",
            "HERMES_TELEGRAM_BOT_TOKEN",
            "STATUS_BOT_TOKEN",
            "OWNER_ID",
        }
        with patch.dict(panel_app.os.environ, {"SERVER_USERNAME": "panel-user"}, clear=True):
            response = self.client.get("/api/env/server-secrets")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(expected_keys.issubset(response.json["env"]))
        self.assertEqual(set(response.json["env"]), expected_keys)
        self.assertNotIn("API_KEY_9ROUTER", response.json["env"])
        self.assertEqual(response.json["env"]["SERVER_USERNAME"], "panel-user")
        self.assertEqual(response.json["env"]["SERVER_PASSWORD"], "")
        self.assertEqual(response.json["count"], len(response.json["env"]))

    def test_server_secrets_environment_reads_hermes_telegram_token_from_hermes_env(self):
        hermes_env = self.root / "hermes.env"
        hermes_env.write_text(
            "TELEGRAM_BOT_TOKEN=synthetic-telegram-token\n",
            encoding="utf-8",
        )
        with (
            patch.object(panel_app, "HERMES_ENV_FILE", str(hermes_env)),
            patch.dict(panel_app.os.environ, {}, clear=True),
        ):
            response = self.client.get("/api/env/server-secrets")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json["env"]["HERMES_TELEGRAM_BOT_TOKEN"],
            "synthetic-telegram-token",
        )

    def test_logs_endpoint_reads_the_three_named_service_logs(self):
        service_logs = {
            "panel": "Management Panel started\n",
            "hermes": "Hermes Gateway ready\n",
            "status-bot": "Status Bot polling\n",
        }
        log_paths = {}
        for key, content in service_logs.items():
            log_path = self.root / f"{key}.log"
            log_path.write_text(content, encoding="utf-8")
            log_paths[key] = str(log_path)

        with patch.dict(panel_app.SERVICE_LOG_PATHS, log_paths):
            for key, content in service_logs.items():
                with self.subTest(service=key):
                    response = self.client.get(f"/api/logs/{key}")

                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json["logs"], content)
                    self.assertEqual(response.json["total_lines"], 1)

    def test_status_bot_worker_writes_to_its_dedicated_log(self):
        log_path = self.root / "status-bot.log"
        logger = panel_app.STATUS_BOT_LOGGER
        original_handlers = set(logger.handlers)
        try:
            with (
                patch.object(panel_app, "STATUS_BOT_LOG_PATH", str(log_path)),
                patch.dict(panel_app.os.environ, {"STATUS_BOT_TOKEN": "", "OWNER_ID": ""}, clear=True),
                patch.dict(panel_app.SERVICE_LOG_PATHS, {"status-bot": str(log_path)}),
            ):
                panel_app.telegram_poll_worker()
                for handler in logger.handlers:
                    handler.flush()
                response = self.client.get("/api/logs/status-bot")

            self.assertEqual(response.status_code, 200)
            self.assertIn("Telegram command listener skipped", response.json["logs"])
        finally:
            for handler in set(logger.handlers) - original_handlers:
                logger.removeHandler(handler)
                handler.close()

    def test_status_bot_delivery_errors_are_logged_without_secrets(self):
        log_path = self.root / "status-bot.log"
        logger = panel_app.STATUS_BOT_LOGGER
        original_handlers = set(logger.handlers)
        token = "synthetic-status-bot-token"
        try:
            with (
                patch.object(panel_app, "STATUS_BOT_LOG_PATH", str(log_path)),
                patch.dict(
                    panel_app.os.environ,
                    {"STATUS_BOT_TOKEN": token, "OWNER_ID": "synthetic-owner-id"},
                ),
                patch.object(panel_app.urllib.request, "urlopen", side_effect=OSError("request failed")),
            ):
                panel_app.send_telegram_msg("Test status message")
                for handler in logger.handlers:
                    handler.flush()
                log = log_path.read_text(encoding="utf-8")

            self.assertIn("Telegram message delivery failed (OSError)", log)
            self.assertNotIn(token, log)
        finally:
            for handler in set(logger.handlers) - original_handlers:
                logger.removeHandler(handler)
                handler.close()

    def test_status_bot_delivery_targets_owner_id(self):
        token = "synthetic-status-bot-token"
        owner_id = "123456789"
        requests = []

        def capture_request(request, timeout):
                requests.append(request)

        with (
                patch.dict(
                    panel_app.os.environ,
                    {"STATUS_BOT_TOKEN": token, "OWNER_ID": owner_id},
                    clear=True,
                ),
                patch.object(panel_app.urllib.request, "urlopen", side_effect=capture_request),
        ):
                panel_app.send_telegram_msg("Test status message")

        self.assertEqual(len(requests), 1)
        self.assertIn("chat_id=123456789", requests[0].data.decode("utf-8"))

    def test_logs_selector_starts_with_the_three_service_options(self):
        response = self.client.get("/")
        page = response.get_data(as_text=True)

        self.assertIn('<option value="panel">Management Panel</option>', page)
        self.assertIn('<option value="hermes">Hermes Gateway</option>', page)
        self.assertIn('<option value="status-bot">Status Bot</option>', page)
        self.assertNotIn('<option value="server-secrets">Server Secrets</option>', page)

    def test_add_bot_route_starts_background_install(self):
        with patch.object(panel_app.threading.Thread, "start"):
            response = self.client.post(
                "/api/bots",
                json={
                    "name": "Example Bot",
                    "repository": "https://github.com/example/telegram-bot.git",
                    "ref": "main",
                    "entrypoint": "src/bot.py",
                    "pat": "synthetic-token",
                    "environment": "BOT_TOKEN=synthetic-token OTHER_VAR = value-two",
                },
            )

        self.assertEqual(response.status_code, 202)
        self.assertIn("job", response.json)
        self.assertIn("bot", response.json)
        self.assertEqual(response.json["job"]["bot_name"], "Example Bot")
        self.assertEqual(response.json["job"]["definition"]["entrypoint"], "src/bot.py")
        self.assertEqual(response.json["bot"]["directory_name"], "Example Bot")
        self.assertNotIn("synthetic-token", response.get_data(as_text=True))

    def test_bot_info_uses_named_directory_and_legacy_id_fallback(self):
        _, definition = self.add_bot()
        named_definition = {**definition, "directory_name": "Example Bot"}

        self.assertEqual(
            panel_app.get_bot_info(named_definition)["dir"],
            str(self.root / "bots" / "Example Bot"),
        )
        self.assertEqual(
            panel_app.get_bot_info(definition)["dir"],
            str(self.root / "bots" / definition["id"]),
        )

    def test_add_bot_rejects_directory_name_already_reserved_by_queued_install(self):
        payload = {
            "name": "Example Bot",
            "repository": "https://github.com/example/telegram-bot.git",
            "ref": "main",
            "entrypoint": "src/bot.py",
        }
        with patch.object(panel_app.threading.Thread, "start"):
            first = self.client.post("/api/bots", json=payload)
            second = self.client.post("/api/bots", json=payload)

        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 409)

    def test_edit_bot_updates_fields_env_and_icon_without_revealing_pat(self):
        registry, definition = self.add_bot()
        set_bot_credentials(
            registry,
            definition["id"],
            {"pat": "synthetic-private-token", "environment": {"BOT_TOKEN": "old-token"}},
            panel_app.BOT_CREDENTIALS_KEY,
        )
        save_registry(panel_app.BOT_REGISTRY_PATH, registry)

        with patch.object(panel_app, "get_bot_proc", return_value=None):
            response = self.client.put(
                f"/api/bots/{definition['id']}",
                json={
                    "name": "Renamed Bot",
                    "repository": "https://github.com/example/new-bot.git",
                    "ref": "feature/branch",
                    "entrypoint": "package/main.py",
                    "icon": "🛰️",
                    "pat": "",
                    "environment": "BOT_TOKEN=new-token\n# ignored comment",
                },
            )

        self.assertEqual(response.status_code, 200)
        updated_registry = panel_app.get_bot_registry()
        updated = updated_registry["bots"][0]
        self.assertEqual(updated["name"], "Renamed Bot")
        self.assertEqual(updated["repository"], "https://github.com/example/new-bot.git")
        self.assertEqual(updated["ref"], "feature/branch")
        self.assertEqual(updated["entrypoint"], "package/main.py")
        self.assertEqual(updated["icon"], "🛰️")
        credentials = panel_app.get_bot_credentials(updated_registry, definition["id"], panel_app.BOT_CREDENTIALS_KEY)
        self.assertEqual(credentials["pat"], "synthetic-private-token")
        self.assertEqual(credentials["environment"], {"BOT_TOKEN": "new-token"})
        self.assertNotIn("synthetic-private-token", response.get_data(as_text=True))

    def test_update_bot_queues_stopped_update_using_saved_credentials(self):
        registry, definition = self.add_bot()
        set_bot_credentials(
            registry,
            definition["id"],
            {"pat": "synthetic-private-token", "environment": {"BOT_TOKEN": "synthetic-bot-token"}},
            panel_app.BOT_CREDENTIALS_KEY,
        )
        save_registry(panel_app.BOT_REGISTRY_PATH, registry)

        with patch.object(panel_app, "get_bot_proc", return_value=None), patch.object(panel_app, "stop_bot_process", return_value=True), patch.object(panel_app.threading.Thread, "start"):
            response = self.client.post(f"/api/bots/{definition['id']}/update")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json["job"]["operation"], "update")
        self.assertFalse(response.json["job"]["definition"]["enabled"])
        self.assertFalse(panel_app.BOT_INSTALL_PAYLOADS[response.json["job"]["id"]]["restart_after_update"])
        saved_payload = panel_app.BOT_INSTALL_PAYLOADS[response.json["job"]["id"]]
        self.assertEqual(saved_payload["pat"], "synthetic-private-token")
        self.assertEqual(saved_payload["environment"], {"BOT_TOKEN": "synthetic-bot-token"})
        self.assertNotIn("synthetic-private-token", response.get_data(as_text=True))
        self.assertFalse(panel_app.get_bot_registry()["bots"][0]["enabled"])

    def test_update_bot_remembers_to_restart_bot_that_was_running(self):
        registry, definition = self.add_bot()
        definition["enabled"] = True
        set_bot_credentials(
            registry,
            definition["id"],
            {"pat": "", "environment": {}},
            panel_app.BOT_CREDENTIALS_KEY,
        )
        save_registry(panel_app.BOT_REGISTRY_PATH, registry)

        with patch.object(panel_app, "get_bot_proc", return_value=Mock()), patch.object(panel_app, "stop_bot_process", return_value=True), patch.object(panel_app.threading.Thread, "start"):
            response = self.client.post(f"/api/bots/{definition['id']}/update")

        self.assertEqual(response.status_code, 202)
        payload = panel_app.BOT_INSTALL_PAYLOADS[response.json["job"]["id"]]
        self.assertTrue(payload["restart_after_update"])
        self.assertFalse(payload["definition"]["enabled"])
        self.assertFalse(panel_app.get_bot_registry()["bots"][0]["enabled"])

    def test_successful_update_keeps_previously_stopped_bot_stopped(self):
        registry, definition = self.add_bot()
        set_bot_credentials(registry, definition["id"], {"pat": "", "environment": {}}, panel_app.BOT_CREDENTIALS_KEY)
        save_registry(panel_app.BOT_REGISTRY_PATH, registry)
        with patch.object(panel_app, "get_bot_proc", return_value=None), patch.object(panel_app, "stop_bot_process", return_value=True), patch.object(panel_app.threading.Thread, "start"):
            response = self.client.post(f"/api/bots/{definition['id']}/update")

        job_id = response.json["job"]["id"]
        payload = dict(panel_app.BOT_INSTALL_PAYLOADS[job_id])

        def clone_to_staging(_job_id, _definition, _pat, repo_dir=None):
            Path(repo_dir).mkdir(parents=True)

        with patch.object(panel_app, "_clone_bot_repository", side_effect=clone_to_staging), patch.object(
            panel_app, "_install_bot_dependencies"
        ), patch.object(panel_app, "_validate_repo_entrypoint"), patch.object(panel_app, "start_bot_process") as start_bot:
            panel_app._run_bot_update(
                job_id,
                payload["definition"],
                payload["pat"],
                payload["environment"],
                payload["restart_after_update"],
            )

        start_bot.assert_not_called()
        self.assertEqual(panel_app.BOT_INSTALL_JOBS[job_id]["status"], "success")
        self.assertIn("remains stopped", panel_app.BOT_INSTALL_JOBS[job_id]["message"])
        self.assertFalse(panel_app.get_bot_registry()["bots"][0]["enabled"])

    def test_successful_update_restarts_previously_running_bot(self):
        registry, definition = self.add_bot()
        definition["enabled"] = True
        set_bot_credentials(registry, definition["id"], {"pat": "", "environment": {}}, panel_app.BOT_CREDENTIALS_KEY)
        save_registry(panel_app.BOT_REGISTRY_PATH, registry)
        with patch.object(panel_app, "get_bot_proc", return_value=Mock()), patch.object(panel_app, "stop_bot_process", return_value=True), patch.object(panel_app.threading.Thread, "start"):
            response = self.client.post(f"/api/bots/{definition['id']}/update")

        job_id = response.json["job"]["id"]
        payload = dict(panel_app.BOT_INSTALL_PAYLOADS[job_id])

        def clone_to_staging(_job_id, _definition, _pat, repo_dir=None):
            Path(repo_dir).mkdir(parents=True)

        def start_and_enable(bot_id, _definition):
            panel_app.set_bot_enabled(bot_id, True)
            return 200, "Example Bot started"

        with patch.object(panel_app, "_clone_bot_repository", side_effect=clone_to_staging), patch.object(
            panel_app, "_install_bot_dependencies"
        ), patch.object(panel_app, "_validate_repo_entrypoint"), patch.object(
            panel_app, "start_bot_process", side_effect=start_and_enable
        ) as start_bot:
            panel_app._run_bot_update(
                job_id,
                payload["definition"],
                payload["pat"],
                payload["environment"],
                payload["restart_after_update"],
            )

        start_bot.assert_called_once()
        self.assertEqual(panel_app.BOT_INSTALL_JOBS[job_id]["status"], "success")
        self.assertTrue(panel_app.get_bot_registry()["bots"][0]["enabled"])

    def test_start_bot_process_launches_bot_and_marks_it_enabled(self):
        registry, definition = self.add_bot()
        save_registry(panel_app.BOT_REGISTRY_PATH, registry)
        bot_dir = self.root / "bots" / definition["id"]
        entrypoint = bot_dir / "src" / "bot.py"
        entrypoint.parent.mkdir(parents=True)
        entrypoint.write_text("print('started')", encoding="utf-8")
        venv_python = bot_dir / ".venv" / "bin" / "python"
        venv_python.parent.mkdir(parents=True)
        venv_python.write_text("", encoding="utf-8")
        info = {
            **definition,
            "dir": str(bot_dir),
            "entry": definition["entrypoint"],
            "pid_path": str(self.root / "bot.pid"),
            "log_path": str(self.root / "bot.log"),
            "venv_python": str(venv_python),
        }

        with (
            patch.object(panel_app, "get_bot_info", return_value=info),
            patch.object(panel_app, "get_bot_proc", return_value=None),
            patch.object(panel_app.subprocess, "Popen", return_value=SimpleNamespace(pid=12345)) as process_start,
        ):
            status, message = panel_app.start_bot_process(definition["id"], definition)

        self.assertEqual(status, 200)
        self.assertEqual(message, "Example Bot started")
        self.assertTrue(panel_app.get_bot_registry()["bots"][0]["enabled"])
        self.assertEqual((self.root / "bot.pid").read_text(encoding="ascii"), "12345")
        process_start.assert_called_once()

    def test_update_worker_swaps_in_complete_staged_checkout(self):
        registry, definition = self.add_bot()
        save_registry(panel_app.BOT_REGISTRY_PATH, registry)
        current_dir = self.root / "bots" / definition["id"]
        current_dir.mkdir(parents=True)
        (current_dir / "old-version.txt").write_text("old", encoding="utf-8")
        job_id = "update-test-job"
        panel_app._record_bot_job(job_id, "queued", 0, "Queued", bot_id=definition["id"])

        def clone_to_staging(_job_id, update_definition, _pat, repo_dir=None):
            staging_dir = Path(repo_dir)
            entrypoint = staging_dir / update_definition["entrypoint"]
            entrypoint.parent.mkdir(parents=True, exist_ok=True)
            entrypoint.write_text("new", encoding="utf-8")

        with (
            patch.object(panel_app, "_clone_bot_repository", side_effect=clone_to_staging),
            patch.object(panel_app, "_install_bot_dependencies"),
            patch.object(panel_app, "start_bot_process", create=True) as start_bot,
        ):
            panel_app._run_bot_update(job_id, definition, "", {})

        self.assertFalse((current_dir / "old-version.txt").exists())
        self.assertEqual((current_dir / "src" / "bot.py").read_text(encoding="utf-8"), "new")
        self.assertEqual(panel_app.BOT_INSTALL_JOBS[job_id]["status"], "success")
        start_bot.assert_not_called()
        self.assertFalse(panel_app.get_bot_registry()["bots"][0]["enabled"])

    def test_update_worker_preserves_existing_checkout_when_clone_fails(self):
        registry, definition = self.add_bot()
        save_registry(panel_app.BOT_REGISTRY_PATH, registry)
        current_dir = self.root / "bots" / definition["id"]
        current_dir.mkdir(parents=True)
        existing_file = current_dir / "working-state.txt"
        existing_file.write_text("keep me", encoding="utf-8")
        job_id = "failed-update-job"
        panel_app._record_bot_job(job_id, "queued", 0, "Queued", bot_id=definition["id"])

        with patch.object(panel_app, "_clone_bot_repository", side_effect=RuntimeError("network timeout")):
            panel_app._run_bot_update(job_id, definition, "", {})

        self.assertEqual(existing_file.read_text(encoding="utf-8"), "keep me")
        self.assertFalse(any(self.root.joinpath("bots").glob(f".{definition['id']}.update-*")))
        self.assertEqual(panel_app.BOT_INSTALL_JOBS[job_id]["status"], "failed")

    def test_get_bot_edit_metadata_does_not_reveal_credentials(self):
        registry, definition = self.add_bot()
        set_bot_credentials(
            registry,
            definition["id"],
            {"pat": "synthetic-private-token", "environment": {"BOT_TOKEN": "synthetic-bot-token"}},
            panel_app.BOT_CREDENTIALS_KEY,
        )
        save_registry(panel_app.BOT_REGISTRY_PATH, registry)

        response = self.client.get(f"/api/bots/{definition['id']}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["bot"], definition)
        self.assertNotIn("synthetic-private-token", response.get_data(as_text=True))
        self.assertNotIn("synthetic-bot-token", response.get_data(as_text=True))

    def test_icon_only_edit_preserves_running_bot_state(self):
        registry, definition = self.add_bot()
        definition["enabled"] = True
        save_registry(panel_app.BOT_REGISTRY_PATH, registry)

        with patch.object(panel_app, "stop_bot_process") as stop_bot:
            response = self.client.put(
                f"/api/bots/{definition['id']}",
                json={"icon": "🚀"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(panel_app.get_bot_registry()["bots"][0]["enabled"])
        self.assertEqual(panel_app.get_bot_registry()["bots"][0]["icon"], "🚀")
        stop_bot.assert_not_called()

    def test_delete_bot_removes_local_registry_credentials_and_directory_but_preserves_shared_cache(self):
        registry, definition = self.add_bot()
        set_bot_credentials(
            registry,
            definition["id"],
            {"pat": "synthetic-private-token", "environment": {"BOT_TOKEN": "synthetic-bot-token"}},
            panel_app.BOT_CREDENTIALS_KEY,
        )
        save_registry(panel_app.BOT_REGISTRY_PATH, registry)
        bot_dir = self.root / "bots" / definition["id"]
        bot_dir.mkdir(parents=True)
        (bot_dir / "cached-state.txt").write_text("state", encoding="utf-8")

        with patch.object(panel_app, "stop_bot_process", return_value=True):
            response = self.client.delete(f"/api/bots/{definition['id']}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["deleted_cache_snapshots"], 0)
        self.assertIn("shared cache snapshots were preserved", response.json["message"].lower())
        self.assertEqual(panel_app.get_bot_registry()["bots"], [])
        self.assertEqual(panel_app.get_bot_registry()["encrypted_credentials"], {})
        self.assertFalse(bot_dir.exists())
        self.assertNotIn("synthetic-private-token", response.get_data(as_text=True))

    def test_delete_bot_rolls_back_directory_when_registry_save_fails(self):
        registry, definition = self.add_bot()
        save_registry(panel_app.BOT_REGISTRY_PATH, registry)
        bot_dir = self.root / "bots" / definition["id"]
        bot_dir.mkdir(parents=True)
        (bot_dir / "cached-state.txt").write_text("preserve", encoding="utf-8")

        with patch.object(panel_app, "stop_bot_process", return_value=True), patch.object(
            panel_app, "save_registry", side_effect=OSError("registry save failed")
        ):
            response = self.client.delete(f"/api/bots/{definition['id']}")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(panel_app.get_bot_registry()["bots"], [definition])
        self.assertEqual((bot_dir / "cached-state.txt").read_text(encoding="utf-8"), "preserve")

    def test_failed_install_can_be_edited_and_retried_without_resending_secrets(self):
        with patch.object(panel_app.threading.Thread, "start"):
            created = self.client.post(
                "/api/bots",
                json={
                    "name": "Example Bot",
                    "repository": "https://github.com/example/telegram-bot.git",
                    "ref": "main",
                    "entrypoint": "missing.py",
                    "pat": "synthetic-private-token",
                    "environment": "BOT_TOKEN=synthetic-bot-token",
                },
            )

        job_id = created.json["job"]["id"]
        panel_app._record_bot_job(job_id, "failed", 100, "Missing entrypoint", step="error")
        self.assertNotIn("synthetic-private-token", self.client.get("/api/bots/jobs").get_data(as_text=True))
        self.assertNotIn("synthetic-bot-token", self.client.get("/api/bots/jobs").get_data(as_text=True))

        with patch.object(panel_app.threading.Thread, "start"):
            retried = self.client.post(
                f"/api/bots/jobs/{job_id}/retry",
                json={"entrypoint": "src/bot.py", "pat": "", "environment": ""},
            )

        self.assertEqual(retried.status_code, 202)
        self.assertEqual(retried.json["job"]["status"], "queued")
        self.assertEqual(retried.json["job"]["definition"]["entrypoint"], "src/bot.py")
        saved_input = panel_app.BOT_INSTALL_PAYLOADS[job_id]
        self.assertEqual(saved_input["pat"], "synthetic-private-token")
        self.assertEqual(saved_input["environment"], {"BOT_TOKEN": "synthetic-bot-token"})
        self.assertNotIn("synthetic-private-token", retried.get_data(as_text=True))
        self.assertNotIn("synthetic-bot-token", retried.get_data(as_text=True))

    def test_retry_rejects_non_object_json(self):
        with patch.object(panel_app.threading.Thread, "start"):
            created = self.client.post(
                "/api/bots",
                json={
                    "name": "Example Bot",
                    "repository": "https://github.com/example/telegram-bot.git",
                    "ref": "main",
                    "entrypoint": "src/bot.py",
                },
            )
        job_id = created.json["job"]["id"]
        panel_app._record_bot_job(job_id, "failed", 100, "failed", step="error")

        response = self.client.post(f"/api/bots/jobs/{job_id}/retry", json=[])

        self.assertEqual(response.status_code, 400)

    def test_successful_install_discards_in_memory_retry_secrets(self):
        with patch.object(panel_app.threading.Thread, "start"):
            created = self.client.post(
                "/api/bots",
                json={
                    "name": "Example Bot",
                    "repository": "https://github.com/example/telegram-bot.git",
                    "ref": "main",
                    "entrypoint": "src/bot.py",
                    "pat": "synthetic-private-token",
                    "environment": "BOT_TOKEN=synthetic-bot-token",
                },
            )
        job_id = created.json["job"]["id"]
        payload = panel_app.BOT_INSTALL_PAYLOADS[job_id]

        with (
            patch.object(panel_app, "_clone_bot_repository"),
            patch.object(panel_app, "_install_bot_dependencies"),
            patch.object(panel_app, "_validate_repo_entrypoint"),
        ):
            panel_app._run_bot_install(job_id, payload["definition"], payload["pat"], payload["environment"])

        self.assertNotIn(job_id, panel_app.BOT_INSTALL_PAYLOADS)
        self.assertEqual(panel_app.BOT_INSTALL_JOBS[job_id]["status"], "success")

    def test_failed_install_error_redacts_pat(self):
        with patch.object(panel_app.threading.Thread, "start"):
            created = self.client.post(
                "/api/bots",
                json={
                    "name": "Example Bot",
                    "repository": "https://github.com/example/telegram-bot.git",
                    "ref": "main",
                    "entrypoint": "src/bot.py",
                    "pat": "synthetic-private-token",
                },
            )
        job = created.json["job"]
        payload = panel_app.BOT_INSTALL_PAYLOADS[job["id"]]

        with patch.object(
            panel_app,
            "_clone_bot_repository",
            side_effect=RuntimeError("Authentication failed for https://synthetic-private-token@github.com/example/repo.git"),
        ):
            panel_app._run_bot_install(job["id"], payload["definition"], payload["pat"], payload["environment"])

        failure = self.client.get("/api/bots/jobs").get_data(as_text=True)
        self.assertNotIn("synthetic-private-token", failure)
        self.assertIn("[REDACTED]", failure)

    def test_dependency_install_uses_extended_pip_timeout_and_retries(self):
        bot_id = "d" * 32
        repo_dir = self.root / "bots" / bot_id
        python_executable = repo_dir / ".venv" / "bin" / "python"
        python_executable.parent.mkdir(parents=True)
        python_executable.touch()
        (repo_dir / "requirements.txt").write_text("example-package\n", encoding="utf-8")
        definition = {"id": bot_id}

        with patch.object(panel_app, "_run_install_command") as run_command:
            panel_app._install_bot_dependencies("test-job", definition)

        command = run_command.call_args.args[0]
        self.assertIn("--timeout", command)
        self.assertIn("120", command)
        self.assertIn("--retries", command)
        self.assertIn("5", command)

    def test_dependency_read_timeout_is_reported_concisely(self):
        error = """ERROR: Exception: Traceback (most recent call last):
TimeoutError: The read operation timed out
pip._vendor.urllib3.exceptions.ReadTimeoutError: HTTPSConnectionPool(host='files.pythonhosted.org', port=443): Read timed out."""

        message = panel_app.format_bot_install_error(error)

        self.assertIn("Dependency download timed out", message)
        self.assertIn("retry the installation", message)
        self.assertNotIn("Traceback", message)
        self.assertNotIn("files.pythonhosted.org", message)

    def test_other_long_dependency_tracebacks_are_not_shown_to_user(self):
        error = "ERROR: Exception: Traceback (most recent call last):\n" + ("pip internal stack frame\n" * 80)

        message = panel_app.format_bot_install_error(error)

        self.assertEqual(message, "Installation failed with lengthy output. Check the dependency settings and retry.")
        self.assertNotIn("Traceback", message)

    def test_failed_dependency_job_does_not_return_pip_traceback(self):
        with patch.object(panel_app.threading.Thread, "start"):
            created = self.client.post(
                "/api/bots",
                json={
                    "name": "Slow Download Bot",
                    "repository": "https://github.com/example/telegram-bot.git",
                    "ref": "main",
                    "entrypoint": "src/bot.py",
                },
            )
        job_id = created.json["job"]["id"]
        payload = panel_app.BOT_INSTALL_PAYLOADS[job_id]
        read_timeout = "Traceback (most recent call last): ReadTimeoutError: HTTPSConnectionPool(host='files.pythonhosted.org', port=443): Read timed out."

        with patch.object(panel_app, "_clone_bot_repository"), patch.object(
            panel_app,
            "_install_bot_dependencies",
            side_effect=RuntimeError(read_timeout),
        ):
            panel_app._run_bot_install(job_id, payload["definition"], payload["pat"], payload["environment"])

        response = self.client.get(f"/api/bots/jobs/{job_id}")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Dependency download timed out", response.json["job"]["message"])
        self.assertNotIn("Traceback", response.get_data(as_text=True))

    def test_control_route_rejects_unknown_bot_id(self):
        response = self.client.post(f"/api/bot/{'b' * 32}/start")

        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()