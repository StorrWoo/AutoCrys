"""AI Assistant for AutoCrys - context-aware crystallographic co-pilot."""

from .panel import AIAssistantPanel
from .context import AIContext
from .monitor import LogMonitor
from .rules import RuleEngine, Diagnosis
from .skills import SkillMapper, SkillAction
from .llm import LLMBackend, create_llm
from .knowledge import KnowledgeBase
from .config import Config
from .artifacts import ArtifactAnalyzer, AnalysisReport, Evidence, Finding

__all__ = [
    "AIAssistantPanel",
    "AIContext",
    "LogMonitor",
    "RuleEngine",
    "Diagnosis",
    "SkillMapper",
    "SkillAction",
    "LLMBackend",
    "create_llm",
    "KnowledgeBase",
    "Config",
    "ArtifactAnalyzer",
    "AnalysisReport",
    "Evidence",
    "Finding",
]
