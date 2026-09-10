# Adapted from PipesHub (https://github.com/pipeshub-ai/pipeshub-ai), commit 578bcd9.
# Licensed under the Apache License, Version 2.0.
"""Tree-sitter source parsing primitives used by Neuron's chunk adapter."""

from connectors.core.chunking.code_parser.engine import (
    MAX_FILE_SIZE_BYTES,
    decode_source,
    parse_code,
)
from connectors.core.chunking.code_parser.lang_config import (
    LANGUAGES,
    SUPPORTED_CODE_EXTENSIONS,
    LanguageConfig,
    config_for_extension,
    config_for_language,
    detect_language,
)
from connectors.core.chunking.code_parser.models import ParsedFile, ParsedSymbol

__all__ = [
    "LANGUAGES",
    "MAX_FILE_SIZE_BYTES",
    "SUPPORTED_CODE_EXTENSIONS",
    "LanguageConfig",
    "ParsedFile",
    "ParsedSymbol",
    "config_for_extension",
    "config_for_language",
    "decode_source",
    "detect_language",
    "parse_code",
]
