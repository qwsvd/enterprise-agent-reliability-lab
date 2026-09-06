from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable

from app.benchmarks.models import (
    BenchmarkAvailability,
    BenchmarkError,
    BenchmarkIdentity,
    ExternalExecutionRequest,
    ExternalExecutionResult,
)


ProcessRunner = Callable[..., subprocess.CompletedProcess[str]]
ExecutableFinder = Callable[[str], str | None]


class ExternalProcessBenchmarkProvider:
    """Execute an explicitly built benchmark command without a shell.

    Result-file parsing remains the adapter's responsibility. Keeping execution
    and import separate prevents a zero exit code from being presented as a
    fabricated benchmark score.
    """

    def __init__(
        self,
        identity: BenchmarkIdentity,
        *,
        command: str = "tau2",
        supported_contract_versions: str,
        external_python_requirement: str | None = None,
        finder: ExecutableFinder = shutil.which,
        runner: ProcessRunner = subprocess.run,
    ) -> None:
        self.identity = identity
        self.command = command
        self.supported_contract_versions = supported_contract_versions
        self.external_python_requirement = external_python_requirement
        self.finder = finder
        self.runner = runner

    def inspect(self) -> BenchmarkAvailability:
        path = self.finder(self.command)
        return BenchmarkAvailability(
            benchmark=self.identity,
            provider="external_process",
            command=self.command,
            available=path is not None,
            executable_path=path,
            external_python_requirement=self.external_python_requirement,
            supported_contract_versions=self.supported_contract_versions,
        )

    def execute(self, request: ExternalExecutionRequest) -> ExternalExecutionResult:
        executable = self.finder(request.executable)
        if executable is None:
            return self._error("provider_unavailable", "External benchmark executable not found")
        try:
            completed = self.runner(
                [executable, *request.arguments],
                cwd=request.working_directory,
                capture_output=True,
                text=True,
                timeout=request.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return self._error("provider_timeout", "External benchmark execution timed out", True)
        except OSError:
            return self._error("provider_error", "External benchmark process could not start")
        if completed.returncode != 0:
            return ExternalExecutionResult(
                status="provider_error",
                return_code=completed.returncode,
                errors=[
                    BenchmarkError(
                        code="provider_nonzero_exit",
                        message="External benchmark process exited unsuccessfully",
                    )
                ],
            )
        return ExternalExecutionResult(status="completed", return_code=0)

    @staticmethod
    def _error(code: str, message: str, retryable: bool = False) -> ExternalExecutionResult:
        return ExternalExecutionResult(
            status="provider_error",
            errors=[BenchmarkError(code=code, message=message, retryable=retryable)],
        )
