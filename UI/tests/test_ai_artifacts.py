from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import numpy as np


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from UI.ai_assistant.artifacts import ArtifactAnalyzer
from UI.ai_assistant.context import AIContext
from UI.ai_assistant.knowledge import KnowledgeBase
from UI.ai_assistant.config import Config
from UI.ai_assistant.llm import (
    NullBackend,
    OllamaBackend,
    OpenAIBackend,
    OpenClawAgentBackend,
    create_llm,
)
from UI.ai_assistant.panel import AIAssistantPanel, response_char_limit, suggestion_char_limit
from UI.ai_assistant.responses import (
    compact_suggestion,
    grounded_user_message,
    limit_text,
    offline_response,
    suggestion_evidence,
)
from UI.ai_assistant.skills import SkillMapper


def destroy_test_root(root):
    # Cancel this test window's scheduled callbacks before Tcl teardown.
    for callback in root.tk.call("after", "info"):
        root.tk.call("after", "cancel", callback)
    root.destroy()


def write_synthetic_r3d(output):
    output.mkdir(parents=True, exist_ok=True)
    qc = {"frames": {"discovered": 12, "placeholder_frame_count": 2, "intensity_frame_count": 10},
          "sparse_observations": {"count": 50}, "peaks": {"count": 3}, "geometry": {}, "volume": {}}
    (output / "qc_report.json").write_text(json.dumps(qc), encoding="utf-8")
    (output / "observations").mkdir(exist_ok=True)
    frames = [{"is_placeholder_zero_frame": i < 2, "used_for_intensity": i >= 2} for i in range(12)]
    (output / "observations" / "frame_geometry.json").write_text(json.dumps({"frames": frames}), encoding="utf-8")
    return qc


class RealArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.analyzer = ArtifactAnalyzer(ROOT)

    @unittest.skipUnless((ROOT / "Data" / "sample2_2" / "diff" / "p" / "CORRECT.LP").is_file(), "optional private integration fixture is not distributed")
    def test_real_xds_good_dataset(self):
        report = self.analyzer.analyze(
            ROOT / "Data" / "sample2_2",
            "AutoXDS",
            "sample2_2",
            hints={"root": ROOT / "Data"},
        )
        values = {item.label: item.value for item in report.evidence}
        self.assertEqual(report.status, "complete")
        self.assertEqual(values["Space group"], "62")
        self.assertGreater(float(values["Completeness"]), 80)
        self.assertTrue(any(path.endswith("CORRECT.LP") for path in report.artifacts))
        self.assertTrue(any("AutoSolve" in step for step in report.next_steps))

    def test_real_xds_low_completeness_dataset(self):
        dataset = ROOT / "Data" / "example_specimen_0902" / "example_specimen_7"
        if not dataset.exists():
            self.skipTest("optional real low-completeness dataset is not present")
        report = self.analyzer.analyze(
            dataset,
            "AutoXDS",
            "example_specimen_7",
            hints={"root": ROOT / "Data"},
        )
        codes = {item.code for item in report.findings}
        self.assertIn("xds_low_completeness", codes)
        self.assertTrue(any("HCA" in step for step in report.next_steps))

    @unittest.skipUnless((ROOT / "Demo" / "AutoR3D_Demo_single_legacy" / "results" / "AutoR3D" / "qc_report.json").is_file(), "optional private integration fixture is not distributed")
    def test_real_r3d_placeholder_geometry_and_qc(self):
        report = self.analyzer.analyze(
            ROOT / "Demo" / "AutoR3D_Demo_single_legacy",
            "Reconstruction",
            "Demo_single",
            hints={"output": "results/AutoR3D"},
        )
        values = {item.label: item.value for item in report.evidence}
        self.assertEqual(report.status, "complete")
        self.assertEqual(values["Frames"], "421 total / 42 placeholders / 379 intensity")
        self.assertEqual(values["Sparse observations"], "4063")
        self.assertEqual(values["Peak candidates"], "19")
        self.assertIn("r3d_placeholder_verified", {item.code for item in report.findings})
        self.assertNotIn("r3d_placeholder_mismatch", {item.code for item in report.findings})

    @unittest.skipUnless((ROOT / "Demo" / "AutoSolve" / "1.res").is_file(), "optional private integration fixture is not distributed")
    def test_real_shelxt_solution(self):
        report = self.analyzer.analyze(
            ROOT / "Demo" / "AutoSolve",
            "AutoSolve",
            "1",
            hints={"workdir": ROOT / "Demo" / "AutoSolve", "basename": "1"},
        )
        values = {item.label: item.value for item in report.evidence}
        self.assertEqual(report.status, "solved_success")
        self.assertGreater(int(values["Atom records before HKLF"]), 20)
        self.assertEqual(values["Best CFOM"], "0.4581")
        self.assertIn("P21", values["Title / space group"])

    def test_missing_dataset_is_safe(self):
        report = self.analyzer.analyze(ROOT / "does-not-exist", "AutoXDS", "missing")
        self.assertEqual(report.status, "not-ready")
        self.assertIn("xds_output_missing", {item.code for item in report.findings})


class ArtifactEdgeCaseTests(unittest.TestCase):
    def test_detects_broken_placeholder_policy(self):
        source = ROOT / "Demo" / "AutoR3D_Demo_single_legacy" / "results" / "AutoR3D"
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "results" / "AutoR3D"
            (out / "observations").mkdir(parents=True)
            write_synthetic_r3d(out)
            data = json.loads((out / "observations" / "frame_geometry.json").read_text(encoding="utf-8"))
            placeholder = next(item for item in data["frames"] if item["is_placeholder_zero_frame"])
            placeholder["used_for_intensity"] = True
            (out / "observations" / "frame_geometry.json").write_text(json.dumps(data), encoding="utf-8")
            report = ArtifactAnalyzer(ROOT).inspect_r3d(Path(temp), "broken", {"output": "results/AutoR3D"})
            self.assertIn("r3d_placeholder_mismatch", {item.code for item in report.findings})

    def test_no_observations_recommends_single_first_change(self):
        source = ROOT / "Demo" / "AutoR3D_Demo_single_legacy" / "results" / "AutoR3D" / "qc_report.json"
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "results" / "AutoR3D"
            out.mkdir(parents=True)
            data = write_synthetic_r3d(out)
            data["sparse_observations"]["count"] = 0
            data["peaks"]["count"] = 0
            (out / "qc_report.json").write_text(json.dumps(data), encoding="utf-8")
            report = ArtifactAnalyzer(ROOT).inspect_r3d(Path(temp), "empty", {"output": "results/AutoR3D"})
            self.assertEqual(report.highest_severity, "error")
            self.assertIn("peak-threshold first", report.next_steps[0])


class AssistantModeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="autocrys-synthetic-qc-")
        cls.addClassCleanup(cls.temp.cleanup)
        dataset = Path(cls.temp.name)
        write_synthetic_r3d(dataset / "results" / "AutoR3D")
        cls.report = ArtifactAnalyzer(ROOT).analyze(
            dataset,
            "Reconstruction",
            "Demo_single",
            hints={"output": "results/AutoR3D"},
        )
        cls.knowledge = KnowledgeBase(ROOT)

    def test_offline_mode_answers_from_real_evidence(self):
        response = offline_response("分析当前结果和下一步", self.knowledge, self.report, NullBackend())
        self.assertIn("12 total / 2 placeholders / 10 intensity", response)
        self.assertIn("50", response)
        self.assertIn("not final reflection integration", response)

    def test_context_supplies_report_to_model(self):
        context = AIContext(active_tab="Reconstruction", dataset_name="Demo_single")
        context.update_artifact_report(self.report)
        prompt = grounded_user_message(context.build_prompt_context(), "What should I do next?")
        self.assertIn("Artifact analysis", prompt)
        self.assertIn("Peak candidates: 3", prompt)

    def test_context_snapshot_is_compact_and_omits_panel_text(self):
        context = AIContext(active_tab="AutoXDS", dataset_name="sample")
        context.update_action_context(
            {
                "datasets": ["sample", "sample_2"],
                "cell": "10 20 30 90 90 90",
                "panel_text": "DO_NOT_SEND_FULL_PANEL",
            }
        )
        context.append_console("OLD_LOG_SHOULD_DROP")
        for index in range(12):
            context.append_console(f"recent-log-{index}")
        prompt = context.build_prompt_context()
        self.assertIn("Page: AutoXDS", prompt)
        self.assertIn("datasets=['sample', 'sample_2']", prompt)
        self.assertIn("cell=10 20 30 90 90 90", prompt)
        self.assertNotIn("DO_NOT_SEND_FULL_PANEL", prompt)
        self.assertNotIn("OLD_LOG_SHOULD_DROP", prompt)

    def test_reply_limit_requires_explicit_detail_request(self):
        self.assertEqual(response_char_limit("为什么失败？"), 1200)
        self.assertEqual(response_char_limit("请详细说明为什么失败"), 4000)
        self.assertEqual(suggestion_char_limit(300), 1200)

    def test_safety_limit_prefers_a_complete_sentence(self):
        response = limit_text("第一句信息完整。第二句内容很长而且还没有写完", 14)
        self.assertEqual(response, "第一句信息完整。…")

    def test_compact_suggestion_has_two_parts_and_stays_under_limit(self):
        response = compact_suggestion(self.report, 400)
        self.assertIn("判断：", response)
        self.assertIn("建议：", response)
        self.assertLessEqual(len(response), 400)

    @staticmethod
    def _hca_result():
        return {
            "status": "clustered",
            "datasets": ["experiment_21", "experiment_23"],
            "methods": {
                "cc1": {
                    "pairwise": [
                        {
                            "i": "experiment_21",
                            "j": "experiment_23",
                            "cc1": 0.407205,
                            "common_reflections": 37,
                            "distance": 0.913337,
                        }
                    ],
                    "hca": {"merges": []},
                }
            },
        }

    def test_context_captures_last_operation_result_and_its_logs(self):
        context = AIContext(active_tab="AutoXDS", dataset_name="experiment_21")
        context.update_action_context({"datasets": ["experiment_21", "experiment_23"]})
        context.start_operation("AutoXDS HCA Run", "cluster ...")
        context.append_console("pair uses 37 common reflections")
        context.record_operation_result("AutoXDS HCA Run", self._hca_result())
        context.finish_operation("AutoXDS HCA Run", success=True)
        self.assertEqual(context.last_operation["tab"], "AutoXDS")
        self.assertEqual(context.last_operation["result"]["status"], "clustered")
        self.assertIn("37 common", context.last_operation["logs"][0])

    def test_offline_hca_suggestion_uses_hca_metrics_and_cutoff(self):
        operation = {"name": "AutoXDS HCA Run", "success": True, "result": self._hca_result(), "logs": []}
        response = compact_suggestion(
            None,
            0,
            operation=operation,
            hca_result=self._hca_result(),
            hca_cutoff=0.95,
            screen={"hca_clusters": [["experiment_21", "experiment_23"]]},
        )
        self.assertIn("HCA分析了2个数据集", response)
        self.assertIn("共同反射最少37", response)
        self.assertIn("cutoff=0.95", response)
        self.assertIn("1组可进行多数据集合并", response)

    def test_ai_skill_hca_is_classified_from_the_command(self):
        operation = {
            "name": "AI Skill",
            "command": "python auto_xds.py cluster --dataset experiment_21 --dataset experiment_23",
            "success": True,
            "result": self._hca_result(),
            "logs": [],
        }
        response = compact_suggestion(None, 0, operation=operation)
        self.assertIn("HCA分析了2个数据集", response)
        self.assertIn("共同反射最少37", response)
        self.assertIn("尚未选cutoff", response)

    def test_xds_operation_is_not_misclassified_by_stale_hca_state(self):
        operation = {
            "name": "AutoXDS Process & Convert",
            "success": True,
            "result": {"rows": [{"Dataset": "sample2_2", "Completeness": "92.1", "Rfactor": "18.2"}]},
        }
        response = compact_suggestion(
            self.report,
            0,
            operation=operation,
            hca_result=self._hca_result(),
        )
        self.assertIn("AutoXDS Process & Convert", response)
        self.assertIn("完整度范围92.1%", response)
        self.assertNotIn("HCA分析了", response)

    def test_model_evidence_contains_operation_ui_result_and_logs(self):
        operation = {
            "name": "AutoXDS HCA Run",
            "success": True,
            "result": self._hca_result(),
            "logs": ["HCA complete with 37 reflections"],
        }
        evidence = suggestion_evidence(
            None,
            operation=operation,
            hca_result=self._hca_result(),
            hca_cutoff=None,
            screen={"active_tab": "AutoXDS", "hca_clusters": []},
        )
        self.assertIn("[Last completed operation]", evidence)
        self.assertIn("[Current UI state]", evidence)
        self.assertIn("[Operation result]", evidence)
        self.assertIn("HCA complete with 37 reflections", evidence)
        self.assertIn("lower cutoff is stricter", evidence)

    @unittest.skipUnless(os.environ.get("RUN_OLLAMA_TESTS") == "1", "set RUN_OLLAMA_TESTS=1 for local model integration")
    def test_local_ollama_grounded_real_data(self):
        backend = OllamaBackend(model="llama3.2")  # intentionally absent: fallback discovery is part of the test
        self.assertTrue(backend.is_available())
        context = AIContext(active_tab="Reconstruction", dataset_name="Demo_single")
        context.update_artifact_report(self.report)
        response = backend.chat(
            self.knowledge.build_system_prompt(),
            grounded_user_message(context.build_prompt_context(), "用中文简要指出占位零帧是否正确处理，并给出下一步。必须引用数字。"),
        )
        self.assertGreater(len(response.strip()), 20)
        self.assertTrue("42" in response or "421" in response, response)
        self.assertNotIn("[Ollama error", response)


class BackendSelectionTests(unittest.TestCase):
    def test_system_prompt_can_load_only_relevant_skill(self):
        knowledge = KnowledgeBase(ROOT)
        prompt = knowledge.build_system_prompt("catalog", modules=["AutoXDS"])
        self.assertIn("--- AutoXDS ---", prompt)
        self.assertNotIn("--- AutoR3D ---", prompt)
        self.assertLess(len(prompt), len(knowledge.build_system_prompt("catalog")))

    def test_minimal_prompt_has_professional_role_without_full_skills(self):
        knowledge = KnowledgeBase(ROOT)
        prompt = knowledge.build_system_prompt(modules=[])
        self.assertIn("3D Electron Diffraction", prompt)
        self.assertIn("OPTIONAL KNOWLEDGE MODULES", prompt)
        self.assertIn("Always answer in Simplified Chinese", prompt)
        self.assertNotIn("AVAILABLE EXECUTABLE SKILLS", prompt)
        self.assertIn("<autocrys_result>", prompt)
        self.assertNotIn("--- AutoXDS ---", prompt)

    def test_agent_bridge_prompt_delegates_skills_to_openclaw(self):
        prompt = KnowledgeBase(ROOT).build_agent_bridge_prompt()
        self.assertIn("skills and tools configured in your OpenClaw environment", prompt)
        self.assertIn("<autocrys_result>", prompt)
        self.assertNotIn("OPTIONAL KNOWLEDGE MODULES", prompt)
        self.assertNotIn("AVAILABLE EXECUTABLE SKILLS", prompt)
        self.assertNotIn('"skill_id"', prompt)

    def test_openai_backend_reports_empty_response(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"choices": []}'
        backend = OpenAIBackend(api_key="test-key", model="test-model")
        with mock.patch("UI.ai_assistant.llm.urllib.request.urlopen", return_value=response):
            result = backend.chat("system", "user")
        self.assertIn("empty response", result)

    def test_openai_backend_accepts_reasoning_content_fallback(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            "choices": [{"message": {"content": "", "reasoning_content": "备用回复"}}]
        }, ensure_ascii=False).encode("utf-8")
        backend = OpenAIBackend(api_key="test-key", model="test-model")
        with mock.patch("UI.ai_assistant.llm.urllib.request.urlopen", return_value=response):
            result = backend.chat("system", "user")
        self.assertEqual(result, "备用回复")

    def test_openai_backend_uses_deployment_as_request_model(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"choices":[{"message":{"content":"OK"}}]}'
        backend = OpenAIBackend(
            api_key="test-key",
            model="GPT-5.6-SOL",
            request_model="example-gpt-5-deployment",
        )
        with mock.patch("UI.ai_assistant.llm.urllib.request.urlopen", return_value=response) as urlopen:
            result = backend.chat("system", "user")
        request = urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(result, "OK")
        self.assertEqual(backend.model_name, "GPT-5.6-SOL")
        self.assertEqual(backend.request_model_name, "example-gpt-5-deployment")
        self.assertEqual(payload["model"], "example-gpt-5-deployment")

    def test_openai_backend_gpt5_uses_completion_token_budget(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"choices":[{"message":{"content":"OK"}}]}'
        backend = OpenAIBackend(
            api_key="test-key",
            model="GPT-5.6-SOL",
            provider="custom",
            request_model="example-gpt-5-deployment",
        )
        with mock.patch("UI.ai_assistant.llm.urllib.request.urlopen", return_value=response) as urlopen:
            result = backend.chat("system", "user")
        payload = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(result, "OK")
        self.assertIn("max_completion_tokens", payload)
        self.assertNotIn("max_tokens", payload)
        self.assertNotIn("temperature", payload)

    def test_openai_backend_deepseek_keeps_legacy_sampling_parameters(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"choices":[{"message":{"content":"OK"}}]}'
        backend = OpenAIBackend(api_key="test-key", model="deepseek-pro", provider="deepseek")
        with mock.patch("UI.ai_assistant.llm.urllib.request.urlopen", return_value=response) as urlopen:
            backend.chat("system", "user")
        payload = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(payload["max_tokens"], 2048)
        self.assertEqual(payload["temperature"], 0.3)
        self.assertNotIn("max_completion_tokens", payload)

    def test_openai_backend_surfaces_gateway_error_envelope(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            "success": False,
            "message": "Unsupported parameter: 'max_tokens' is not supported with this model.",
            "code": 500,
            "result": None,
        }).encode("utf-8")
        backend = OpenAIBackend(api_key="test-key", model="GPT-5.6-SOL")
        with mock.patch("UI.ai_assistant.llm.urllib.request.urlopen", return_value=response):
            result = backend.chat("system", "user")
        self.assertIn("Unsupported parameter", result)

    def test_agent_mode_builds_openclaw_backend(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            path.write_text(json.dumps({
                "llm": {
                    "mode": "agent",
                    "agent": {
                        "session_key": "autocrys-test",
                        "agent_id": "main",
                        "timeout_seconds": 120,
                    },
                },
            }), encoding="utf-8")
            backend = create_llm(path)
        self.assertIsInstance(backend, OpenClawAgentBackend)
        self.assertEqual(backend.mode_name, "agent")
        self.assertEqual(backend.model_name, "autocrys-test")

    def test_openclaw_backend_sends_message_file_and_parses_reply(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({
                "status": "ok",
                "result": {"finalAssistantVisibleText": "晶体学回复"},
            }, ensure_ascii=False),
            stderr="",
        )
        seen: dict = {}

        def fake_run(argv, **kwargs):
            message_path = Path(argv[argv.index("--message-file") + 1])
            seen["argv"] = argv
            seen["path"] = message_path
            seen["message"] = message_path.read_text(encoding="utf-8")
            return completed

        backend = OpenClawAgentBackend(session_key="autocrys-test")
        with mock.patch("UI.ai_assistant.llm.subprocess.run", side_effect=fake_run):
            result = backend.chat("系统提示", "用户消息")
        self.assertEqual(result, "晶体学回复")
        self.assertEqual(seen["argv"][seen["argv"].index("--session-key") + 1], "autocrys-test")
        self.assertIn("--agent", seen["argv"])
        self.assertIn("--timeout", seen["argv"])
        self.assertIn("系统提示", seen["message"])
        self.assertIn("用户消息", seen["message"])
        self.assertFalse(seen["path"].exists())

    def test_openclaw_backend_declares_native_skill_management(self):
        self.assertTrue(OpenClawAgentBackend().manages_own_skills)
        self.assertFalse(NullBackend().manages_own_skills)

    def test_openclaw_backend_handles_payloads_and_failures(self):
        payload_response = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({
                "status": "ok",
                "result": {"payloads": [{"text": "来自payload"}]},
            }, ensure_ascii=False),
            stderr="",
        )
        backend = OpenClawAgentBackend()
        with mock.patch("UI.ai_assistant.llm.subprocess.run", return_value=payload_response):
            self.assertEqual(backend.chat("", "你好"), "来自payload")
        error_response = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="gateway unreachable",
        )
        with mock.patch("UI.ai_assistant.llm.subprocess.run", return_value=error_response):
            self.assertIn("gateway unreachable", backend.chat("", "你好"))
        with mock.patch("UI.ai_assistant.llm.subprocess.run", side_effect=FileNotFoundError()):
            self.assertIn("command not found", backend.chat("", "你好"))

    def test_agent_completion_block_is_validated_and_removed_from_visible_text(self):
        visible, payload, error = AIAssistantPanel._extract_task_result(
            '完成。\n<autocrys_result>{"module":"AutoSolve","operation":"solve_shelxt",'
            '"success":true,"summary":"找到结构模型"}</autocrys_result>'
        )
        self.assertEqual(visible, "完成。")
        self.assertIsNone(error)
        self.assertEqual(payload["module"], "AutoSolve")
        self.assertTrue(payload["success"])

        _visible, payload, error = AIAssistantPanel._extract_task_result(
            '<autocrys_result>{"module":"Other","success":true,"summary":"x"}</autocrys_result>'
        )
        self.assertIsNone(payload)
        self.assertIn("unknown module", error)

    def test_openai_backend_accepts_responses_style_output(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            "output": [{"content": [{"type": "output_text", "text": "兼容回复"}]}]
        }, ensure_ascii=False).encode("utf-8")
        backend = OpenAIBackend(api_key="test-key", model="test-model")
        with mock.patch("UI.ai_assistant.llm.urllib.request.urlopen", return_value=response):
            result = backend.chat("system", "user")
        self.assertEqual(result, "兼容回复")

    def test_chat_thread_reports_exception_to_ui_queue(self):
        panel = object.__new__(AIAssistantPanel)
        panel.context = mock.Mock()
        panel.context.build_prompt_context.return_value = "context"
        panel._llm = mock.Mock()
        panel._llm.chat.side_effect = RuntimeError("request failed")
        panel._system_prompt = "system"
        panel._async_queue = queue.Queue()
        panel._run_llm_query("hello", {})
        kind, payload = panel._async_queue.get(timeout=2)
        self.assertEqual(kind, "llm_error")
        self.assertIn("request failed", payload)

    def test_agent_chat_does_not_expand_local_skill_modules(self):
        panel = object.__new__(AIAssistantPanel)
        panel.context = mock.Mock()
        panel.context.build_prompt_context.return_value = "context"
        panel._llm = mock.Mock()
        panel._llm.manages_own_skills = True
        panel._llm.chat.return_value = (
            '<autocrys_context>{"modules":["AutoXDS"]}</autocrys_context>'
        )
        panel._system_prompt = "local skill prompt"
        panel._agent_bridge_prompt = "agent bridge prompt"
        panel._async_queue = queue.Queue()
        panel._run_llm_query("hello", {})
        kind, payload = panel._async_queue.get(timeout=2)
        self.assertEqual(kind, "llm")
        self.assertEqual(panel._llm.chat.call_count, 1)
        self.assertEqual(panel._llm.chat.call_args.args[0], "agent bridge prompt")

    def test_agent_reply_cannot_trigger_local_skill_execution(self):
        panel = object.__new__(AIAssistantPanel)
        panel._llm = mock.Mock()
        panel._llm.manages_own_skills = True
        panel._skills = mock.Mock()
        panel._add_message = mock.Mock()
        panel._task_result_callback = None
        panel._apply_llm_response(
            '<autocrys_action>{"skill_id":"process_xds","params":{}}</autocrys_action>',
            {},
        )
        panel._skills.parse_model_response.assert_not_called()
        self.assertFalse(hasattr(panel, "_pending_skill"))

    def test_config_controls_default_assistant_state(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Config(Path(temp) / "config.json")
            self.assertFalse(config.assistant_enabled)
            config.assistant_enabled = True
            self.assertTrue(Config(Path(temp) / "config.json").assistant_enabled)
            self.assertEqual(config.max_suggestion_chars, 300)

    def test_auto_mode_falls_back_to_deepseek_environment_key(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            path.write_text(json.dumps({"llm": {"mode": "auto"}}), encoding="utf-8")
            with mock.patch.object(OllamaBackend, "is_available", return_value=False), mock.patch.dict(
                os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=False
            ):
                backend = create_llm(path)
            self.assertIsInstance(backend, OpenAIBackend)
            self.assertEqual(backend.provider_name, "deepseek")
            self.assertEqual(backend.model_name, "deepseek-flash")

    def test_named_cloud_profile_uses_its_own_environment_key(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            path.write_text(json.dumps({
                "llm": {
                    "mode": "cloud",
                    "cloud": {
                        "active_profile": "gpt-5.6-terra",
                        "profiles": {
                            "gpt-5.6-sol": {
                                "provider": "custom",
                                "api_key_env": "TEST_SOL_API_KEY",
                                "base_url": "https://example.invalid/api/v1/start",
                                "model": "GPT-5.6-SOL",
                            },
                            "gpt-5.6-terra": {
                                "provider": "custom",
                                "api_key_env": "TEST_TERRA_API_KEY",
                                "base_url": "https://example.invalid/api/v1/start",
                                "model": "GPT-5.6-Terra",
                            },
                        },
                    },
                },
            }), encoding="utf-8")
            with mock.patch.dict(os.environ, {"TEST_TERRA_API_KEY": "terra-key"}, clear=False):
                backend = create_llm(path)
            self.assertIsInstance(backend, OpenAIBackend)
            self.assertTrue(backend.is_available())
            self.assertEqual(backend.provider_name, "custom")
            self.assertEqual(backend.model_name, "GPT-5.6-Terra")

    def test_profile_environment_override_takes_precedence(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            path.write_text(json.dumps({
                "llm": {
                    "mode": "cloud",
                    "cloud": {
                        "active_profile": "first",
                        "profiles": {
                            "first": {"api_key_env": "TEST_FIRST_KEY", "model": "first-model"},
                            "second": {"api_key_env": "TEST_SECOND_KEY", "model": "second-model"},
                        },
                    },
                },
            }), encoding="utf-8")
            with mock.patch.dict(os.environ, {
                "AUTOCRYS_LLM_PROFILE": "second",
                "TEST_SECOND_KEY": "second-key",
            }, clear=False):
                backend = create_llm(path)
            self.assertEqual(backend.model_name, "second-model")

    def test_unknown_named_cloud_profile_does_not_use_another_key(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            path.write_text(json.dumps({
                "llm": {
                    "mode": "cloud",
                    "cloud": {
                        "active_profile": "typo",
                        "profiles": {
                            "valid": {"api_key_env": "TEST_VALID_KEY", "model": "valid-model"},
                        },
                    },
                },
            }), encoding="utf-8")
            with mock.patch.dict(os.environ, {"TEST_VALID_KEY": "valid-key"}, clear=False):
                backend = create_llm(path)
            self.assertFalse(backend.is_available())


class SkillSafetyTests(unittest.TestCase):
    def test_model_receives_full_skill_docs_and_action_contract(self):
        mapper = SkillMapper(ROOT)
        prompt = KnowledgeBase(ROOT).build_system_prompt(mapper.action_catalog())
        self.assertIn("Do not claim final reflection integration is complete", prompt)
        self.assertIn("AVAILABLE EXECUTABLE SKILLS", prompt)
        self.assertIn('"skill_id": "run_r3d"', prompt)
        self.assertIn("<autocrys_action>", prompt)
        self.assertIn("Always answer in Simplified Chinese", prompt)
        self.assertIn("aim for about 300 Chinese characters", prompt)
        self.assertIn("rather than ending mid-sentence", prompt)

    def test_artifact_analysis_is_cached_until_state_changes(self):
        analyzer = mock.Mock()
        analyzer.analyze.return_value = object()
        panel = object.__new__(AIAssistantPanel)
        panel._analyzer = analyzer
        panel._analysis_cache = {}
        panel._analysis_cache_lock = threading.Lock()
        inputs = (
            {"output": "results/AutoR3D", "panel_text": "large"},
            ROOT / "Demo" / "AutoR3D_Demo_single_legacy",
            "Demo_single",
            "AutoR3D",
            {},
        )
        first = panel._analyze_inputs(inputs)
        second = panel._analyze_inputs(inputs)
        self.assertIs(first, second)
        analyzer.analyze.assert_called_once()
        panel._refresh_analysis(request=False)
        panel._analyze_inputs(inputs)
        self.assertEqual(analyzer.analyze.call_count, 2)

    def test_model_can_select_skill_without_keyword_router(self):
        mapper = SkillMapper(ROOT)
        response = (
            "我会根据 AutoXDS skill 处理当前数据。\n"
            '<autocrys_action>{"skill_id":"process_xds","params":{"dataset":"sample",'
            '"ignored":"not-allowed"}}</autocrys_action>'
        )
        visible, skill, params, error = mapper.parse_model_response(
            response,
            {"root": ROOT / "Data"},
        )
        self.assertIsNone(error)
        self.assertEqual(skill.skill_id, "process_xds")
        self.assertEqual(params["dataset"], "sample")
        self.assertEqual(params["root"], str(ROOT / "Data"))
        self.assertNotIn("ignored", params)
        self.assertNotIn("autocrys_action", visible)

    def test_model_cannot_select_unregistered_skill(self):
        mapper = SkillMapper(ROOT)
        _visible, skill, params, error = mapper.parse_model_response(
            '<autocrys_action>{"skill_id":"run_any_shell","params":{}}</autocrys_action>'
        )
        self.assertIsNone(skill)
        self.assertEqual(params, {})
        self.assertIn("unknown skill", error)

    def test_real_read_only_list_skill_executes(self):
        mapper = SkillMapper(ROOT)
        skill = next(item for item in mapper.available_skills if item.skill_id == "list_datasets")
        with tempfile.TemporaryDirectory() as temp:
            completed = subprocess.run(
                skill.build_argv(root=temp),
                cwd=str(ROOT), capture_output=True, text=True, timeout=30,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload.get("datasets", []), [])

    @unittest.skipUnless((ROOT / "Demo" / "AutoR3D_Demo_single_legacy" / "diff").is_dir(), "optional private integration fixture is not distributed")
    def test_real_read_only_r3d_inspect_skill_executes(self):
        mapper = SkillMapper(ROOT)
        skill = next(item for item in mapper.available_skills if item.skill_id == "inspect_r3d")
        completed = subprocess.run(
            skill.build_argv(dataset=str(ROOT / "Demo" / "AutoR3D_Demo_single_legacy"), frames="diff"),
            cwd=str(ROOT), capture_output=True, text=True, timeout=45,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["frames_discovered"], 421)
        self.assertEqual(payload["placeholder_frame_count"], 42)

    def test_context_without_intent_never_triggers_skill(self):
        mapper = SkillMapper(ROOT)
        context = {"dataset_name": "sample2_2", "root": ROOT / "Data", "sg": "62", "composition": "C2"}
        self.assertEqual(mapper.match("当前结果有什么问题？", context), [])


class UISmokeTests(unittest.TestCase):
    def test_rotation_axis_search_tab_is_hidden(self):
        try:
            import tkinter as tk
            from tkinter import ttk
            from UI.ui import AutoR3DUI
            root = tk.Tk()
        except Exception as exc:
            self.skipTest(f"Tk UI unavailable: {exc}")
        root.withdraw()
        try:
            ui = AutoR3DUI(root, ai_assistant=False)
            labels = [ui._notebook.tab(tab_id, "text") for tab_id in ui._notebook.tabs()]
            self.assertNotIn("Rotation Axis", labels)
            self.assertNotIn("Global Search", labels)
            self.assertNotIn("Refine Axis", labels)
            self.assertNotIn("Peak Search", labels)
            self.assertIn("AutoR3D", labels)
            self.assertNotIn("Reconstruction", labels)
            self.assertEqual(Path(ui.dataset_var.get()), ROOT / "Data")
            self.assertEqual(Path(ui.output_var.get()), ROOT / "Data" / "AutoR3D")
            self.assertEqual(Path(ui.solve_workdir_var.get()), ROOT / "Data")
            self.assertEqual(Path(ui.solve_res_var.get()), ROOT / "Data" / "initial.res")
            ui.xds_datasets_var.set("sample")
            clear_process = ui._build_autoxds_command("process")
            self.assertIn("--clear-declared-cell-sg", clear_process)
            self.assertIn("--preserve-original-cell-sg-on-first-run", clear_process)
            with tempfile.TemporaryDirectory() as temp:
                ui.xds_root_var.set(temp)
                with mock.patch.object(ui, "xds_run_json_async") as run_json:
                    ui.xds_process_convert()
            process_command = run_json.call_args.args[1]
            self.assertIn("--clear-declared-cell-sg", process_command)
            self.assertIn("--preserve-original-cell-sg-on-first-run", process_command)
            ui.xds_cell_var.set("10 20 30 90 90 90")
            ui.xds_sg_var.set("19")
            constrained_process = ui._build_autoxds_command("process")
            self.assertNotIn("--clear-declared-cell-sg", constrained_process)
            self.assertIn("--declared-cell", constrained_process)
            ui.xds_cell_var.set("")
            ui.xds_sg_var.set("19")
            clear_set = ui._build_autoxds_command("set-cell-sg")
            self.assertIn("--clear-declared-cell-sg", clear_set)
            self.assertNotIn("--preserve-original-cell-sg-on-first-run", clear_set)
            self.assertEqual(ui.xds_sg_var.get(), "")
            self.assertEqual(str(ui.reconstruction_button.cget("state")), "disabled")
            with self.assertRaisesRegex(RuntimeError, "Import a dataset first|Import Dataset again"):
                ui._build_reconstruction_args()
            ui.slice_layer_var.set("0")
            self.assertEqual(ui._slice_spec().layer, 0)
            ui.slice_layer_var.set("-2")
            self.assertEqual(ui._slice_spec().layer, -2)
            ui.slice_layer_var.set("0.5")
            with self.assertRaisesRegex(ValueError, "must be an integer"):
                ui._slice_spec()
            self.assertTrue(ui.slice_cell_var.get())
            self.assertEqual(ui.slice_threshold_var.get(), 0.0)
            self.assertTrue(ui.embedded_cell_var.get())
            self.assertFalse(ui.embedded_indexed_var.get())
            self.assertEqual(float(ui.embedded_point_size_var.get()), 2.0)
            self.assertEqual(float(ui.embedded_brightness_var.get()), 3.0)
            self.assertEqual(float(ui.embedded_contrast_var.get()), 3.0)
            controls = ui.embedded_panel_controls
            labels = [
                str(widget.cget("text"))
                for widget in controls.winfo_children()
                if isinstance(widget, ttk.Label) and str(widget.cget("text"))
            ]
            buttons = [
                str(widget.cget("text"))
                for widget in controls.winfo_children()
                if isinstance(widget, ttk.Button)
            ]
            self.assertNotIn("Points", labels)
            self.assertNotIn("Mode", labels)
            self.assertIn("Brightness", labels)
            self.assertIn("Contrast", labels)
            self.assertIn("Gamma", labels)
            self.assertNotIn("Reset View", buttons)
            self.assertFalse(
                any(isinstance(widget, ttk.Combobox) for widget in controls.winfo_children())
            )
            self.assertEqual(int(ui.embedded_threshold_scale.grid_info()["columnspan"]), 10)
            tone_scales = [
                widget
                for widget in controls.winfo_children()
                if isinstance(widget, ttk.Scale) and widget is not ui.embedded_threshold_scale
            ]
            self.assertEqual(len(tone_scales), 4)
            self.assertEqual({int(widget.grid_info()["columnspan"]) for widget in tone_scales}, {2})
            self.assertTrue(ui.embedded_indexed_check.instate(["disabled"]))
            indexed_mask = ui._indexed_point_mask(
                np.asarray([[1.05, 2.0, -3.09], [1.11, 2.0, 3.0], [np.nan, 0.0, 0.0]])
            )
            np.testing.assert_array_equal(indexed_mask, np.asarray([True, False, False]))
            ui._on_embedded_threshold("42")
            self.assertEqual(ui.embedded_threshold_label_var.get(), "42%")
            weak = ui._intensity_color(0.0, 0.0, 100.0)
            strong = ui._intensity_color(100.0, 0.0, 100.0)
            self.assertEqual(weak, (11, 13, 16))
            self.assertGreater(sum(strong), sum(weak))
            ui.embedded_brightness_var.set(0.0)
            self.assertEqual(ui._intensity_color(100.0, 0.0, 100.0), (11, 13, 16))
            ui.embedded_brightness_var.set(1.0)
            point_canvas = mock.Mock()
            ui._draw_embedded_point(point_canvas, 10.0, 20.0, 3.0, strong)
            point_canvas.create_oval.assert_called_once()
            ui.set_embedded_axis_view("a")
            self.assertAlmostEqual(ui.embedded_yaw, np.pi / 2.0)
            self.assertAlmostEqual(ui.embedded_pitch, 0.0)
            np.testing.assert_allclose(ui.embedded_view_frame[:, 1], np.asarray([0.0, 0.0, 1.0]))
            ui.set_embedded_axis_view("c")
            self.assertAlmostEqual(ui.embedded_pitch, -np.pi / 2.0)
            self.assertEqual(ui.embedded_roll, 0.0)
            np.testing.assert_allclose(ui.embedded_view_frame[:, 1], np.asarray([0.0, 1.0, 0.0]))
            monoclinic_basis = np.asarray(
                [
                    [0.05867, 0.0, 0.03807],
                    [0.0, 0.03453, 0.0],
                    [0.0, 0.0, 0.09183],
                ],
                dtype=np.float64,
            )
            ui.embedded_reciprocal_basis = monoclinic_basis
            ui.set_embedded_axis_view("c")
            expected_yaw, expected_pitch = ui._embedded_view_angles(monoclinic_basis[:, 2])
            self.assertAlmostEqual(ui.embedded_yaw, expected_yaw)
            self.assertAlmostEqual(ui.embedded_pitch, expected_pitch)
            self.assertNotAlmostEqual(ui.embedded_yaw, 0.0)
            self.assertAlmostEqual(
                float(np.dot(monoclinic_basis[:, 1], ui.embedded_view_frame[:, 0])), 0.0, places=10
            )
            self.assertGreater(float(np.dot(monoclinic_basis[:, 1], ui.embedded_view_frame[:, 1])), 0.0)
            initial_frame = ui.embedded_view_frame.copy()
            ui.embedded_drag_start = (0, 0, initial_frame, "vertical")
            ui._embedded_drag_move(mock.Mock(x=0, y=200))
            self.assertFalse(np.allclose(ui.embedded_view_frame[:, 2], initial_frame[:, 2]))
            initial_frame = ui.embedded_view_frame.copy()
            ui.embedded_drag_start = (0, 0, initial_frame, "orbit")
            ui._embedded_drag_move(mock.Mock(x=200, y=-200))
            self.assertFalse(np.allclose(ui.embedded_view_frame, initial_frame))
            np.testing.assert_allclose(
                ui.embedded_view_frame.T @ ui.embedded_view_frame,
                np.eye(3),
                atol=1e-12,
            )
        finally:
            destroy_test_root(root)

    def test_ai_suggestion_button_offline_uses_real_xds(self):
        try:
            import tkinter as tk
            from UI.ui import AutoR3DUI
            root = tk.Tk()
        except Exception as exc:
            self.skipTest(f"Tk UI unavailable: {exc}")
        root.withdraw()
        try:
            ui = AutoR3DUI(root, ai_assistant=True)
            ui._ai_panel._llm = NullBackend()
            ui.xds_set_selected_datasets(
                [{"name": "sample2_2", "path": str(ROOT / "Data" / "sample2_2")}]
            )
            ui._ai_panel._request_suggestion()
            deadline = time.time() + 8
            transcript = ""
            while time.time() < deadline:
                root.update()
                time.sleep(0.03)
                transcript = ui.ai_message_log.get("1.0", "end")
                if "[AI 建议]" in transcript:
                    break
            self.assertIn("判断：", transcript)
            self.assertIn("建议：", transcript)
        finally:
            destroy_test_root(root)

    def test_explicit_no_ai_variant_has_only_console(self):
        try:
            import tkinter as tk
            from UI.ui import AutoR3DUI
            root = tk.Tk()
        except Exception as exc:
            self.skipTest(f"Tk UI unavailable: {exc}")
        root.withdraw()
        try:
            ui = AutoR3DUI(root, ai_assistant=False)
            self.assertFalse(ui.ai_assistant_enabled)
            self.assertFalse(hasattr(ui, "_ai_panel"))
            self.assertTrue(ui.console_log.winfo_exists())
        finally:
            destroy_test_root(root)

    @unittest.skipUnless(
        os.environ.get("RUN_OLLAMA_TESTS") == "1",
        "set RUN_OLLAMA_TESTS=1 for local model integration",
    )
    def test_ai_suggestion_button_with_real_xds_and_ollama(self):
        try:
            import tkinter as tk
            from UI.ui import AutoR3DUI
            root = tk.Tk()
        except Exception as exc:
            self.skipTest(f"Tk UI unavailable: {exc}")
        root.withdraw()
        temp_config = tempfile.TemporaryDirectory(prefix="autocrys-local-ui-")
        try:
            local_config = json.loads(json.dumps(Config()._data))
            local_config.setdefault("llm", {})["mode"] = "local"
            local_path = Path(temp_config.name) / "ai_assistant.json"
            local_path.write_text(json.dumps(local_config), encoding="utf-8")
            original_init = Config.__init__

            def init_with_local_config(instance, config_path=None):
                original_init(instance, local_path)

            with mock.patch.object(Config, "__init__", init_with_local_config):
                ui = AutoR3DUI(root, ai_assistant=True)
            self.assertIsInstance(ui._ai_panel._llm, OllamaBackend)
            ui.xds_set_selected_datasets(
                [{"name": "sample2_2", "path": str(ROOT / "Data" / "sample2_2")}]
            )
            ui._ai_panel._request_suggestion()
            deadline = time.time() + 90
            transcript = ""
            while time.time() < deadline:
                root.update()
                time.sleep(0.05)
                transcript = ui.ai_message_log.get("1.0", "end")
                if "[AI 建议]" in transcript:
                    break
            self.assertIn("[AI 建议]", transcript)
            suggestion = transcript.rsplit("[AI 建议]\n", 1)[-1].strip()
            self.assertTrue("判断" in suggestion or "问题" in suggestion)
            self.assertIn("建议", suggestion)
            self.assertGreater(len(suggestion), 20)
        finally:
            destroy_test_root(root)
            temp_config.cleanup()

    def test_ai_suggestion_reviews_previous_hca_operation(self):
        try:
            import tkinter as tk
            from UI.ui import AutoR3DUI
            root = tk.Tk()
        except Exception as exc:
            self.skipTest(f"Tk UI unavailable: {exc}")
        root.withdraw()
        try:
            ui = AutoR3DUI(root, ai_assistant=True)
            ui._ai_panel._llm = NullBackend()
            hca = AssistantModeTests._hca_result()
            ui._ai_context.start_operation("AutoXDS HCA Run", "cluster")
            ui._ai_context.append_console("37 common reflections")
            ui._ai_context.record_operation_result("AutoXDS HCA Run", hca)
            ui._ai_context.finish_operation("AutoXDS HCA Run", success=True)
            ui._ai_context.update_hca_result(hca, None)
            ui._ai_panel._request_suggestion()
            deadline = time.time() + 8
            transcript = ""
            while time.time() < deadline:
                root.update()
                time.sleep(0.03)
                transcript = ui.ai_message_log.get("1.0", "end")
                if "[AI 建议]" in transcript:
                    break
            suggestion = transcript.rsplit("[AI 建议]\n", 1)[-1]
            self.assertIn("HCA分析了2个数据集", suggestion)
            self.assertIn("共同反射最少37", suggestion)
            self.assertLessEqual(len(suggestion.strip()), 1200)
        finally:
            destroy_test_root(root)

    def test_ai_has_a_main_workspace_separate_from_console(self):
        try:
            import tkinter as tk
            from tkinter import ttk
            from UI.ui import AutoR3DUI
            root = tk.Tk()
        except Exception as exc:
            self.skipTest(f"Tk UI unavailable: {exc}")
        root.withdraw()
        try:
            ui = AutoR3DUI(root, ai_assistant=True)
            self.assertIs(ui._ai_panel._msg_text, ui.ai_message_log)
            self.assertIsNot(ui.ai_message_log, ui.console_log)
            tabs = [ui._notebook.tab(tab_id, "text") for tab_id in ui._notebook.tabs()]
            self.assertIn("Assistant", tabs)
            self.assertEqual(ui._ai_panel._suggest_button.cget("text"), "AI建议")
            self.assertEqual(ui._ai_panel._send_button.cget("text"), "Send")
            self.assertFalse(any(isinstance(widget, ttk.PanedWindow) for widget in ui.root.winfo_children()))
        finally:
            destroy_test_root(root)

    def test_agent_task_result_switches_to_matching_workflow_panel(self):
        try:
            import tkinter as tk
            from UI.ui import AutoR3DUI
            root = tk.Tk()
        except Exception as exc:
            self.skipTest(f"Tk UI unavailable: {exc}")
        root.withdraw()
        try:
            ui = AutoR3DUI(root, ai_assistant=True)
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps({"status": "ok", "result": "solved"}),
                stderr="",
            )
            ui._show_agent_task_result(
                {"tab": "AutoSolve", "skill_id": "solve_shelxt"},
                "AutoSolve · solve_shelxt",
                completed,
                None,
            )
            self.assertEqual(ui._notebook.tab(ui._notebook.select(), "text"), "AutoSolve")
            self.assertIn("solved", ui.solve_preview.get("1.0", "end"))

            ui._show_agent_task_result(
                {"tab": "AutoXDS", "skill_id": "process_xds"},
                "AutoXDS · process_xds",
                completed,
                None,
            )
            self.assertEqual(ui._notebook.tab(ui._notebook.select(), "text"), "AutoXDS")
            self.assertIn("solved", ui.xds_center_text.get("1.0", "end"))
        finally:
            destroy_test_root(root)

    def test_merge_confirmation_keeps_all_explicit_datasets(self):
        try:
            import tkinter as tk
            from UI.ui import AutoR3DUI
            root = tk.Tk()
        except Exception as exc:
            self.skipTest(f"Tk UI unavailable: {exc}")

        root.withdraw()
        try:
            ui = AutoR3DUI(root, ai_assistant=True)
            panel = ui._ai_panel
            panel._llm = NullBackend()
            panel._process_message("can you merge sample1_2 and sample2_1?")
            self.assertIsNotNone(panel._pending_skill)
            self.assertEqual(panel._pending_skill[0].skill_id, "merge_and_run")
            self.assertEqual(panel._pending_argv.count("--dataset"), 2)
            self.assertIn("--run", panel._pending_argv)
            self.assertIn("Datasets (2): sample1_2, sample2_1", panel._skill_label.cget("text"))
            self.assertIn('--dataset "sample1_2" --dataset "sample2_1"', panel._pending_command)

            panel._cancel_skill()
            panel._process_message("merge sample1_2")
            self.assertIsNone(panel._pending_skill)
            transcript = panel._msg_text.get("1.0", "end")
            self.assertIn("datasets (at least 2)", transcript)
        finally:
            destroy_test_root(root)

    def test_online_model_selects_skill_after_reading_catalog(self):
        try:
            import tkinter as tk
            from UI.ui import AutoR3DUI
            root = tk.Tk()
        except Exception as exc:
            self.skipTest(f"Tk UI unavailable: {exc}")

        root.withdraw()
        try:
            ui = AutoR3DUI(root, ai_assistant=True)
            panel = ui._ai_panel
            backend = mock.Mock()
            backend.chat.side_effect = [
                '<autocrys_context>{"modules":["AutoXDS"]}</autocrys_context>',
                (
                    "我会先读取数据集列表。\n"
                    '<autocrys_action>{"skill_id":"list_datasets","params":{}}</autocrys_action>'
                ),
            ]
            panel._llm = backend
            panel._process_message("请根据 skill 自己判断下一步")
            deadline = time.time() + 4
            while time.time() < deadline and panel._pending_skill is None:
                root.update()
                time.sleep(0.02)
            self.assertIsNotNone(panel._pending_skill)
            self.assertEqual(panel._pending_skill[0].skill_id, "list_datasets")
            self.assertEqual(backend.chat.call_count, 2)
            first_prompt, _ = backend.chat.call_args_list[0].args
            system_prompt, _ = backend.chat.call_args_list[1].args
            self.assertNotIn("AVAILABLE EXECUTABLE SKILLS", first_prompt)
            self.assertNotIn("--- AutoXDS ---", first_prompt)
            self.assertIn("AVAILABLE EXECUTABLE SKILLS", system_prompt)
            self.assertIn("--- AutoXDS ---", system_prompt)
            self.assertNotIn("--- AutoR3D ---", system_prompt)
        finally:
            destroy_test_root(root)

    def test_larger_fonts_and_dense_forms_fit_wsl_window(self):
        try:
            import tkinter as tk
            from tkinter import font as tkfont, ttk
            from UI.ui import AutoR3DUI
            root = tk.Tk()
        except Exception as exc:
            self.skipTest(f"Tk UI unavailable: {exc}")

        try:
            ui = AutoR3DUI(root, ai_assistant=True)
            root.geometry("1560x930+0+0")
            root.update_idletasks()
            root.update()

            style = ttk.Style(root)
            main_font = tkfont.Font(root, font=style.lookup("TLabel", "font"))
            ai_font = tkfont.Font(root, font=style.lookup("AI.TLabel", "font"))
            console_font = tkfont.Font(root, font=ui.console_log.cget("font"))
            self.assertGreaterEqual(main_font.metrics("linespace"), 16)
            self.assertGreaterEqual(ai_font.metrics("linespace"), 16)
            self.assertGreaterEqual(console_font.metrics("linespace"), 16)
            self.assertIn(
                main_font.actual("family").casefold(),
                {
                    "microsoft yahei ui",
                    "microsoft yahei",
                    "dengxian",
                    "noto sans cjk sc",
                    "source han sans sc",
                    "wenquanyi zen hei",
                    "simhei",
                    "simsun",
                    "nsimsun",
                    "song ti",
                    "fangsong ti",
                    "gothic",
                },
            )
            self.assertIn(
                console_font.actual("family").casefold(),
                {"consolas", "cascadia mono", "dejavu sans mono", "liberation mono", "courier new"},
            )
            xds_font = tkfont.Font(root, font=ui.xds_center_text.cget("font"))
            self.assertEqual(xds_font.actual("family"), console_font.actual("family"))
            input_font = tkfont.Font(root, font=ui._ai_panel._input_entry.cget("font"))
            self.assertEqual(input_font.actual("family"), main_font.actual("family"))
            self.assertGreaterEqual(int(ui.console_log.cget("spacing3")), 8)

            # Flexible AI text must not request the old 80-character width.
            for tab_id in ui._notebook.tabs():
                if ui._notebook.tab(tab_id, "text") == "Assistant":
                    ui._notebook.select(tab_id)
                    break
            root.update_idletasks()
            root.update()
            self.assertLessEqual(ui._ai_panel.winfo_reqwidth(), ui._ai_panel.winfo_width())

            for tab_id in ui._notebook.tabs():
                if ui._notebook.tab(tab_id, "text") == "AutoXDS":
                    ui._notebook.select(tab_id)
                    break
            root.update_idletasks()
            root.update()
            self.assertGreaterEqual(ui.xds_root_entry.winfo_width(), 140)

            for tab_id in ui._notebook.tabs():
                if ui._notebook.tab(tab_id, "text") == "AutoSolve":
                    ui._notebook.select(tab_id)
                    break
            root.update_idletasks()
            root.update()
            self.assertGreaterEqual(ui.solve_workdir_entry.winfo_width(), 140)
        finally:
            destroy_test_root(root)

    def test_compact_console_filters_chatter_but_keeps_failures(self):
        try:
            import tkinter as tk
            from UI.ui import AutoR3DUI
            root = tk.Tk()
        except Exception as exc:
            self.skipTest(f"Tk UI unavailable: {exc}")
        root.withdraw()
        try:
            compact = AutoR3DUI(root, ai_assistant=False, console_verbose=False)
            compact.console_log.delete("1.0", "end")
            compact._console_log("routine subprocess detail", detail=True)
            compact._console_log("ERROR: real failure", detail=True)
            transcript = compact.console_log.get("1.0", "end")
            self.assertNotIn("routine subprocess detail", transcript)
            self.assertIn("ERROR: real failure", transcript)
        finally:
            destroy_test_root(root)

    def test_main_console_preserves_chatter_and_blank_lines(self):
        try:
            import tkinter as tk
            from UI.ui import AutoR3DUI
            root = tk.Tk()
        except Exception as exc:
            self.skipTest(f"Tk UI unavailable: {exc}")
        root.withdraw()
        try:
            verbose = AutoR3DUI(root, ai_assistant=False, console_verbose=True)
            verbose.console_log.delete("1.0", "end")
            verbose._console_log("routine subprocess detail", detail=True)
            verbose._console_log("")
            self.assertEqual(
                verbose.console_log.get("1.0", "end"),
                "routine subprocess detail\n\n\n",
            )
        finally:
            destroy_test_root(root)

    @unittest.skipUnless((ROOT / "Data" / "sample2_2" / "diff" / "p" / "CORRECT.LP").is_file() and (ROOT / "Demo" / "AutoSolve" / "1.res").is_file(), "optional private integration fixture is not distributed")
    def test_real_panel_tracks_xds_r3d_and_solve(self):
        try:
            import tkinter as tk
            from UI.ui import AutoR3DUI
            root = tk.Tk()
        except Exception as exc:
            self.skipTest(f"Tk UI unavailable: {exc}")
        root.withdraw()
        ui = AutoR3DUI(root, ai_assistant=True)

        def select(name: str) -> None:
            for tab_id in ui._notebook.tabs():
                if ui._notebook.tab(tab_id, "text") == name:
                    ui._notebook.select(tab_id)
                    root.update()
                    return
            self.fail(f"missing tab {name}")

        def wait_for(module: str):
            deadline = time.time() + 6
            while time.time() < deadline:
                root.update()
                time.sleep(0.03)
                report = ui._ai_context.artifact_report
                if report and report.module == module:
                    return report
            self.fail(f"no {module} report: {ui._ai_context.artifact_report}")

        try:
            select("AutoXDS")
            xds_path = ROOT / "Data" / "sample2_2"
            ui.xds_set_selected_datasets([{"name": "sample2_2", "path": str(xds_path)}])
            self.assertEqual(wait_for("AutoXDS").status, "complete")

            ui._console_log("!!! ERROR !!! CANNOT OPEN OR READ FILE bin1_04.tmp")
            root.update()
            self.assertGreaterEqual(ui._ai_context.diagnosis_count, 1)

            live_states = []
            ui._ai_context.on(
                "console_output",
                lambda **event: live_states.append(ui._ai_context.operation_state)
                if event.get("line") == "stream-start" else None,
            )
            ui._run_command_async(
                "stream smoke",
                [sys.executable, "-u", "-c", "import time; print('stream-start', flush=True); time.sleep(.6); print('stream-end', flush=True)"],
            )
            deadline = time.time() + 3
            saw_live_line = False
            while time.time() < deadline and ui._ai_context.operation_state == "processing":
                root.update()
                time.sleep(0.02)
                if "stream-start" in ui._ai_context.console_buffer:
                    saw_live_line = True
            self.assertTrue(saw_live_line)
            self.assertIn("processing", live_states)
            self.assertTrue(any(item["name"] == "stream smoke" for item in ui._ai_context.processing_history))
            visible_console = ui.console_log.get("1.0", "end")
            self.assertNotIn("stream-start", visible_console)
            self.assertIn("[exit 0]", visible_console)

            select("AutoR3D")
            ui.dataset_path = ROOT / "Demo" / "AutoR3D_Demo_single_legacy"
            ui.dataset_var.set(str(ui.dataset_path))
            ui._ai_context.update_dataset(ui.dataset_path, "Demo_single")
            self.assertEqual(wait_for("AutoR3D").status, "complete")

            select("AutoSolve")
            ui.solve_workdir_var.set(str(ROOT / "Demo" / "AutoSolve"))
            ui.solve_basename_var.set("1")
            ui._ai_panel._request_analysis()
            self.assertEqual(wait_for("AutoSolve").status, "solved_success")
        finally:
            destroy_test_root(root)


class SkillArgumentTests(unittest.TestCase):
    def test_refine_skill_uses_selected_res_and_hkl(self):
        mapper = SkillMapper(ROOT)
        res = ROOT / "Demo" / "AutoSolve" / "1.res"
        hkl = ROOT / "Demo" / "AutoSolve" / "1.hkl"
        skill, params = mapper.match(
            "refine this structure with shelxl",
            {"res": res, "hkl": hkl},
        )[0]
        self.assertEqual(skill.skill_id, "refine_shelxl")
        self.assertEqual(params, {"res": str(res), "hkl": str(hkl)})
        argv = skill.build_argv(**params)
        self.assertIn("--open-olex2", argv)
        self.assertIn(str(res), argv)
        self.assertIn(str(hkl), argv)

    def test_explicit_single_dataset_overrides_selected_context(self):
        mapper = SkillMapper(ROOT)
        skill, params = mapper.match(
            "process sample2_1",
            {"root": ROOT / "Data", "dataset_name": "sample1_2"},
        )[0]
        self.assertEqual(skill.skill_id, "process_xds")
        self.assertEqual(params["dataset"], "sample2_1")

    def test_cluster_repeats_dataset_option_for_three_names(self):
        mapper = SkillMapper(ROOT)
        skill, params = mapper.match(
            "cluster sample1_2, sample2_1 and sample2_2",
            {"root": ROOT / "Data", "dataset_name": "sample1_2"},
        )[0]
        self.assertEqual(skill.skill_id, "cluster_hca")
        self.assertEqual(params["datasets"], ["sample1_2", "sample2_1", "sample2_2"])
        argv = skill.build_argv(**params)
        self.assertEqual(argv.count("--dataset"), 3)

    def test_chinese_merge_keeps_both_names(self):
        mapper = SkillMapper(ROOT)
        skill, params = mapper.match(
            "请合并 sample1_2 和 sample2_1",
            {"root": ROOT / "Data"},
        )[0]
        self.assertEqual(skill.skill_id, "merge_and_run")
        self.assertEqual(params["datasets"], ["sample1_2", "sample2_1"])

    def test_merge_extracts_every_explicit_dataset_and_runs(self):
        mapper = SkillMapper(ROOT)
        skill, params = mapper.match(
            "can you merge sample1_2 and sample2_1?",
            {"root": ROOT / "Data", "dataset_name": "sample1_2"},
        )[0]
        self.assertEqual(skill.skill_id, "merge_and_run")
        self.assertEqual(params["datasets"], ["sample1_2", "sample2_1"])
        argv = skill.build_argv(**params)
        self.assertEqual(argv.count("--dataset"), 2)
        self.assertIn("--run", argv)
        self.assertEqual(
            [argv[index + 1] for index, value in enumerate(argv) if value == "--dataset"],
            ["sample1_2", "sample2_1"],
        )

    def test_merge_uses_selected_datasets_when_names_are_implicit(self):
        mapper = SkillMapper(ROOT)
        skill, params = mapper.match(
            "merge the selected datasets",
            {"root": ROOT / "Data", "datasets": ["sample1_2", "sample2_1"]},
        )[0]
        self.assertEqual(skill.skill_id, "merge_and_run")
        self.assertEqual(params["datasets"], ["sample1_2", "sample2_1"])

    def test_merge_with_only_one_dataset_is_rejected_before_execution(self):
        mapper = SkillMapper(ROOT)
        skill, params = mapper.match(
            "merge sample1_2",
            {"root": ROOT / "Data", "datasets": ["sample1_2", "sample2_1"]},
        )[0]
        self.assertEqual(params["datasets"], ["sample1_2"])
        self.assertIn("datasets (at least 2)", skill.missing_params(params))

    def test_prepare_merge_requires_explicit_prepare_wording(self):
        mapper = SkillMapper(ROOT)
        skill, params = mapper.match(
            "prepare merge inputs for sample1_2 and sample2_1",
            {"root": ROOT / "Data"},
        )[0]
        self.assertEqual(skill.skill_id, "merge_datasets")
        self.assertNotIn("--run", skill.build_argv(**params))

    def test_quoted_composition_stays_one_argument(self):
        mapper = SkillMapper(ROOT)
        skill = next(item for item in mapper.available_skills if item.skill_id == "solve_shelxt")
        argv = skill.build_argv(dataset="folder with spaces/data", composition="Au4 C20 H16 N2", sg="P2(1)/c")
        self.assertIn("folder with spaces/data", argv)
        self.assertIn("Au4 C20 H16 N2", argv)
        self.assertIn("P2(1)/c", argv)

    def test_missing_required_parameters_are_reported(self):
        mapper = SkillMapper(ROOT)
        skill = next(item for item in mapper.available_skills if item.skill_id == "solve_shelxt")
        self.assertEqual(skill.missing_params({"dataset": "x"}), ["composition", "sg"])

    def test_real_chinese_intents_choose_specific_skills(self):
        mapper = SkillMapper(ROOT)
        context = {"root": ROOT / "Data", "dataset_name": "sample2_2"}
        self.assertEqual(mapper.match("请合并并运行这些数据", context)[0][0].skill_id, "merge_and_run")
        self.assertEqual(mapper.match("请对数据做 HCA 聚类", context)[0][0].skill_id, "cluster_hca")
        solve_context = {"dataset_name": "sample2_2", "composition": "C20 H16 N2", "sg": "62"}
        self.assertEqual(mapper.match("求解结构并打开 Olex2", solve_context)[0][0].skill_id, "solve_shelxt_with_olex2")


if __name__ == "__main__":
    unittest.main(verbosity=2)
