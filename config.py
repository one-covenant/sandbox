from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    app_env: str | None = "development"
    local: bool = False
    wallet_name: str | None = None

    chutes_api_key: str | None = None

    app_url: str = "bitsec.ai"
    platform_url: str = "https://bitsec.ai/"

    host_cwd: str = "."
    validator_dir: str = "validator"

    proxy_container: str = "bitsec_proxy"
    proxy_network: str = "bitsec-net"
    proxy_port: int = 8087
    proxy_url: str = "http://localhost:8087"

    skip_execution: bool = False
    skip_evaluation: bool = False

    use_bt_logging: bool = False

    # Sandbox backend selection: "docker" (default) or "basilica"
    sandbox_backend: str = "docker"

    # Basilica SDK configuration (required when sandbox_backend="basilica")
    basilica_api_url: str | None = None
    basilica_api_token: str | None = None
    basilica_image_prefix: str = "ghcr.io/bitsec-ai"
    basilica_cpu: str | None = None      # e.g., "500m"
    basilica_memory: str | None = None   # e.g., "512Mi"
    basilica_timeout: int | None = None  # Timeout in seconds

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="allow"
    )

settings = Settings()
