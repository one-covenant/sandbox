"""Factory for creating sandbox executors based on configuration."""

from config import settings


def get_executor_class():
    """
    Returns the appropriate executor class based on SANDBOX_BACKEND setting.

    Returns:
        AgentExecutor or BasilicaAgentExecutor class
    """
    backend = settings.sandbox_backend.lower()

    if backend == "basilica":
        from validator.basilica_executor import BasilicaAgentExecutor
        return BasilicaAgentExecutor
    else:  # default to "docker"
        from validator.executor import AgentExecutor
        return AgentExecutor


def create_executor(job_run, agent_filepath, project_key, job_run_reports_dir, platform_client):
    """
    Factory function to create the appropriate executor instance.

    Args:
        job_run: The job run model instance
        agent_filepath: Path to the agent Python file
        project_key: The project identifier
        job_run_reports_dir: Directory for storing reports
        platform_client: Platform API client

    Returns:
        Executor instance (AgentExecutor or BasilicaAgentExecutor)
    """
    ExecutorClass = get_executor_class()
    return ExecutorClass(
        job_run=job_run,
        agent_filepath=agent_filepath,
        project_key=project_key,
        job_run_reports_dir=job_run_reports_dir,
        platform_client=platform_client,
    )
