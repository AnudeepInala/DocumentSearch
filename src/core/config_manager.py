"""
Enterprise Document Search System - Configuration Manager
Production-grade configuration loader and validator
"""

import os
import yaml
from typing import Any, Dict, Optional
from pathlib import Path
from dataclasses import dataclass, field

from core.constants import VERSION


@dataclass
class PathConfig:
    """Path configuration"""
    source_drive: str
    working_root: str
    queue_db: str
    temp_dir: str
    logs_dir: str
    checkpoints_dir: str
    metrics_dir: str
    backup_dir: str
    app_root: str


@dataclass
class TikaInstance:
    """Tika server instance configuration"""
    host: str
    port: int
    memory_mb: int
    temp_dir: str


@dataclass
class TikaConfig:
    """Tika configuration"""
    instances: list[TikaInstance]
    timeout_seconds: int
    max_retries: int
    retry_backoff_seconds: list[int]
    connection_pool_size: int


@dataclass
class WorkerPool:
    """Worker pool configuration"""
    num_workers: int
    queue_name: str
    tika_ports: list[int]
    target_time_seconds: int


@dataclass
class DiscoveryConfig:
    """Discovery configuration"""
    num_workers: int
    partition_strategy: str
    custom_partitions: list
    batch_size: int
    target_rate: int
    exclude_patterns: list[str]
    filter_by_extension: bool
    excluded_extensions: list[str]
    included_extensions: list[str]
    priority_folders: list
    size_categories: Dict[str, int]
    continuous_discovery: bool = False  # New: Enable continuous monitoring
    rescan_interval_seconds: int = 300  # New: Rescan interval in seconds (default 5 min)


@dataclass
class ExtractionConfig:
    """Extraction configuration"""
    total_workers: int
    pools: Dict[str, WorkerPool]
    tika: TikaConfig
    ocr_detection: Dict[str, Any]
    restart_after_files: int


@dataclass
class OpenSearchConfig:
    """OpenSearch configuration"""
    hosts: list[str]
    use_ssl: bool
    verify_certs: bool
    index_name: str
    initial_batch_size: int
    min_batch_size: int
    max_batch_size: int
    batch_adjustment_step: int
    target_batch_time_seconds: float
    fast_batch_threshold_seconds: float
    slow_batch_threshold_seconds: float
    timeout_seconds: int
    max_retries: int
    retry_backoff_seconds: list[int]
    connection_pool_size: int
    bulk_refresh_interval: str
    normal_refresh_interval: str
    flush_timeout_seconds: int = 10  # Force flush after timeout (default 10s)
    startup_timeout_seconds: int = 120  # Wait for OpenSearch startup
    
    # From environment
    username: Optional[str] = None
    password: Optional[str] = None


@dataclass
class IndexingConfig:
    """Indexing configuration"""
    num_workers: int
    opensearch: OpenSearchConfig
    mapping: Dict[str, Any]


@dataclass
class PaddleConfig:
    """PaddleOCR configuration"""
    lang: str = "en"
    det: bool = True
    rec: bool = True
    cls: bool = True
    timeout_seconds: int = 120
    det_limit_side_len: Optional[int] = None
    det_limit_type: Optional[str] = None
    max_side_limit: Optional[int] = None


@dataclass
class OCRConfig:
    """OCR configuration"""
    initial_workers: int
    post_indexing_workers: int
    paddle: PaddleConfig
    preprocessing: Dict[str, Any]
    quality: Dict[str, int]
    priorities: list[Dict[str, Any]]
    multipage: Dict[str, Any]
    update_batch_size: int
    poppler_path: Optional[str] = None
    smart_retries: Dict[str, Any] = None
    max_pages_per_pdf: int = 100
    min_confidence: int = 25


@dataclass
class OrchestratorConfig:
    """Orchestrator configuration"""
    heartbeat_interval_seconds: int
    heartbeat_timeout_seconds: int
    service_check_interval_seconds: int
    resource_check_interval_seconds: int
    cpu: Dict[str, Any]
    memory: Dict[str, int]
    disk: Dict[str, int]
    checkpoint: Dict[str, Any]
    shutdown: Dict[str, Any]
    circuit_breaker: Dict[str, Any]


@dataclass
class NLPConfig:
    """NLP Configuration"""
    enabled: bool
    model_path: str
    max_text_length: int


@dataclass
class TaggingConfig:
    """Tagging configuration"""
    workers: int
    batch_size: int
    max_retries: int
    review_threshold: float
    taxonomy_path: str
    tagger_version: str
    hot_reload_seconds: int
    metadata_excel_path: str = ""
    metadata_mode_enabled: bool = True
    strict_spacy_when_no_metadata: bool = True
    required_non_empty_export_columns: list[str] = field(default_factory=list)
    metadata_input_source: str = "config"
    metadata_upload_dir: str = ""


@dataclass
class RedisConfig:
    """Redis Configuration"""
    url: str
    max_connections: int
    timeout: int


@dataclass
class LoggingConfig:
    """Logging configuration"""
    default_level: str
    components: Dict[str, str]
    rotation: Dict[str, int]
    format: str
    date_format: str
    use_json: bool


@dataclass
class EmailConfig:
    """Email configuration"""
    enabled: bool
    smtp_host: str
    smtp_port: int
    use_tls: bool
    from_address: str
    to_addresses: list[str]
    
    # From environment
    username: Optional[str] = None
    password: Optional[str] = None


@dataclass
class AlertingConfig:
    """Alerting configuration"""
    email: EmailConfig
    critical_alerts: list[Dict[str, Any]]
    warning_alerts: list[Dict[str, Any]]
    progress_updates: Dict[str, int]
    daily_summary: Dict[str, Any]
    completion_notification: Dict[str, bool]


@dataclass
class APIConfig:
    """API configuration"""
    host: str
    port: int
    workers: int
    require_auth: bool
    cors_enabled: bool
    allowed_origins: list[str]
    search: Dict[str, Any]
    rate_limit: Dict[str, Any]
    
    # From environment
    api_token: Optional[str] = None


@dataclass
class SystemConfig:
    """Complete system configuration"""
    version: str
    redis: RedisConfig
    nlp: NLPConfig
    tagging: TaggingConfig
    paths: PathConfig
    discovery: DiscoveryConfig
    extraction: ExtractionConfig
    indexing: IndexingConfig
    ocr: OCRConfig
    orchestrator: OrchestratorConfig
    logging: LoggingConfig
    alerting: AlertingConfig
    api: APIConfig
    dashboard: Dict[str, Any]
    modes: Dict[str, Any]
    performance: Dict[str, Any]
    deduplication: Dict[str, Any]
    backup: Dict[str, Any]
    testing: Dict[str, Any]


class ConfigurationManager:
    """
    Production-grade configuration manager
    Loads, validates, and provides access to system configuration
    """
    
    def __init__(self, config_path: Optional[str] = None):
        """
        Initialize configuration manager
        
        Args:
            config_path: Path to configuration YAML file
        """
        if config_path is None:
            config_path = self._find_config_file()
        
        self.config_path = Path(config_path)
        self.raw_config: Dict[str, Any] = {}
        self.config: Optional[SystemConfig] = None
        
        self._load_config()
        self._load_environment_variables()
        self._validate_config()
        self._create_config_objects()
    
    def _find_config_file(self) -> str:
        """
        Find configuration file in standard locations.
        
        Search Order (Precedence):
        1. Explicit path passed to __init__
        2. ./config/config.yaml (Project local - highest auto-detect priority)
        3. ./config.yaml (Current dir)
        4. OS-specific global path (fallback only)
        """
        search_paths = [
            str(Path("config") / "config.yaml"),                # Project local (priority)
            "config.yaml",                                      # Current dir
            str(Path("C:/DocumentSearch/config/config.yaml")),  # Windows global (fallback)
            str(Path("/etc/docsearch/config.yaml")),            # Linux global (fallback)
        ]
        
        for path in search_paths:
            if Path(path).exists():
                return path
        
        raise FileNotFoundError(
            "Configuration file not found. Searched: " + ", ".join(search_paths)
        )
    
    def _interpolate_dict(self, data: Any, app_root: str) -> Any:
        """Recursively replace {app_root} placeholder in values."""
        if isinstance(data, dict):
            return {k: self._interpolate_dict(v, app_root) for k, v in data.items()}
        elif isinstance(data, list):
            return [self._interpolate_dict(item, app_root) for item in data]
        elif isinstance(data, str):
            return data.replace("{app_root}", app_root)
        return data

    def _load_config(self) -> None:
        """Load configuration from YAML file"""
        if not self.config_path.exists():
            raise FileNotFoundError(f"Configuration file not found: {self.config_path}")
        
        with open(self.config_path, 'r', encoding='utf-8') as f:
            self.raw_config = yaml.safe_load(f)
            
        # Dynamically interpolate {app_root} placeholder in all path settings
        app_root = self.raw_config.get('paths', {}).get('app_root', '')
        if app_root:
            self.raw_config = self._interpolate_dict(self.raw_config, app_root)
    
    def _load_environment_variables(self) -> None:
        """Load sensitive configuration from environment variables"""
        def set_if_present(target: Dict[str, Any], key: str, value: Optional[str]) -> None:
            if value is not None and str(value).strip() != "":
                # Attempt type conversion based on existing value
                if key in target:
                    if isinstance(target[key], bool):
                        target[key] = str(value).lower() in ('true', '1', 'yes', 'on')
                    elif isinstance(target[key], int):
                        try:
                            target[key] = int(value)
                        except ValueError:
                            pass # Keep as string or original
                    elif isinstance(target[key], float):
                         try:
                            target[key] = float(value)
                         except ValueError:
                            pass
                    else:
                        target[key] = value
                else:
                    target[key] = value

        # OpenSearch credentials
        opensearch_user = os.getenv('OPENSEARCH_USER')
        opensearch_password = os.getenv('OPENSEARCH_PASSWORD')
        
        if 'indexing' not in self.raw_config:
            self.raw_config['indexing'] = {}
        if 'opensearch' not in self.raw_config['indexing']:
            self.raw_config['indexing']['opensearch'] = {}

        set_if_present(self.raw_config['indexing']['opensearch'], 'username', opensearch_user)
        set_if_present(self.raw_config['indexing']['opensearch'], 'password', opensearch_password)
        
        # SMTP credentials
        smtp_user = os.getenv('SMTP_USER')
        smtp_password = os.getenv('SMTP_PASSWORD')
        
        if 'alerting' not in self.raw_config:
            self.raw_config['alerting'] = {}
        if 'email' not in self.raw_config['alerting']:
            self.raw_config['alerting']['email'] = {}

        set_if_present(self.raw_config['alerting']['email'], 'username', smtp_user)
        set_if_present(self.raw_config['alerting']['email'], 'password', smtp_password)
        
        # Email addresses from environment (overrides config file if present)
        alert_from_email = os.getenv('ALERT_FROM_EMAIL')
        alert_to_emails = os.getenv('ALERT_TO_EMAILS')
        
        if alert_from_email:
            self.raw_config['alerting']['email']['from_address'] = alert_from_email
        
        if alert_to_emails:
            # Parse comma-separated list
            self.raw_config['alerting']['email']['to_addresses'] = [
                email.strip() for email in alert_to_emails.split(',') if email.strip()
            ]
        
        # API token
        api_token = os.getenv('API_TOKEN')
        
        if 'api' not in self.raw_config:
            self.raw_config['api'] = {}

        set_if_present(self.raw_config['api'], 'api_token', api_token)
    
    def _validate_config(self) -> None:
        """Validate configuration completeness and correctness"""
        required_sections = [
            'paths', 'discovery', 'extraction', 'indexing', 'ocr',
            'orchestrator', 'logging', 'alerting', 'api'
        ]
        
        for section in required_sections:
            if section not in self.raw_config:
                raise ValueError(f"Required configuration section missing: {section}")
        
        # Validate paths exist or can be created
        paths = self.raw_config['paths']
        
        # Validate Tika instances configuration
        tika_instances = self.raw_config['extraction']['tika']['instances']
        if len(tika_instances) < 1:
            raise ValueError("At least one Tika instance must be configured")
        
        # Validate worker counts
        extraction_workers = self.raw_config['extraction']['total_workers']
        if extraction_workers < 1:
            raise ValueError("At least one extraction worker required")
        
        indexing_workers = self.raw_config['indexing']['num_workers']
        if indexing_workers < 1:
            raise ValueError("At least one indexing worker required")
    
    def _create_config_objects(self) -> None:
        """Create typed configuration objects from raw dict"""
        # Create RedisConfig (use defaults if not present)
        redis_data = self.raw_config.get('redis', {
            'url': 'redis://localhost:6379/0',
            'max_connections': 50,
            'timeout': 30
        })
        redis = RedisConfig(**redis_data)
        
        # Create NLPConfig (use defaults if not present)
        nlp_data = self.raw_config.get('nlp', {
            'enabled': False,
            'model_path': 'en_core_web_md',
            'max_text_length': 100000
        })
        nlp = NLPConfig(**nlp_data)

        # Create TaggingConfig (use defaults if not present)
        default_taxonomy = str(
            Path(self.raw_config['paths']['working_root']) / "taxonomy" / "master_taxonomy.xlsx"
        )
        tagging_data = self.raw_config.get('tagging', {
            'workers': 2,
            'batch_size': 8,
            'max_retries': 3,
            'review_threshold': 0.75,
            'taxonomy_path': default_taxonomy,
            'tagger_version': 'local-hybrid-v1',
            'hot_reload_seconds': 30,
            'metadata_excel_path': '',
            'metadata_mode_enabled': True,
            'strict_spacy_when_no_metadata': True,
            'required_non_empty_export_columns': [
                'smart_id', 'file_name', 'category', 'department', 'purpose',
                'key_names', 'amount_found', 'important_dates', 'location_mentioned',
                'confidentiality', 'current_status', 'processed_on', 'file_type', 'file_size'
            ],
            'metadata_input_source': 'config',
            'metadata_upload_dir': str(Path(self.raw_config['paths']['working_root']) / 'metadata' / 'uploads'),
        })
        tagging_data.setdefault('metadata_excel_path', '')
        tagging_data.setdefault('metadata_mode_enabled', True)
        tagging_data.setdefault('strict_spacy_when_no_metadata', True)
        tagging_data.setdefault('required_non_empty_export_columns', [
            'smart_id', 'file_name', 'category', 'department', 'purpose',
            'key_names', 'amount_found', 'important_dates', 'location_mentioned',
            'confidentiality', 'current_status', 'processed_on', 'file_type', 'file_size'
        ])
        tagging_data.setdefault('metadata_input_source', 'config')
        tagging_data.setdefault('metadata_upload_dir', str(Path(self.raw_config['paths']['working_root']) / 'metadata' / 'uploads'))
        tagging = TaggingConfig(**tagging_data)
        
        # Create PathConfig
        paths = PathConfig(**self.raw_config['paths'])
        
        # Create TikaConfig
        tika_instances = [
            TikaInstance(**inst) 
            for inst in self.raw_config['extraction']['tika']['instances']
        ]
        
        tika_config = TikaConfig(
            instances=tika_instances,
            timeout_seconds=self.raw_config['extraction']['tika']['timeout_seconds'],
            max_retries=self.raw_config['extraction']['tika']['max_retries'],
            retry_backoff_seconds=self.raw_config['extraction']['tika']['retry_backoff_seconds'],
            connection_pool_size=self.raw_config['extraction']['tika']['connection_pool_size']
        )
        
        # Create worker pools
        pools = {}
        for pool_name, pool_data in self.raw_config['extraction']['pools'].items():
            pools[pool_name] = WorkerPool(**pool_data)
        
        # Create ExtractionConfig
        extraction = ExtractionConfig(
            total_workers=self.raw_config['extraction']['total_workers'],
            pools=pools,
            tika=tika_config,
            ocr_detection=self.raw_config['extraction']['ocr_detection'],
            restart_after_files=self.raw_config['extraction']['restart_after_files']
        )
        
        # Create OpenSearchConfig
        opensearch = OpenSearchConfig(**self.raw_config['indexing']['opensearch'])
        
        # Create IndexingConfig
        indexing = IndexingConfig(
            num_workers=self.raw_config['indexing']['num_workers'],
            opensearch=opensearch,
            mapping=self.raw_config['indexing']['mapping']
        )
        
        # Create PaddleConfig
        paddle = PaddleConfig(**self.raw_config['ocr'].get('paddle', {}))
        
        # Create OCRConfig
        ocr = OCRConfig(
            initial_workers=self.raw_config['ocr']['initial_workers'],
            post_indexing_workers=self.raw_config['ocr']['post_indexing_workers'],
            paddle=paddle,
            preprocessing=self.raw_config['ocr']['preprocessing'],
            quality=self.raw_config['ocr']['quality'],
            priorities=self.raw_config['ocr']['priorities'],
            multipage=self.raw_config['ocr']['multipage'],
            update_batch_size=self.raw_config['ocr']['update_batch_size'],
            poppler_path=self.raw_config['ocr'].get('poppler_path'),
            smart_retries=self.raw_config['ocr'].get('smart_retries', {'enabled': False, 'min_confidence_threshold': 60}),
            max_pages_per_pdf=self.raw_config['ocr'].get('max_pages_per_pdf', 100),
            min_confidence=self.raw_config['ocr'].get('quality', {}).get('min_confidence', 25),
        )
        
        # Create OrchestratorConfig
        orchestrator = OrchestratorConfig(**self.raw_config['orchestrator'])
        
        # Create LoggingConfig
        logging_config = LoggingConfig(**self.raw_config['logging'])
        
        # Create EmailConfig
        email = EmailConfig(**self.raw_config['alerting']['email'])
        
        # Create AlertingConfig
        alerting = AlertingConfig(
            email=email,
            critical_alerts=self.raw_config['alerting']['critical_alerts'],
            warning_alerts=self.raw_config['alerting']['warning_alerts'],
            progress_updates=self.raw_config['alerting']['progress_updates'],
            daily_summary=self.raw_config['alerting']['daily_summary'],
            completion_notification=self.raw_config['alerting']['completion_notification']
        )
        
        # Create APIConfig
        api = APIConfig(**self.raw_config['api'])
        
        # Create DiscoveryConfig
        discovery = DiscoveryConfig(**self.raw_config['discovery'])
        
        # Create complete SystemConfig
        self.config = SystemConfig(
            version=VERSION,
            redis=redis,
            nlp=nlp,
            tagging=tagging,
            paths=paths,
            discovery=discovery,
            extraction=extraction,
            indexing=indexing,
            ocr=ocr,
            orchestrator=orchestrator,
            logging=logging_config,
            alerting=alerting,
            api=api,
            dashboard=self.raw_config['dashboard'],
            modes=self.raw_config['modes'],
            performance=self.raw_config['performance'],
            deduplication=self.raw_config['deduplication'],
            backup=self.raw_config['backup'],
            testing=self.raw_config['testing']
        )
    
    def get_config(self) -> SystemConfig:
        """Get complete system configuration"""
        if self.config is None:
            raise RuntimeError("Configuration not loaded")
        return self.config
    
    def get_section(self, section: str) -> Any:
        """Get specific configuration section"""
        if section not in self.raw_config:
            raise KeyError(f"Configuration section not found: {section}")
        return self.raw_config[section]
    
    def ensure_directories(self) -> None:
        """Ensure all required directories exist"""
        if self.config is None:
            raise RuntimeError("Configuration not loaded")
        
        dirs_to_create = [
            self.config.paths.working_root,
            self.config.paths.queue_db,
            self.config.paths.temp_dir,
            self.config.paths.logs_dir,
            self.config.paths.checkpoints_dir,
            self.config.paths.metrics_dir,
            self.config.paths.backup_dir,
            str(Path(self.config.tagging.taxonomy_path).parent),
            str(Path(self.config.paths.working_root) / "metadata"),
            str(Path(self.config.tagging.metadata_upload_dir)),
        ]
        
        # Add Tika temp directories
        for tika_inst in self.config.extraction.tika.instances:
            dirs_to_create.append(tika_inst.temp_dir)
        
        for dir_path in dirs_to_create:
            Path(dir_path).mkdir(parents=True, exist_ok=True)
    
    def print_config_summary(self) -> None:
        """Print configuration summary for validation"""
        if self.config is None:
            raise RuntimeError("Configuration not loaded")
        
        print(f"\n{'='*80}")
        print(f"Enterprise Document Search System v{self.config.version}")
        print(f"Configuration: {self.config_path}")
        print(f"{'='*80}\n")
        
        print(f"Source Drive: {self.config.paths.source_drive}")
        print(f"Working Root: {self.config.paths.working_root}")
        print("\nWorker Configuration:")
        print(f"  Discovery Workers: {self.config.discovery.num_workers}")
        print(f"  Extraction Workers: {self.config.extraction.total_workers}")
        print(f"  Indexing Workers: {self.config.indexing.num_workers}")
        print(f"  OCR Workers: {self.config.ocr.initial_workers} (initial)")
        
        print("\nTika Instances:")
        for inst in self.config.extraction.tika.instances:
            print(f"  - {inst.host}:{inst.port} ({inst.memory_mb}MB)")
        
        print("\nOpenSearch:")
        print(f"  Hosts: {', '.join(self.config.indexing.opensearch.hosts)}")
        print(f"  Index: {self.config.indexing.opensearch.index_name}")
        print(f"  Batch Size: {self.config.indexing.opensearch.initial_batch_size}")
        
        print(f"\nOperational Mode: {self.config.modes['default']}")
        print(f"\n{'='*80}\n")


# Singleton instance
_config_manager: Optional[ConfigurationManager] = None


def get_config_manager(config_path: Optional[str] = None) -> ConfigurationManager:
    """
    Get singleton configuration manager instance
    
    Args:
        config_path: Path to configuration file (only used on first call)
    
    Returns:
        ConfigurationManager instance
    """
    global _config_manager
    
    if _config_manager is None:
        _config_manager = ConfigurationManager(config_path)
    
    return _config_manager


def get_config() -> SystemConfig:
    """
    Get system configuration (convenience function)
    
    Returns:
        SystemConfig instance
    """
    return get_config_manager().get_config()
