#!/usr/bin/env python3
"""Comprehensive end-to-end tests for the AI Assistant module.

Tests all components: rules, skills, knowledge, monitor, context, config, LLM.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from UI.ai_assistant.config import Config
from UI.ai_assistant.context import AIContext
from UI.ai_assistant.knowledge import KnowledgeBase
from UI.ai_assistant.llm import create_llm, NullBackend, OllamaBackend, OpenAIBackend, AnthropicBackend
from UI.ai_assistant.monitor import LogMonitor
from UI.ai_assistant.rules import Diagnosis, Rule, RuleEngine, build_default_engine
from UI.ai_assistant.skills import SkillAction, SkillMapper

PASS = 0
FAIL = 0
SKIP = 0


def t(desc, result):
    global PASS, FAIL, SKIP
    if result is True:
        PASS += 1
        print(f"  PASS  {desc}")
    elif result == "SKIP":
        SKIP += 1
        print(f"  SKIP  {desc}")
    else:
        FAIL += 1
        print(f"  FAIL  {desc}  --> {result}")


def eq(a, b, desc):
    if a == b:
        t(desc, True)
    else:
        t(desc, f"expected {b!r}, got {a!r}")


def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


# ============================================================
# 1. CONFIG TESTS
# ============================================================
section("1. CONFIG")

test_config_path = Path(tempfile.mkdtemp(prefix="autocrys-ai-config-")) / "config.json"
cfg = Config(test_config_path)
eq(cfg.llm_mode, "auto", "Default mode auto-detects a backend")
eq(cfg.assistant_enabled, False, "AI assistant is disabled by default")
eq(cfg.max_suggestion_chars, 300, "Suggestion output defaults to 300 characters")
eq(cfg.auto_diagnose, True, "Auto-diagnose enabled by default")
eq(cfg.max_console_lines, 200, "Default max console lines")
eq(cfg.language, "zh", "Default language is zh")

cfg.llm_mode = "local"
eq(cfg.llm_mode, "local", "Mode can be changed to local")
cfg.llm_mode = "offline"
cfg.auto_diagnose = False
eq(cfg.auto_diagnose, False, "Auto-diagnose can be disabled")
cfg.auto_diagnose = True  # restore


# ============================================================
# 2. RULE ENGINE TESTS
# ============================================================
section("2. RULE ENGINE")

engine = build_default_engine()
eq(engine.rule_count, 13, "13 default rules loaded")

# Test XDS INSUFFICIENT PERCENTAGE
diags = engine.feed_line("!!! ERROR !!! INSUFFICIENT PERCENTAGE (< 50%) of reflections", "AutoXDS")
t("Detects INSUFFICIENT PERCENTAGE error", len(diags) >= 1)
if diags:
    d = diags[0]
    eq(d.rule_id, "xds_insufficient_percentage", "Correct rule_id for insufficient pct")
    eq(d.severity, "error", "Correct severity")
    eq(d.fix_id, "xds_recovery", "Has fix_id")

# Test WSL processor error
engine.reset_cooldowns()
diags = engine.feed_line("!!! ERROR !!! CANNOT OPEN OR READ FILE bin1_04.tmp", "AutoXDS")
t("Detects WSL processor error", any(d.rule_id == "xds_cannot_open_tmp" for d in diags))

# Test XSCALE misplaced param
engine.reset_cooldowns()
diags = engine.feed_line("!!! ERROR !!! MISPLACED PARAMETER", "AutoXDS")
t("Detects MISPLACED PARAMETER", any(d.rule_id == "xscale_misplaced_param" for d in diags))

# Test high R-factor
engine.reset_cooldowns()
diags = engine.feed_line("Rfactor: 0.245", "AutoXDS")
t("Detects high R-factor", len(diags) >= 1)
if diags:
    t("R-factor diag is warning", any(d.severity == "warning" for d in diags))

# Test low completeness
engine.reset_cooldowns()
diags = engine.feed_line("COMPLETNESS 59.2%", "AutoXDS")
t("Detects low completeness", len(diags) >= 1)

# Test no_solution
engine.reset_cooldowns()
diags = engine.feed_line("shelxt_no_solution", "AutoSolve")
t("Detects SHELXT no_solution", any(d.rule_id == "no_solution" for d in diags))

# Test shelxt_failed
engine.reset_cooldowns()
diags = engine.feed_line("input_preparation_failed", "AutoSolve")
t("Detects SHELXT failed", any(d.rule_id == "shelxt_failed" for d in diags))

# Test XDS general error
engine.reset_cooldowns()
diags = engine.feed_line("!!! ERROR !!! REDUCE returns IRANK=2", "AutoXDS")
t("Detects XDS general error (IRANK=2)", any(d.rule_id == "xds_general_error" for d in diags))

# Test XSCALE warning
engine.reset_cooldowns()
diags = engine.feed_line("!!! WARNING !!! Something in XSCALE", "AutoXDS")
t("Detects XSCALE warning", any(d.rule_id == "xscale_warning" for d in diags))

# Test context filtering - rules only fire on matching tabs
engine.reset_cooldowns()
diags = engine.feed_line("shelxt_no_solution", "AutoXDS")
t("SHELXT rule does NOT fire on AutoXDS tab", len(diags) == 0)

# Test cooldown
engine.reset_cooldowns()
d1 = engine.feed_line("!!! ERROR !!! INSUFFICIENT PERCENTAGE (< 50%)", "AutoXDS")
d2 = engine.feed_line("!!! ERROR !!! INSUFFICIENT PERCENTAGE (< 50%)", "AutoXDS")
t("Cooldown prevents duplicate insufficient pct",
  not any(d.rule_id == "xds_insufficient_percentage" for d in d2))

# Test severity_icon and severity_tag
diag = Diagnosis(rule_id="test", severity="error", title="T", detail="D", suggestion="S")
eq(diag.severity_icon, "[ERROR]", "Error icon")
eq(diag.severity_tag, "diag_error", "Error tag")
diag2 = Diagnosis(rule_id="test2", severity="warning", title="T", detail="D", suggestion="S")
eq(diag2.severity_icon, "[WARN]", "Warning icon")
diag3 = Diagnosis(rule_id="test3", severity="info", title="T", detail="D", suggestion="S")
eq(diag3.severity_icon, "[INFO]", "Info icon")


# ============================================================
# 3. KNOWLEDGE BASE TESTS
# ============================================================
section("3. KNOWLEDGE BASE")

kb = KnowledgeBase(ROOT)
mods = kb.modules()
t("Has 4 modules", len(mods) == 4)
t("Has AutoXDS", "AutoXDS" in mods)
t("Has AutoR3D", "AutoR3D" in mods)
t("Has AutoSolve", "AutoSolve" in mods)
t("Has AutoRefine", "AutoRefine" in mods)

# Test section access
scope = kb.get_section("AutoXDS", "Scope")
t("AutoXDS scope has content", len(scope) > 100)
t("Scope mentions dataset", "dataset" in scope.lower())

# Test full content
full = kb.get_full("AutoXDS")
t("AutoXDS full content > 500 chars", len(full) > 500)
t("Full contains NAME_TEMPLATE", "NAME_TEMPLATE" in full)

# Test search
hits = kb.search("NAME_TEMPLATE")
t("Search finds NAME_TEMPLATE", len(hits) >= 1)
hits2 = kb.search("INSUFFICIENT PERCENTAGE")
t("Search finds INSUFFICIENT PERCENTAGE", len(hits2) >= 1)
hits3 = kb.search("nxostng")  # nonsense
t("Search returns empty for nonsense", len(hits3) == 0)

# Test system prompt generation
prompt = kb.build_system_prompt()
t("System prompt has content", len(prompt) > 500)
t("System prompt mentions XDS", "XDS" in prompt)
t("System prompt has knowledge section", "AVAILABLE KNOWLEDGE" in prompt)


# ============================================================
# 4. CONTEXT TESTS
# ============================================================
section("4. CONTEXT TRACKER")

ctx = AIContext()
eq(ctx.active_tab, "", "Initial tab is empty")
eq(ctx.operation_state, "idle", "Initial state is idle")

ctx.update_tab("AutoXDS")
eq(ctx.active_tab, "AutoXDS", "Tab updated")

ctx.update_dataset(Path("/tmp/experiment_003"), "experiment_003")
eq(ctx.dataset_name, "experiment_003", "Dataset name set")

ctx.start_operation("XDS Process", "xds process --dataset experiment_003")
eq(ctx.operation_state, "processing", "State is processing")
eq(ctx.operation_name, "XDS Process", "Operation name set")

ctx.finish_operation("XDS Process", success=True)
eq(ctx.operation_state, "done", "State is done after success")

ctx.set_idle()
eq(ctx.operation_state, "idle", "State returns to idle")

# Test console buffer
ctx.append_console("Hello World")
ctx.append_console("Error line")
eq(len(ctx.console_buffer), 2, "Console buffer has 2 lines")

# Test history
ctx.start_operation("HCA", None)
ctx.finish_operation("HCA", success=True)
eq(len(ctx.processing_history), 2, "History has 2 entries")
eq(ctx.processing_history[0]["name"], "XDS Process", "First entry correct")

# Test diagnosis counting
ctx.add_diagnosis(None)
eq(ctx.diagnosis_count, 1, "Diagnosis count increments")

# Test event listeners
events = []
def my_listener(**data):
    events.append(data)
ctx.on("test_event", my_listener)
ctx.emit("test_event", value=42)
eq(len(events), 1, "Event listener works")
eq(events[0]["value"], 42, "Event data correct")

# Test build_prompt_context
ctx2 = AIContext()
ctx2.update_tab("AutoSolve")
ctx2.update_dataset(Path("/tmp/exp_1"), "exp_1")
ctx2.update_status("SHELXT completed")
ctx2.append_console("Solution found")
prompt_ctx = ctx2.build_prompt_context()
t("Prompt context has tab info", "AutoSolve" in prompt_ctx)
t("Prompt context has dataset", "exp_1" in prompt_ctx)
t("Prompt context has log", "Solution found" in prompt_ctx)

# Test summary rows
ctx.update_summary_rows({"exp_1": {"Cell": "11.83 7.42 26.64", "SG": "4"}})
t("Summary rows stored", "exp_1" in ctx.summary_rows)


# ============================================================
# 5. MONITOR TESTS
# ============================================================
section("5. MONITOR")

mon = LogMonitor()
eq(mon.active, True, "Monitor starts active")
eq(mon.diagnosis_count, 0, "No diagnoses initially")

# Feed a problematic XDS line
diags = mon.feed_single(
    "!!! ERROR !!! INSUFFICIENT PERCENTAGE (< 50%) of expected reflections",
    "AutoXDS"
)
t("Monitor detects XDS error", len(diags) >= 1)
if diags:
    t("Monitor diag has correct rule_id", diags[0].rule_id == "xds_insufficient_percentage")

# Feed a WSL error
diags2 = mon.feed_single(
    "!!! ERROR !!! CANNOT OPEN OR READ FILE bin1_01.tmp",
    "AutoXDS"
)
t("Monitor detects WSL error", len(diags2) >= 1)

# Feed a line on wrong tab (should NOT fire)
mon.engine.reset_cooldowns()
diags3 = mon.feed_single("shelxt_no_solution", "AutoXDS")
t("Monitor respects tab context (no false positive)", len(diags3) == 0)

# Test deactivation
mon.active = False
diags4 = mon.feed_single("!!! ERROR !!! anything", "")
eq(len(diags4), 0, "Inactive monitor returns empty")
mon.active = True

# Test line history
mon.clear()
for i in range(10):
    mon.feed_single(f"line {i}", "")
eq(len(mon.recent_lines()), 10, "Recent lines shows all")
eq(len(mon.all_lines), 10, "All lines returns 10")

# Test max_lines
mon.max_lines = 5
mon.feed_single("line 11", "")
eq(len(mon.all_lines), 5, "Max lines enforced at 5")

# Test recent diagnoses
mon.clear()
mon.feed_single("!!! ERROR !!! MISPLACED PARAMETER", "AutoXDS")
t("Recent diagnoses works", len(mon.recent_diagnoses()) >= 1)

# Test callback
callbacks_fired = []
mon.set_diagnosis_callback(lambda diag: callbacks_fired.append(diag))
mon.feed_single("!!! WARNING !!! test", "AutoXDS")
t("Diagnosis callback fired", len(callbacks_fired) >= 1)

# Test reset_cooldowns
mon.engine.reset_cooldowns()
mon.feed_single("!!! ERROR !!! INSUFFICIENT PERCENTAGE (< 50%)", "AutoXDS")
mon.feed_single("!!! ERROR !!! INSUFFICIENT PERCENTAGE (< 50%)", "AutoXDS")
t("After reset, duplicate fires again", mon.diagnosis_count >= 2)


# ============================================================
# 6. SKILL MAPPER TESTS
# ============================================================
section("6. SKILL MAPPER")

sm = SkillMapper(ROOT)
eq(len(sm.available_skills), 17, "17 skills registered")

# Test process_xds match
matches = sm.match("process experiment_003", {"dataset_name": "experiment_003"})
t("process matches something", len(matches) > 0)

# Test process with explicit dataset
matches2 = sm.match("run xds on experiment_005", {"dataset_name": "experiment_005"})
t("run xds matches", len(matches2) > 0)

# Test cluster
matches3 = sm.match("cluster experiment_1 experiment_2 experiment_3", {"dataset_name": "experiment_1"})
t("cluster matches", len(matches3) > 0)
if matches3:
    t("cluster skill matched", any(m[0].skill_id == "cluster_hca" for m in matches3))

# Test merge
matches4 = sm.match("merge datasets experiment_1 experiment_2", {"dataset_name": "experiment_1"})
t("merge matches", len(matches4) > 0)

# Test solve
matches5 = sm.match("solve experiment_003 with Au4 C20 H16 N2 sg 4",
                     {"dataset_name": "experiment_003", "sg": "4"})
t("solve matches", len(matches5) > 0)
if matches5:
    t("solve skill found", any(m[0].skill_id.startswith("solve") for m in matches5))

# Test Chinese input
matches6 = sm.match("处理 experiment_003", {"dataset_name": "experiment_003"})
t("Chinese 'process' matches", len(matches6) > 0)

matches7 = sm.match("合并 experiment_1", {"dataset_name": "experiment_1"})
t("Chinese 'merge' matches", len(matches7) > 0)

# Test command building
matches8 = sm.match("process experiment_003", {"dataset_name": "experiment_003"})
if matches8:
    skill, params = matches8[0]
    cmd = skill.build_command(**params)
    t("Command builds without error", len(cmd) > 0)

# Test SkillAction.build_command with partial params
sa = SkillAction(
    skill_id="test",
    description="test",
    command_template="echo {name} {value}",
    params=["name", "value"],
)
cmd = sa.build_command(name="hello", value="world")
eq(cmd, "echo hello world", "SkillAction builds correctly")
cmd2 = sa.build_command(name="hello")
eq(cmd2, "echo hello", "Partial params replaced with empty string")

# Test list_datasets skill
matches9 = sm.match("list datasets", {"root": str(ROOT / "Data")})
t("list datasets matches", len(matches9) > 0)

# Test context param filling
matches10 = sm.match("solve", {
    "dataset_name": "experiment_003",
    "composition": "Au4 C20 H16 N2",
    "sg": "14",
})
t("Context fills solve params", len(matches10) > 0)
if matches10:
    skill, params = matches10[0]
    t("dataset from context", params.get("dataset") == "experiment_003")

# Test irrelevant input
matches11 = sm.match("hello world", {})
t("Irrelevant input returns empty or low-confidence matches",
  len(matches11) == 0 or (matches11[0] and matches11[0][1] and len(matches11[0][1]) <= 1))


# ============================================================
# 7. LLM TESTS
# ============================================================
section("7. LLM")

# Test NullBackend
null = NullBackend()
eq(null.mode_name, "offline", "Null backend name")
resp = null.chat("You are helpful", "Hello")
t("Null returns text", len(resp) > 0)
t("Null mentions offline in Chinese", "离线" in resp)

# Test OllamaBackend init
ollama = OllamaBackend(host="http://localhost:11434", model="sorc/qwen3.5-instruct-uncensored:4b")
eq(ollama.mode_name, "local", "Ollama backend name")

# Test Ollama availability
avail = ollama.is_available()
t(f"Ollama available: {avail}", avail if avail else "SKIP")

if avail:
    resp = ollama.chat("You are AutoCrys AI assistant. Be brief.", "What is XDS? Reply in one sentence.")
    t("Ollama responds", len(resp) > 10)
    print(f"  Ollama response: {resp[:200]}")
else:
    t("Ollama not available - skip chat test", "SKIP")

# Test create_llm from config
llm = create_llm(test_config_path)
t("create_llm returns backend", llm is not None)
t("Default is NullBackend", isinstance(llm, NullBackend))

# Test OpenAI backend (no key, so not available)
openai_backend = OpenAIBackend(api_key="", model="gpt-4o")
eq(openai_backend.is_available(), False, "OpenAI not available without key")
resp2 = openai_backend.chat("hi", "hello")
t("OpenAI without key returns error", "key" in resp2.lower() or "error" in resp2.lower())

# Test Anthropic backend
anthropic_backend = AnthropicBackend(api_key="", model="claude-sonnet-4-20250514")
eq(anthropic_backend.is_available(), False, "Anthropic not available without key")

# Test config.json roundtrip with LLM modes
cfg.llm_mode = "local"
t("Config mode set to local", cfg.llm_mode == "local")
cfg._data.setdefault("llm", {})["local"] = {"host": "http://localhost:11434", "model": "llama3.2"}
cfg.save()
cfg2 = Config(test_config_path)
t("Reloaded config has local mode", cfg2.llm_mode == "local")
cfg.llm_mode = "offline"
cfg.save()


# ============================================================
# 8. OUTPUT PATTERN TESTS (Real data simulation)
# ============================================================
section("8. REAL OUTPUT SIMULATION")

engine.reset_cooldowns()

simulated_outputs = [
    ("AutoXDS", "!!! ERROR !!! AUTOMATIC DETERMINATION OF SPOT SIZE PARAMETERS HAS FAILED."),
    ("AutoXDS", "!!! ERROR !!! CANNOT OPEN OR READ FILE bin1_04.tmp"),
    ("AutoXDS", "!!! ERROR !!! REDUCE returns IRANK=2"),
    ("AutoXDS", "!!! WARNING !!! SOLUTION MAY NOT BE UNIQUE."),
    ("AutoXDS", "R-factor 0.245 COMPLETENESS 92.3%"),
    ("AutoXDS", "ISa 3.41 Rfactor 0.153 Completeness 87.5"),
    ("AutoXDS", "MISPLACED PARAMETER in XSCALE.INP"),
    ("AutoSolve", "shelxt_no_solution: no atom model found before HKLF"),
    ("AutoSolve", "input_preparation_failed: missing_composition"),
    ("AutoXDS", "SG mismatch detected between datasets"),
]

total_diags = 0
for tab, line in simulated_outputs:
    diags = engine.feed_line(line, tab)
    total_diags += len(diags)
    for d in diags:
        print(f"  [{d.severity_icon}] {tab}: {d.title}")

t("Simulated output produces diagnoses", total_diags > 0)
eq(total_diags, 11, "Expected ~11 diagnoses from 10 real patterns")


# ============================================================
# 9. INTEGRATION TEST: Monitor + Context + Rules
# ============================================================
section("9. INTEGRATION (Monitor + Context + Rules)")

mon2 = LogMonitor()
ctx3 = AIContext()

def pipeline(line, tab, dataset_name=None):
    ctx3.update_tab(tab)
    if dataset_name:
        ctx3.update_dataset(Path("/tmp") / dataset_name, dataset_name)
    ctx3.append_console(line)
    diags = mon2.feed_single(line, ctx3.active_tab)
    for d in diags:
        ctx3.add_diagnosis(d)
    return diags

mon2.clear()
ctx3 = AIContext()

# Simulate an XDS processing session
pipeline("Starting XDS processing for experiment_003", "AutoXDS", "experiment_003")
pipeline("COLSPOT: 12000 spots found", "AutoXDS")
pipeline("INTEGRATE: processing frames...", "AutoXDS")
r1 = pipeline("!!! ERROR !!! INSUFFICIENT PERCENTAGE (< 50%) of reflections", "AutoXDS")
r2 = pipeline("XDS recovery mode: reducing MINIMUM_NUMBER_OF_REFLECTIONS", "AutoXDS")
pipeline("CORRECT: processing complete, ISa 7.11", "AutoXDS")
pipeline("Rfactor 0.245 detected - high!", "AutoXDS")

t("Integration: console has lines", len(ctx3.console_buffer) > 0)
t("Integration: monitor has diagnoses", mon2.diagnosis_count > 0)
t("Integration: context has diagnosis count", ctx3.diagnosis_count > 0)
t("Integration: operation state tracked", ctx3.active_tab == "AutoXDS")
t("Integration: dataset tracked", ctx3.dataset_name == "experiment_003")

# Test build_prompt_context with full state
prompt = ctx3.build_prompt_context()
t("Integration: prompt includes all context", "AutoXDS" in prompt and "experiment_003" in prompt)


# ============================================================
# 10. EDGE CASES
# ============================================================
section("10. EDGE CASES")

# Empty input to monitor
mon3 = LogMonitor()
diags = mon3.feed_single("", "")
eq(len(diags), 0, "Empty line: no diagnoses")

# Very long line
long_line = "X" * 10000
diags = engine.feed_line(long_line, "")
t("Very long line handled without exception", True)

# Malformed regex in rule (should not crash)
try:
    r = Rule(rule_id="test", patterns=[r"([a-z"], severity="info", title="T", suggestion="S")
    t("Invalid regex Rule created", "SKIP")  # May or may not crash at creation
except re.error:
    t("Invalid regex caught at Rule creation", True)

# Context with None values
ctx4 = AIContext()
ctx4.update_dataset(None, None)
eq(ctx4.dataset_name, None, "None dataset handled")

# SkillMapper with no context
matches = sm.match("process", {})
t("Match with empty context doesn't crash", isinstance(matches, list))

# Knowledge base with missing module
section_val = kb.get_section("NonExistent", "Anything")
eq(section_val, "", "Missing module returns empty string")

# Monitor max_lines boundary
mon4 = LogMonitor()
mon4.max_lines = 1
mon4.feed_single("line1", "")
mon4.feed_single("line2", "")
eq(len(mon4.all_lines), 1, "max_lines=1 enforced")

# Diagnosis edge cases
d = Diagnosis(rule_id="x", severity="error", title="", detail="", suggestion="")
eq(d.is_error, True, "Empty diag is_error returns True")
eq(d.is_error, False if d.severity == "warning" else True, "is_error correct")

# Skill match score
score = sm._match_score(SkillAction("a", "d", "cmd", ["a", "b"]), {"a": 1, "b": 2})
eq(score, 1.0, "Full params score is 1.0")
score2 = sm._match_score(SkillAction("a", "d", "cmd", ["a", "b"]), {"a": 1})
eq(score2, 0.5, "Half params score is 0.5")
score3 = sm._match_score(SkillAction("a", "d", "cmd", []), {})
eq(score3, 0.5, "No params score is 0.5")


# ============================================================
# 11. LLM PROMPT AND RESPONSE TEST
# ============================================================
section("11. LLM PROMPT QUALITY")

prompt = kb.build_system_prompt()
# Verify prompt quality
t("System prompt has role definition", "AI assistant" in prompt.lower() or "crystallographic" in prompt.lower())
t("System prompt has knowledge sections", "AutoXDS" in prompt)
t("System prompt mentions scripts", "auto_xds" in prompt.lower())
t("System prompt keeps full skill guidance", "Do not claim final reflection integration is complete" in prompt)

# Context prompt test
ctx5 = AIContext()
ctx5.update_tab("AutoXDS")
ctx5.update_dataset(Path("/tmp/exp_1"), "exp_1")
ctx5.start_operation("XDS Processing")
ctx5.append_console("XDS started")
ctx5.append_console("COLSPOT found 5000 spots")
ctx5.append_console("WARNING: high background")
ctx5.finish_operation("XDS Processing", success=True)
ctx_prompt = ctx5.build_prompt_context()
t("Context prompt has tab", "AutoXDS" in ctx_prompt)
t("Context prompt has dataset", "exp_1" in ctx_prompt)
t("Context prompt has log", "high background" in ctx_prompt)


# ============================================================
# 12. SKILL COMMAND OUTPUT PARSING
# ============================================================
section("12. SKILL COMMAND TESTS")

# Test that build_command handles missing params gracefully
for skill in sm.available_skills:
    try:
        cmd = skill.build_command()
        t(f"Skill {skill.skill_id} builds without params", isinstance(cmd, str))
    except Exception as e:
        t(f"Skill {skill.skill_id} crashes without params", f"Error: {e}")

# Test skill run_command (dry - just returns error but doesn't crash)
rc, out, err = sm.run_command("echo hello world")
eq(rc, 0, "echo succeeds")
t("echo produces output", "hello world" in out)


# ============================================================
# ============================================================
section("SUMMARY")
print(f"\n  TOTAL: {PASS + FAIL + SKIP} tests")
print(f"  PASS:  {PASS}")
print(f"  FAIL:  {FAIL}")
print(f"  SKIP:  {SKIP}")
print()

if FAIL > 0:
    print("  SOME TESTS FAILED - review output above")
    sys.exit(1)
else:
    print("  ALL TESTS PASSED")
    sys.exit(0)
