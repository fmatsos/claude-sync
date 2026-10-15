"""claude-sync: sync Claude Code sessions between two machines over SSH."""

from claude_sync.config import Config, ConfigError, default_config_path
from claude_sync.remap import PathMapper, encode_path, remap_tree

__version__ = "0.5.0"

__all__ = [
    "Config",
    "ConfigError",
    "PathMapper",
    "__version__",
    "default_config_path",
    "encode_path",
    "remap_tree",
]
