from __future__ import annotations

import hashlib
import math
import re
from typing import Any

from app.benchmarks.aggregate import summarize_results
from app.benchmarks.base import BenchmarkIntegrationError
from app.benchmarks.models import (
    BenchmarkCase,
    BenchmarkError,
    BenchmarkExecutionResult,
    BenchmarkIdentity,
    BenchmarkOutcome,
    BenchmarkPolicyMetadata,
    BenchmarkReport,
    BenchmarkTool,
    ExpectedObservableOutcome,
    ExternalExecutionRequest,
)


TAU3_SOURCE_URL = "https://github.com/sierra-research/tau2-bench"
TAU3_IMPLEMENTATION = "sierra-research/tau2-bench"
TAU3_CONTRACT_SCHEMA = "after-sales-benchmark/tau3-v1"
SUPPORTED_TAU3_VERSIONS = ">=1.0.1,<1.1.0"
TAU3_EXTERNAL_PYTHON = ">=3.12,<3.14"
_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:[-+][A-Za-z0-9.-]+)?$")
_SAFE_CLI_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_ERROR_TERMINATIONS = {
    "agent_error",
    "user_error",
    "infrastructure_error",
    "unexpected_error",
}


class Tau3BenchmarkAdapter:
    """Map current τ³-bench task/result contracts without importing ``tau2``."""

    def __init__(self, version: str = "1.0.1") -> None:
        self.version = version
        self._validate_version(version)

    @property
    def identity(self) -> BenchmarkIdentity:
        return BenchmarkIdentity(
            name="tau3-bench",
            implementation=TAU3_IMPLEMENTATION,
            version=self.version,
            source_url=TAU3_SOURCE_URL,
            license="MIT",
        )

    def convert_task(self, document: dict[str, Any]) -> BenchmarkCase:
        version, domain, payload = self._unwrap(document, "task")
        self._require_adapter_version(version)
        return self.convert_official_task(
            payload,
            domain=domain,
            policy=document.get("policy"),
            tool_definitions=document.get("tool_definitions"),
            metadata=self._contract_metadata(document),
        )

    def convert_official_task(
        self,
        task: dict[str, Any],
        *,
        domain: str,
        policy: str | None = None,
        tool_definitions: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> BenchmarkCase:
        if not isinstance(task, dict):
            raise BenchmarkIntegrationError("malformed_task", "tau3-bench task must be an object")
        task_id = task.get("id")
        scenario = task.get("user_scenario")
        if not isinstance(task_id, str) or not task_id:
            raise BenchmarkIntegrationError("malformed_task", "tau3-bench task id must be a non-empty string")
        if not isinstance(scenario, dict):
            raise BenchmarkIntegrationError("malformed_task", "tau3-bench user_scenario must be an object")
        criteria = task.get("evaluation_criteria") or {}
        if not isinstance(criteria, dict):
            raise BenchmarkIntegrationError(
                "malformed_task", "tau3-bench evaluation_criteria must be an object or null"
            )
        reward_basis = self._list_of_strings(criteria.get("reward_basis", ["DB", "COMMUNICATE"]), "reward_basis")
        actions = self._list_of_objects(criteria.get("actions") or [], "actions")
        communicate = self._list_of_strings(criteria.get("communicate_info") or [], "communicate_info")
        env_assertions = self._list_of_objects(criteria.get("env_assertions") or [], "env_assertions")
        nl_assertions = self._list_of_strings(criteria.get("nl_assertions") or [], "nl_assertions")

        benchmark_input = {"user_scenario": scenario}
        for key in (
            "description",
            "ticket",
            "initial_state",
            "required_documents",
            "user_tools",
        ):
            if key in task and task[key] is not None:
                benchmark_input[key] = task[key]

        return BenchmarkCase(
            benchmark=self.identity,
            domain=self._require_text(domain, "domain"),
            case_id=task_id,
            policy=self._policy_metadata(domain, policy),
            tools=self._convert_tools(tool_definitions or {}),
            benchmark_input=benchmark_input,
            expected_outcome=ExpectedObservableOutcome(
                score_basis=reward_basis,
                reference_actions=actions,
                communicate_info=communicate,
                environment_assertions=env_assertions,
                natural_language_assertions=nl_assertions,
                metadata={
                    "reference_actions_are_required": "ACTION" in reward_basis,
                    "grading_semantics": "product_of_reward_basis_components",
                },
            ),
            metadata=metadata or {},
        )

    def import_results(self, document: dict[str, Any]) -> BenchmarkReport:
        version, domain, payload = self._unwrap(document, "results")
        self._require_adapter_version(version)
        return self.import_official_results(
            payload,
            domain=domain,
            metadata=self._contract_metadata(document),
        )

    def import_official_results(
        self,
        results: dict[str, Any],
        *,
        domain: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> BenchmarkReport:
        if not isinstance(results, dict):
            raise BenchmarkIntegrationError("malformed_results", "tau3-bench Results must be an object")
        info = results.get("info")
        tasks = results.get("tasks")
        simulations = results.get("simulations")
        if not isinstance(info, dict) or not isinstance(tasks, list) or not isinstance(simulations, list):
            raise BenchmarkIntegrationError(
                "malformed_results", "tau3-bench Results requires info, tasks, and simulations fields"
            )
        environment = info.get("environment_info")
        if not isinstance(environment, dict):
            raise BenchmarkIntegrationError(
                "malformed_results", "tau3-bench Results info.environment_info must be an object"
            )
        upstream_domain = self._require_text(environment.get("domain_name"), "domain_name")
        if domain is not None and domain != upstream_domain:
            raise BenchmarkIntegrationError(
                "domain_mismatch",
                f"Contract domain {domain!r} does not match Results domain {upstream_domain!r}",
            )
        normalized = [self._convert_simulation(item, upstream_domain) for item in simulations]
        report_metadata: dict[str, Any] = {
            "source_artifact": "tau2.Results",
            "task_count": len(tasks),
            "upstream_git_commit": info.get("git_commit"),
            "num_trials": info.get("num_trials"),
            "max_steps": info.get("max_steps"),
        }
        agent_info = info.get("agent_info")
        if isinstance(agent_info, dict):
            report_metadata["agent_implementation"] = agent_info.get("implementation")
            report_metadata["agent_model"] = agent_info.get("llm")
        report_metadata.update(metadata or {})
        return BenchmarkReport(
            benchmark=self.identity,
            results=normalized,
            aggregate=summarize_results(normalized),
            metadata={key: value for key, value in report_metadata.items() if value is not None},
        )

    def build_run_request(
        self,
        *,
        domain: str,
        agent: str,
        agent_llm: str,
        user_llm: str,
        save_to: str,
        task_ids: list[str] | None = None,
        timeout_seconds: float = 3600,
    ) -> ExternalExecutionRequest:
        values = {
            "domain": domain,
            "agent": agent,
            "agent_llm": agent_llm,
            "user_llm": user_llm,
            "save_to": save_to,
        }
        for name, value in values.items():
            if not isinstance(value, str) or not _SAFE_CLI_VALUE.fullmatch(value):
                raise BenchmarkIntegrationError("invalid_command_argument", f"Unsafe {name} value")
        arguments = [
            "run",
            "--domain",
            domain,
            "--agent",
            agent,
            "--agent-llm",
            agent_llm,
            "--user-llm",
            user_llm,
            "--save-to",
            save_to,
        ]
        safe_task_ids: list[str] = []
        for task_id in task_ids or []:
            if not _SAFE_CLI_VALUE.fullmatch(task_id):
                raise BenchmarkIntegrationError("invalid_command_argument", "Unsafe task id")
            safe_task_ids.append(task_id)
        if safe_task_ids:
            arguments.extend(["--task-ids", ",".join(safe_task_ids)])
        return ExternalExecutionRequest(
            benchmark=self.identity,
            executable="tau2",
            arguments=arguments,
            timeout_seconds=timeout_seconds,
        )

    def _convert_simulation(self, raw: Any, domain: str) -> BenchmarkExecutionResult:
        if not isinstance(raw, dict):
            raise BenchmarkIntegrationError("malformed_results", "Each simulation must be an object")
        execution_id = self._require_text(raw.get("id"), "simulation id")
        task_id = self._require_text(raw.get("task_id"), "simulation task_id")
        termination = self._require_text(raw.get("termination_reason"), "termination_reason")
        reward_info = raw.get("reward_info")
        score: float | None = None
        if reward_info is not None:
            if not isinstance(reward_info, dict) or not isinstance(reward_info.get("reward"), (int, float)):
                raise BenchmarkIntegrationError(
                    "malformed_results", "reward_info.reward must be numeric when reward_info is present"
                )
            score = float(reward_info["reward"])
            if not 0 <= score <= 1:
                raise BenchmarkIntegrationError("invalid_score", "tau3-bench reward must be between 0 and 1")

        errors: list[BenchmarkError] = []
        if score is None and termination in _ERROR_TERMINATIONS:
            outcome = BenchmarkOutcome.ERROR
            passed = None
            errors.append(
                BenchmarkError(
                    code=f"tau3_{termination}",
                    message="External tau3-bench simulation terminated with an error",
                )
            )
        elif score is None:
            outcome = BenchmarkOutcome.UNSCORED
            passed = None
        else:
            passed = math.isclose(score, 1.0, abs_tol=1e-6)
            outcome = BenchmarkOutcome.PASSED if passed else BenchmarkOutcome.FAILED

        run_metadata = {
            "trial": raw.get("trial"),
            "duration_seconds": raw.get("duration"),
            "mode": raw.get("mode"),
            "reward_basis": reward_info.get("reward_basis") if isinstance(reward_info, dict) else None,
            "reward_breakdown": reward_info.get("reward_breakdown") if isinstance(reward_info, dict) else None,
        }
        return BenchmarkExecutionResult(
            benchmark=self.identity,
            domain=domain,
            case_id=task_id,
            execution_id=execution_id,
            score=score,
            passed=passed,
            outcome=outcome,
            termination_reason=termination,
            metadata={key: value for key, value in run_metadata.items() if value is not None},
            errors=errors,
        )

    def _convert_tools(self, definitions: dict[str, Any]) -> list[BenchmarkTool]:
        if not isinstance(definitions, dict):
            raise BenchmarkIntegrationError("malformed_task", "tool_definitions must be an object")
        tools: list[BenchmarkTool] = []
        for key in sorted(definitions):
            value = definitions[key]
            if not isinstance(value, dict):
                raise BenchmarkIntegrationError("malformed_task", "Each tool signature must be an object")
            name = value.get("name", key)
            if not isinstance(name, str) or not name:
                raise BenchmarkIntegrationError("malformed_task", "Tool name must be non-empty")
            params = value.get("params") or {}
            returns = value.get("returns")
            if not isinstance(params, dict) or (returns is not None and not isinstance(returns, dict)):
                raise BenchmarkIntegrationError("malformed_task", "Tool schemas must be objects")
            tools.append(
                BenchmarkTool(
                    name=name,
                    description=value.get("doc") if isinstance(value.get("doc"), str) else "",
                    input_schema=params,
                    output_schema=returns,
                )
            )
        return tools

    @staticmethod
    def _policy_metadata(domain: str, policy: str | None) -> BenchmarkPolicyMetadata:
        digest = hashlib.sha256(policy.encode("utf-8")).hexdigest() if policy is not None else None
        return BenchmarkPolicyMetadata(
            identifier=f"{domain}-policy",
            sha256=digest,
            metadata={"content_imported": False},
        )

    @staticmethod
    def _unwrap(document: dict[str, Any], artifact: str) -> tuple[str, str, dict[str, Any]]:
        if not isinstance(document, dict):
            raise BenchmarkIntegrationError("malformed_contract", "Benchmark contract must be an object")
        if document.get("contract_schema") != TAU3_CONTRACT_SCHEMA:
            raise BenchmarkIntegrationError(
                "unsupported_contract", f"Expected contract_schema {TAU3_CONTRACT_SCHEMA!r}"
            )
        version = document.get("benchmark_version")
        domain = document.get("domain")
        payload = document.get(artifact)
        if not isinstance(version, str) or not isinstance(domain, str) or not isinstance(payload, dict):
            raise BenchmarkIntegrationError(
                "malformed_contract", f"Contract requires benchmark_version, domain, and {artifact}"
            )
        return version, domain, payload

    @staticmethod
    def _contract_metadata(document: dict[str, Any]) -> dict[str, Any]:
        metadata = document.get("metadata") or {}
        if not isinstance(metadata, dict):
            raise BenchmarkIntegrationError("malformed_contract", "Contract metadata must be an object")
        return metadata

    def _require_adapter_version(self, version: str) -> None:
        self._validate_version(version)
        if version != self.version:
            raise BenchmarkIntegrationError(
                "version_mismatch",
                f"Adapter version {self.version} cannot import a {version} contract",
            )

    @staticmethod
    def _validate_version(version: str) -> None:
        match = _VERSION.fullmatch(version)
        if not match:
            raise BenchmarkIntegrationError("unsupported_version", f"Invalid tau3-bench version: {version}")
        major, minor, patch = (int(part) for part in match.groups())
        if major != 1 or minor != 0 or patch < 1:
            raise BenchmarkIntegrationError(
                "unsupported_version",
                f"Unsupported tau3-bench version {version}; supported range is {SUPPORTED_TAU3_VERSIONS}",
            )

    @staticmethod
    def _require_text(value: Any, field: str) -> str:
        if not isinstance(value, str) or not value:
            raise BenchmarkIntegrationError("malformed_results", f"{field} must be a non-empty string")
        return value

    @staticmethod
    def _list_of_strings(value: Any, field: str) -> list[str]:
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise BenchmarkIntegrationError("malformed_task", f"{field} must be a list of strings")
        return value

    @staticmethod
    def _list_of_objects(value: Any, field: str) -> list[dict[str, Any]]:
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            raise BenchmarkIntegrationError("malformed_task", f"{field} must be a list of objects")
        return value
