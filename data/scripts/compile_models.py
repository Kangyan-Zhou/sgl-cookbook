#!/usr/bin/env python3
"""
Model Configuration Compiler

Compiles simplified model configuration YAML files into the full schema format.

Usage:
    python compile_models.py [--input-dir DIR] [--output-dir DIR] [--check]
    python compile_models.py --version 0.5.8  # Generate for specific version

The compiler reads simplified YAML files from the input directory and generates
full schema-compliant YAML files in the output directory.

Supports two patterns:
1. Variant Generation: Define base_name + capabilities + quantizations
2. Explicit Models: Define name directly (no variant expansion)

Version Range Support:
    Configurations can specify version ranges using the 'versions' field:

    configurations:
      - name: default
        versions:
          min: "0.5.6"   # Inclusive minimum version
          max: null      # Exclusive maximum (null = no limit)
        tp: 8
      - name: speculative-mtp
        versions:
          min: "0.5.7"   # Only available from v0.5.7
          max: "0.6.0"   # Removed in v0.6.0
        tp: 8

    When --version is specified, only configurations within range are included.
"""

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import yaml


# =============================================================================
# Version Handling
# =============================================================================


def parse_version(version_str: str) -> tuple[int, ...]:
    """
    Parse a version string into a tuple of integers for comparison.

    Supports formats like: "0.5.6", "v0.5.6", "0.5.6-beta1"
    Returns tuple like: (0, 5, 6)

    Raises:
        ValueError: If version_str is not a string or contains no numeric version parts
    """
    # Validate input type - common mistake is YAML like `min: 0.5.6` without quotes
    # which parses as float 0.5 instead of string "0.5.6"
    if not isinstance(version_str, str):
        raise ValueError(
            f"Version must be a string, got {type(version_str).__name__}: {version_str!r}. "
            f"Hint: In YAML, version numbers must be quoted: \"0.5.6\""
        )
    # Remove 'v' prefix if present
    version_str = version_str.lstrip("v")
    # Extract numeric parts (ignore pre-release suffixes like -beta1)
    match = re.match(r"^(\d+(?:\.\d+)*)", version_str)
    if not match:
        raise ValueError(f"Invalid version format: '{version_str}'. Expected format: '0.5.6' or 'v0.5.6'")
    return tuple(int(x) for x in match.group(1).split("."))


def version_in_range(
    version: tuple[int, ...],
    min_version: str | None,
    max_version: str | None,
) -> bool:
    """
    Check if a version is within the specified range.

    Args:
        version: Parsed version tuple (e.g., (0, 5, 8))
        min_version: Minimum version string (inclusive), or None for no minimum
        max_version: Maximum version string (exclusive), or None for no maximum

    Returns:
        True if version is within range [min_version, max_version)
    """
    if min_version is not None:
        min_parsed = parse_version(min_version)
        if version < min_parsed:
            return False

    if max_version is not None:
        max_parsed = parse_version(max_version)
        if version >= max_parsed:
            return False

    return True


def item_in_version_range(
    item: dict,
    target_version: tuple[int, ...] | None,
    item_type: str = "item",
    inherited_range: dict | None = None,
) -> bool:
    """
    Check if an item with optional 'versions' field is within target version range.

    This is a unified helper used for filtering configurations, hardware, models,
    and families by version. Supports inheritance: if item has no 'versions' field
    or 'versions' is explicitly null, falls back to inherited_range. If both are
    None/absent, the item is included (returns True).

    Args:
        item: Dict that may contain a 'versions' field with 'min'/'max' keys
        target_version: Target version tuple, or None to include all
        item_type: Description of item type for error messages (e.g., "configuration", "hardware")
        inherited_range: Version range inherited from parent (file/family), used if item has no 'versions'

    Returns:
        True if item should be included for the target version

    Raises:
        ValueError: If the 'versions' field has invalid structure or unexpected keys
    """
    if target_version is None:
        return True

    # Get versions from item, or inherit from parent
    versions = item.get("versions")
    if versions is None:
        versions = inherited_range
    if versions is None:
        return True

    item_name = item.get("name", "<unnamed>")

    # Validate versions field structure
    if not isinstance(versions, dict):
        raise ValueError(
            f"Invalid 'versions' field in {item_type} '{item_name}': "
            f"expected dict with 'min'/'max' keys, got {type(versions).__name__}. "
            f"Example: versions: {{ min: \"0.5.6\", max: \"0.6.0\" }}"
        )

    # Check for typos/unexpected keys
    valid_keys = {"min", "max"}
    unexpected_keys = set(versions.keys()) - valid_keys
    if unexpected_keys:
        raise ValueError(
            f"Unexpected keys in 'versions' field for {item_type} '{item_name}': {unexpected_keys}. "
            f"Valid keys are: {valid_keys}"
        )

    min_ver = versions.get("min")
    max_ver = versions.get("max")

    # Add context to any version parsing errors
    try:
        return version_in_range(target_version, min_ver, max_ver)
    except ValueError as e:
        raise ValueError(f"{e} (in {item_type} '{item_name}')") from e


def filter_by_version(
    items: list[dict],
    target_version: tuple[int, ...] | None,
    item_type: str = "configuration",
    verbose: bool = False,
    inherited_range: dict | None = None,
) -> list[dict]:
    """
    Filter a list of items by version range.

    Items without a 'versions' field inherit from inherited_range if provided.
    Items with 'versions' field are filtered based on their own min/max.

    Args:
        items: List of configuration dicts
        target_version: Target version tuple, or None to include all
        item_type: Description of item type for error/log messages
        verbose: If True, log filtered items to stderr
        inherited_range: Version range inherited from parent (file/family)

    Returns:
        Filtered list of items within version range

    Raises:
        ValueError: If an item has an invalid 'versions' field structure
    """
    if target_version is None:
        return items

    result = []
    for item in items:
        if item_in_version_range(item, target_version, item_type, inherited_range):
            result.append(item)
        elif verbose:
            item_name = item.get("name", "<unnamed>")
            # Use item's versions or inherited range for logging
            versions = item.get("versions") or inherited_range or {}
            min_ver = versions.get("min", "any")
            max_ver = versions.get("max", "any")
            version_str = ".".join(str(v) for v in target_version)
            print(
                f"  Filtered {item_type} '{item_name}': "
                f"version {version_str} not in range [{min_ver}, {max_ver})",
                file=sys.stderr,
            )
    return result


# =============================================================================
# Variant Generation Constants
# =============================================================================

# Only "base" has a default suffix (empty). Other capabilities like "instruct"
# and "thinking" must be explicitly defined via model_name_suffix in src files.
MODEL_NAME_SUFFIXES = {
    "base": "",
}

DEFAULT_QUANT_SUFFIXES = {
    "bf16": "",
    "fp8": "-FP8",
    "fp4": "-FP4",
    "int4": "-INT4",
}


# =============================================================================
# Engine Configuration Builders
# =============================================================================


def merge_extra_args(hw_args: list, config_args: list) -> list:
    """
    Merge extra_args from hardware config and config template.

    Hardware args come first, then config args are appended. Arguments starting
    with '--' are treated as keys. If a key exists in hw_args, the corresponding
    key-value pair from config_args is skipped to avoid duplicates.

    Args:
        hw_args: Hardware-specific extra arguments (higher priority)
        config_args: Config template extra arguments

    Returns:
        Merged list of extra arguments with hw_args taking precedence
    """
    if not config_args:
        return list(hw_args)
    if not hw_args:
        return list(config_args)

    # Extract keys (args starting with --) from hw_args
    hw_keys = {arg for arg in hw_args if isinstance(arg, str) and arg.startswith("--")}

    # Start with all hw_args
    result = list(hw_args)

    # Add config_args, skipping key-value pairs where key is already in hw_args
    i = 0
    while i < len(config_args):
        arg = config_args[i]
        if isinstance(arg, str) and arg.startswith("--"):
            if arg not in hw_keys:
                # Add the key
                result.append(arg)
                # Add the value if present (next arg that doesn't start with --)
                if i + 1 < len(config_args):
                    next_arg = config_args[i + 1]
                    if not (isinstance(next_arg, str) and next_arg.startswith("--")):
                        result.append(next_arg)
                        i += 1
            else:
                # Skip this key and its value
                if i + 1 < len(config_args):
                    next_arg = config_args[i + 1]
                    if not (isinstance(next_arg, str) and next_arg.startswith("--")):
                        i += 1  # Skip the value too
        i += 1

    return result


def build_engine_config(
    hw_config: dict,
    config_template: dict,
    quant: str | None = None,
    quant_overrides: dict | None = None,
) -> dict:
    """
    Build a full engine configuration block.

    Args:
        hw_config: Hardware-specific configuration (tp, extra_args, etc.)
        config_template: Named configuration template (default, tp2, speculative-mtp, etc.)
        quant: Quantization type (bf16, fp8, etc.)
        quant_overrides: Per-quantization overrides (e.g., fp8: { ep: 2 })

    Returns:
        Engine configuration dict with tp, dp, ep, extra_args, etc.
    """
    # Get tp: config template overrides hardware config when explicitly specified
    # This allows named configs like "tp2", "tp4", "tp8" to set specific tp values
    tp = config_template.get("tp", hw_config.get("tp", 8))

    # Merge extra_args: hardware args take precedence for duplicate keys
    hw_extra_args = hw_config.get("extra_args", [])
    config_extra_args = config_template.get("extra_args", [])
    merged_extra_args = merge_extra_args(hw_extra_args, config_extra_args)

    # Build base engine config from config template, overridden by hardware config
    engine = {
        "env_vars": hw_config.get("env_vars", config_template.get("env_vars", {})),
        "tp": tp,
        "dp": hw_config.get("dp", config_template.get("dp")),
        "ep": hw_config.get("ep", config_template.get("ep")),
        "enable_dp_attention": hw_config.get(
            "enable_dp_attention", config_template.get("enable_dp_attention")
        ),
        "extra_args": merged_extra_args,
    }

    # Apply quantization-specific overrides (e.g., fp8: { ep: 2 })
    if quant and quant_overrides:
        for key, value in quant_overrides.items():
            engine[key] = value

    return engine


def build_named_configuration(
    config_template: dict,
    hw_config: dict,
    quant: str,
    quant_overrides: dict | None = None,
    speculative_draft_model: str | None = None,
    effective_version_range: dict | None = None,
) -> dict:
    """
    Build a full named configuration block.

    Args:
        config_template: Named configuration template (default, tp2, speculative-eagle3, etc.)
        hw_config: Hardware-specific configuration
        quant: Quantization type (bf16, fp8, etc.)
        quant_overrides: Per-quantization overrides
        speculative_draft_model: Path to speculative draft model. When provided and
            the config name contains "speculative", adds --speculative-draft-model-path
            to extra_args.
        effective_version_range: The effective version range for this configuration
            (from config's own 'versions' or inherited from parent)

    Returns:
        Full configuration block with attributes, engine config, etc.
    """
    engine_config = build_engine_config(
        hw_config, config_template, quant, quant_overrides
    )

    # Add speculative draft model to extra_args for speculative configurations
    config_name = config_template.get("name", "")
    if speculative_draft_model and "speculative" in config_name.lower():
        engine_config["extra_args"].extend([
            "--speculative-draft-model-path",
            speculative_draft_model,
        ])

    result = {
        "name": config_template["name"],
        "attributes": {
            "nodes": config_template.get("nodes", "single"),
            "optimization": config_template.get("optimization", "balanced"),
            "quantization": quant,
        },
        "quantized_model_path": None,
        "engine": engine_config,
        "prefill": None,
        "decode": None,
    }

    # Add version_range if available, with quoted strings to prevent YAML float parsing
    if effective_version_range:
        result["version_range"] = {
            "min": QuotedString(effective_version_range["min"]) if effective_version_range.get("min") else None,
            "max": QuotedString(effective_version_range["max"]) if effective_version_range.get("max") else None,
        }

    return result


def build_hardware_config(
    hw_name: str,
    hw_config: dict,
    defaults: dict,
    quant: str,
    quant_overrides: dict | None = None,
    speculative_draft_model: str | None = None,
    target_version: tuple[int, ...] | None = None,
    verbose: bool = False,
    inherited_range: dict | None = None,
) -> dict:
    """
    Build hardware configuration with all named configurations.

    Args:
        hw_name: Hardware name (H100, H200, B200, etc.)
        hw_config: Hardware-specific configuration
        defaults: File-level defaults including configurations list
        quant: Quantization type
        quant_overrides: Per-quantization overrides
        speculative_draft_model: Path to speculative draft model
        target_version: Target version tuple for filtering, or None to include all
        verbose: If True, log filtered items
        inherited_range: Version range inherited from family

    Returns:
        Hardware configuration dict with list of named configurations
    """
    # Filter configurations by version range
    all_configs = defaults.get("configurations", [])
    filtered_configs = filter_by_version(
        all_configs, target_version, item_type="configuration", verbose=verbose,
        inherited_range=inherited_range
    )

    # Build configuration list with version ranges
    configurations = []
    for config_template in filtered_configs:
        # Determine the effective version range for this configuration
        # Config's own 'versions' takes precedence over inherited range
        effective_version_range = config_template.get("versions") or inherited_range
        configurations.append(
            build_named_configuration(
                config_template, hw_config, quant, quant_overrides, speculative_draft_model,
                effective_version_range=effective_version_range
            )
        )
    return {"configurations": configurations}


# =============================================================================
# Model Builders
# =============================================================================


def get_llm_attr(obj: dict, key: str, default: Any = None) -> Any:
    """Get an LLM attribute from either obj.llm.key or obj.key (for backwards compatibility)."""
    llm = obj.get("llm", {})
    if key in llm:
        return llm[key]
    return obj.get(key, default)


def get_merged_hardware_config(
    family: dict,
    model_def: dict,
    defaults: dict,
    target_version: tuple[int, ...] | None = None,
    verbose: bool = False,
    inherited_range: dict | None = None,
) -> tuple[dict, list[str]]:
    """
    Merge hardware configs from file-level defaults, family, and model levels.

    Args:
        family: Family-level configuration
        model_def: Model definition dict
        defaults: File-level defaults
        target_version: Target version tuple for filtering, or None to include all
        verbose: If True, log filtered items
        inherited_range: Version range inherited from family

    Returns (merged_hw_configs, hardware_list) where:
    - merged_hw_configs: dict with 'default' and hardware-specific overrides
    - hardware_list: list of hardware names to generate

    Inheritance order (most specific wins):
    1. model.hardware.{H100,H200,B200} - specific hardware override
    2. model.hardware.default - model default for all hardware
    3. family.hardware.{H100,H200,B200} - family hardware override
    4. family.hardware.default - family default for all hardware
    5. defaults.hardware.{H100,H200,B200} - file-level per-hardware default
    """
    # Get hardware configs from each level
    # defaults.hardware can be either:
    # - dict: { H100: { tp: 8 }, H200: { tp: 8 } } (new format with per-hw defaults)
    # - list: [H100, H200, B200] (old format, just hardware names)
    defaults_hw = defaults.get("hardware", {})
    family_hw = family.get("hardware", {})
    model_hw = model_def.get("hardware", {}) if isinstance(model_def, dict) else {}

    # Handle both old (list) and new (dict) formats for defaults.hardware
    if isinstance(defaults_hw, list):
        # Old format: list of hardware names, no per-hw defaults
        file_level_hw_configs = {}
        default_hardware_list = defaults_hw
    else:
        # New format: dict with per-hardware configs
        file_level_hw_configs = defaults_hw
        default_hardware_list = list(defaults_hw.keys())

    # Get default configs from family and model (applies to all hardware)
    family_default = family_hw.get("default", {})
    model_default = model_hw.get("default", {})

    # Build merged hardware config with file-level defaults as base
    merged_hw_configs = {"default": {**family_default, **model_default}}

    # Collect all hardware-specific keys from all levels
    all_hw_keys = set(default_hardware_list)
    for key in family_hw:
        if key != "default":
            all_hw_keys.add(key)
    for key in model_hw:
        if key != "default":
            all_hw_keys.add(key)

    # Merge hardware-specific configs with inheritance chain
    # Order: file-level hw -> family default -> family hw -> model default -> model hw
    for hw_name in all_hw_keys:
        # Start with file-level per-hardware default
        file_hw_config = file_level_hw_configs.get(hw_name, {})
        # Then family-level per-hardware override
        family_hw_specific = family_hw.get(hw_name, {})
        # Then model-level per-hardware override
        model_hw_specific = model_hw.get(hw_name, {})
        # Merge: file -> family_default -> family_hw -> model_default -> model_hw
        merged_hw_configs[hw_name] = {
            **file_hw_config,
            **family_default,
            **family_hw_specific,
            **model_default,
            **model_hw_specific,
        }

    # Determine hardware list:
    # - If model has explicit hardware keys (not just default), use those
    # - Else if family has explicit hardware keys (not just default), use those
    # - Else use all hardware from defaults.hardware
    model_explicit_hw = [k for k in model_hw if k != "default"]
    family_explicit_hw = [k for k in family_hw if k != "default"]

    if model_explicit_hw:
        # Model explicitly lists hardware (e.g., only H200/B200)
        hardware_list = model_explicit_hw
    elif family_explicit_hw and not model_default and not family_default:
        # Family explicitly lists hardware without defaults
        hardware_list = family_explicit_hw
    else:
        # Use all hardware from file-level defaults
        hardware_list = default_hardware_list

    # Filter hardware by version range using unified helper
    if target_version is not None:
        filtered_hardware_list = []
        for hw_name in hardware_list:
            hw_config = merged_hw_configs.get(hw_name, {})
            # Create a temporary dict with 'name' for error messages
            hw_item = {"name": hw_name, **hw_config}
            if item_in_version_range(hw_item, target_version, item_type="hardware", inherited_range=inherited_range):
                filtered_hardware_list.append(hw_name)
            elif verbose:
                versions = hw_config.get("versions") or inherited_range or {}
                min_ver = versions.get("min", "any")
                max_ver = versions.get("max", "any")
                version_str = ".".join(str(v) for v in target_version)
                print(
                    f"  Filtered hardware '{hw_name}': "
                    f"version {version_str} not in range [{min_ver}, {max_ver})",
                    file=sys.stderr,
                )
        hardware_list = filtered_hardware_list

    return merged_hw_configs, hardware_list


def get_diffusion_attr(obj: dict, key: str, default: Any = None) -> Any:
    """Get a diffusion attribute from either obj.diffusion.key or obj.key."""
    diffusion = obj.get("diffusion", {})
    if key in diffusion:
        return diffusion[key]
    return obj.get(key, default)


def build_diffusion_attributes(
    family: dict,
    model_def: dict,
) -> dict:
    """Build diffusion model attributes from family defaults and model overrides."""
    model_type = get_diffusion_attr(model_def, "model_type")
    if model_type is None:
        model_type = get_diffusion_attr(family, "model_type", "image")

    task_types = get_diffusion_attr(model_def, "task_types")
    if task_types is None:
        task_types = get_diffusion_attr(family, "task_types")

    supports_lora = get_diffusion_attr(model_def, "supports_lora")
    if supports_lora is None:
        supports_lora = get_diffusion_attr(family, "supports_lora")

    ulysses_degree = get_diffusion_attr(model_def, "ulysses_degree")
    if ulysses_degree is None:
        ulysses_degree = get_diffusion_attr(family, "ulysses_degree")

    ring_degree = get_diffusion_attr(model_def, "ring_degree")
    if ring_degree is None:
        ring_degree = get_diffusion_attr(family, "ring_degree")

    dit_layerwise_offload = get_diffusion_attr(model_def, "dit_layerwise_offload")
    if dit_layerwise_offload is None:
        dit_layerwise_offload = get_diffusion_attr(family, "dit_layerwise_offload")

    return {
        "model_type": model_type,
        "task_types": task_types,
        "supports_lora": supports_lora,
        "ulysses_degree": ulysses_degree,
        "ring_degree": ring_degree,
        "dit_layerwise_offload": dit_layerwise_offload,
    }


def build_model_attributes(
    family: dict,
    model_def: dict,
    capability: str | None = None,
) -> dict:
    """Build model attributes from family defaults and model overrides."""
    # Check if this is a diffusion model (has diffusion attributes)
    is_diffusion = (
        "diffusion" in family
        or "diffusion" in model_def
        or get_diffusion_attr(family, "model_type") is not None
        or get_diffusion_attr(model_def, "model_type") is not None
    )

    if is_diffusion:
        return {"diffusion": build_diffusion_attributes(family, model_def)}

    # LLM model: Determine thinking_capability based on:
    # 1. Model-level override (model_def.llm.thinking_capability or model_def.thinking_capability)
    # 2. Family-level default (family.llm.thinking_capability or family.thinking_capability)
    # 3. Infer from capability variant
    thinking_cap = get_llm_attr(model_def, "thinking_capability")
    if thinking_cap is None:
        thinking_cap = get_llm_attr(family, "thinking_capability")

    # If not explicitly set, infer from capability
    if thinking_cap is None and capability:
        if capability == "thinking":
            thinking_cap = "thinking"
        else:
            thinking_cap = "non_thinking"

    # Get parsers from model or family (check llm wrapper first)
    tool_parser = get_llm_attr(model_def, "tool_parser")
    if tool_parser is None:
        tool_parser = get_llm_attr(family, "tool_parser")

    reasoning_parser = get_llm_attr(model_def, "reasoning_parser")
    if reasoning_parser is None:
        reasoning_parser = get_llm_attr(family, "reasoning_parser")

    chat_template = get_llm_attr(model_def, "chat_template")
    if chat_template is None:
        chat_template = get_llm_attr(family, "chat_template")

    # For instruct variants, typically no reasoning parser
    if capability == "instruct":
        reasoning_parser = None
    # For base variants without thinking capability, no reasoning parser
    elif capability == "base" and thinking_cap != "hybrid":
        reasoning_parser = None

    return {
        "llm": {
            "thinking_capability": thinking_cap,
            "tool_parser": tool_parser,
            "reasoning_parser": reasoning_parser,
            "chat_template": chat_template,
        }
    }


def generate_model_variants(
    company: str,
    family: dict,
    model_def: dict,
    defaults: dict,
    target_version: tuple[int, ...] | None = None,
    verbose: bool = False,
    inherited_range: dict | None = None,
) -> list[dict]:
    """
    Generate model variants from a base model definition with capabilities/quantizations.

    Args:
        company: HuggingFace organization (e.g., 'deepseek-ai', 'nvidia')
        family: Model family configuration
        model_def: Model definition with base_name, capabilities, quantizations, etc.
        defaults: File-level defaults including hardware and configurations
        target_version: Target version tuple for filtering, or None to include all
        verbose: If True, log filtered items
        inherited_range: Version range inherited from family

    Returns:
        List of expanded model configurations
    """
    base_name = model_def["base_name"]
    family_name = family["name"]

    # Get variant dimensions
    capabilities = model_def.get("capabilities", ["base"])
    quantizations = model_def.get("quantizations", ["bf16", "fp8"])

    # Get custom quant suffixes if defined
    quant_suffixes = model_def.get("quant_suffix", DEFAULT_QUANT_SUFFIXES)

    # Get quantized paths if different paths per quantization
    quantized_paths = model_def.get("quantized_paths", {})

    # Get speculative draft model if present
    speculative_draft_model = model_def.get("speculative_draft_model")

    # Get merged hardware config from family and model levels
    hw_configs, hardware_list = get_merged_hardware_config(
        family, model_def, defaults,
        target_version=target_version, verbose=verbose,
        inherited_range=inherited_range
    )
    default_hw_config = hw_configs.get("default", {})

    models = []

    # Get model name suffixes (for capability variants) from model or family level
    name_suffixes = model_def.get("model_name_suffix", family.get("model_name_suffix", MODEL_NAME_SUFFIXES))

    for capability in capabilities:
        for quant in quantizations:
            # Build model name
            name_suffix = name_suffixes.get(capability, MODEL_NAME_SUFFIXES.get(capability, ""))
            quant_suffix = quant_suffixes.get(quant, DEFAULT_QUANT_SUFFIXES.get(quant, ""))
            # Handle empty base_name (model name matches family name)
            if base_name:
                model_name = f"{family_name}-{base_name}{name_suffix}{quant_suffix}"
            else:
                model_name = f"{family_name}{name_suffix}{quant_suffix}"

            # Determine model path
            if quantized_paths and quant in quantized_paths:
                model_path = quantized_paths[quant]
            else:
                # Default: company/model_name
                model_path = f"{company}/{model_name}"

            # Get quantization-specific overrides (e.g., quant_overrides: { fp8: { ep: 2 } })
            quant_overrides_section = model_def.get("quant_overrides", {})
            quant_overrides = quant_overrides_section.get(quant, {})

            # Build hardware configurations
            hardware = {}
            for hw_name in hardware_list:
                # Start with default config, then merge hardware-specific overrides
                hw_config = {**default_hw_config, **hw_configs.get(hw_name, {})}

                # Check hardware constraints (valid_quants)
                valid_quants = hw_config.get("valid_quants")
                if valid_quants and quant not in valid_quants:
                    continue  # Skip this hardware for this quantization

                hardware[hw_name] = build_hardware_config(
                    hw_name, hw_config, defaults, quant, quant_overrides,
                    speculative_draft_model=speculative_draft_model,
                    target_version=target_version, verbose=verbose,
                    inherited_range=inherited_range
                )

            # Skip if no valid hardware
            if not hardware:
                continue

            result = {
                "name": model_name,
                "model_path": model_path,
                "attributes": build_model_attributes(family, model_def, capability),
                "hardware": hardware,
            }

            # Add optional speculative_draft_model if present
            if speculative_draft_model:
                result["speculative_draft_model"] = speculative_draft_model

            models.append(result)

    return models


def build_explicit_model(
    company: str,
    family: dict,
    model_def: dict | str,
    defaults: dict,
    target_version: tuple[int, ...] | None = None,
    verbose: bool = False,
    inherited_range: dict | None = None,
) -> dict:
    """
    Build a single explicit model (no variant generation).

    Args:
        company: HuggingFace organization (e.g., 'deepseek-ai', 'nvidia')
        family: Model family configuration
        model_def: Model definition dict or string (just the name)
        defaults: File-level defaults including hardware and configurations
        target_version: Target version tuple for filtering, or None to include all
        verbose: If True, log filtered items
        inherited_range: Version range inherited from family

    Returns:
        Full model configuration dict
    """
    # Handle string-only model definition (just the name)
    if isinstance(model_def, str):
        model_def = {"name": model_def}

    model_name = model_def["name"]

    # Derive model_path if not specified
    model_path = model_def.get("model_path", f"{company}/{model_name}")

    # Get merged hardware config from family and model levels
    hw_configs, hardware_list = get_merged_hardware_config(
        family, model_def, defaults,
        target_version=target_version, verbose=verbose,
        inherited_range=inherited_range
    )
    default_hw_config = hw_configs.get("default", {})

    # Default quantization for explicit models
    quant = model_def.get("quantization", "fp8")

    # Get speculative draft model if present
    speculative_draft_model = model_def.get("speculative_draft_model")

    # Get quantization-specific overrides (e.g., quant_overrides: { fp8: { ep: 2 } })
    quant_overrides_section = model_def.get("quant_overrides", {})
    quant_overrides = quant_overrides_section.get(quant, {})

    # Build hardware configurations
    hardware = {}
    for hw_name in hardware_list:
        # Start with default config, then merge hardware-specific overrides
        hw_config = {**default_hw_config, **hw_configs.get(hw_name, {})}
        hardware[hw_name] = build_hardware_config(
            hw_name, hw_config, defaults, quant, quant_overrides,
            speculative_draft_model=speculative_draft_model,
            target_version=target_version, verbose=verbose,
            inherited_range=inherited_range
        )

    result = {
        "name": model_name,
        "model_path": model_path,
        "attributes": build_model_attributes(family, model_def),
        "hardware": hardware,
    }

    # Add optional speculative_draft_model if present
    if speculative_draft_model:
        result["speculative_draft_model"] = speculative_draft_model

    return result


def build_family(
    company: str,
    family: dict,
    defaults: dict,
    target_version: tuple[int, ...] | None = None,
    verbose: bool = False,
    inherited_range: dict | None = None,
) -> dict:
    """
    Build a full family configuration.

    Args:
        company: HuggingFace organization name
        family: Family configuration dict
        defaults: File-level defaults
        target_version: Target version tuple for filtering, or None to include all
        verbose: If True, log filtered items
        inherited_range: Version range inherited from file (family's effective range)
    """
    models = []

    for model_def in family.get("models", []):
        # Skip models outside version range
        # String-only model definitions have no version constraint, but inherit from family
        if isinstance(model_def, dict):
            if not item_in_version_range(model_def, target_version, item_type="model", inherited_range=inherited_range):
                if verbose:
                    model_name = model_def.get("name", model_def.get("base_name", "<unnamed>"))
                    versions = model_def.get("versions") or inherited_range or {}
                    min_ver = versions.get("min", "any")
                    max_ver = versions.get("max", "any")
                    version_str = ".".join(str(v) for v in target_version) if target_version else "none"
                    print(
                        f"  Filtered model '{model_name}': "
                        f"version {version_str} not in range [{min_ver}, {max_ver})",
                        file=sys.stderr,
                    )
                continue

        # Determine if this is variant generation or explicit model
        if isinstance(model_def, dict) and "base_name" in model_def:
            # Variant generation mode
            variants = generate_model_variants(
                company, family, model_def, defaults,
                target_version=target_version, verbose=verbose,
                inherited_range=inherited_range
            )
            models.extend(variants)
        else:
            # Explicit model mode
            models.append(build_explicit_model(
                company, family, model_def, defaults,
                target_version=target_version, verbose=verbose,
                inherited_range=inherited_range
            ))

    return {
        "name": family["name"],
        "description": family.get("description"),
        "models": models,
    }


# =============================================================================
# Main Compilation Functions
# =============================================================================


def compile_config(
    source: dict,
    vendors: dict,
    target_version: tuple[int, ...] | None = None,
    verbose: bool = False,
) -> dict:
    """
    Compile a simplified config into full schema format.

    Args:
        source: Source YAML configuration dict
        vendors: Vendor lookup dict
        target_version: Target version tuple for filtering, or None to include all
        verbose: If True, log filtered items to stderr
    """
    # Support both 'vendor' (new) and 'company' (legacy) keys
    vendor_id = source.get("vendor") or source.get("company")
    if not vendor_id:
        raise ValueError("Model config must specify 'vendor' or 'company'")

    # Look up vendor to get huggingface_org (used for model paths)
    if vendor_id in vendors:
        company = vendors[vendor_id]["huggingface_org"]
    else:
        # Fallback: use vendor_id directly as company (for backwards compatibility)
        print(
            f"  Warning: Vendor '{vendor_id}' not found in vendors.yaml. "
            f"Using vendor ID as literal HuggingFace org. "
            f"If this is unintentional, add '{vendor_id}' to vendors.yaml.",
            file=sys.stderr,
        )
        company = vendor_id

    defaults = source.get("defaults", {})

    # Get file-level version_range - this is inherited by all families/models/configs
    # that don't specify their own 'versions' field
    file_version_range = source.get("version_range")
    if file_version_range is not None:
        if not isinstance(file_version_range, dict):
            raise ValueError(
                f"Invalid 'version_range' at file level: "
                f"expected dict with 'min'/'max' keys, got {type(file_version_range).__name__}"
            )
        # Validate version_range keys and values eagerly
        valid_keys = {"min", "max"}
        unexpected_keys = set(file_version_range.keys()) - valid_keys
        if unexpected_keys:
            raise ValueError(
                f"Unexpected keys in file-level 'version_range': {unexpected_keys}. "
                f"Valid keys are: {valid_keys}"
            )
        # Validate version string formats
        try:
            if file_version_range.get("min") is not None:
                parse_version(file_version_range["min"])
            if file_version_range.get("max") is not None:
                parse_version(file_version_range["max"])
        except ValueError as e:
            raise ValueError(f"Invalid version in file-level 'version_range': {e}") from e

    families = []
    for family in source.get("families", []):
        # Skip families outside version range using unified helper
        # Families inherit file-level version_range if they don't have their own 'versions'
        if not item_in_version_range(family, target_version, item_type="family", inherited_range=file_version_range):
            if verbose:
                family_name = family.get("name", "<unnamed>")
                versions = family.get("versions") or file_version_range or {}
                min_ver = versions.get("min", "any")
                max_ver = versions.get("max", "any")
                version_str = ".".join(str(v) for v in target_version) if target_version else "none"
                print(
                    f"  Filtered family '{family_name}': "
                    f"version {version_str} not in range [{min_ver}, {max_ver})",
                    file=sys.stderr,
                )
            continue

        # Warn if family has redundant versions that match file-level version_range
        family_versions = family.get("versions")
        if family_versions is not None and file_version_range is not None:
            if (family_versions.get("min") == file_version_range.get("min") and
                family_versions.get("max") == file_version_range.get("max")):
                family_name = family.get("name", "<unnamed>")
                print(
                    f"  Warning: Family '{family_name}' has redundant 'versions' "
                    f"that matches file-level 'version_range' - consider removing it",
                    file=sys.stderr,
                )

        # Determine the effective version range for this family (for passing to children)
        family_version_range = family.get("versions") or file_version_range

        built_family = build_family(
            company, family, defaults,
            target_version=target_version, verbose=verbose,
            inherited_range=family_version_range
        )
        # Only include families that have at least one model
        if built_family["models"]:
            families.append(built_family)

    # Warn if all families were filtered out (verbose mode)
    if not families and target_version is not None and source.get("families"):
        if verbose:
            version_str = ".".join(str(v) for v in target_version)
            print(
                f"  Warning: All families filtered out for version {version_str}",
                file=sys.stderr,
            )

    return {
        "vendor": vendor_id,
        "families": families,
    }


def load_yaml(path: Path) -> dict:
    """
    Load a YAML file.

    Args:
        path: Path to the YAML file

    Returns:
        Parsed YAML content as a dict

    Raises:
        ValueError: If file cannot be read, parsed, or is empty/invalid
    """
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        raise ValueError(f"YAML file not found: {path}")
    except PermissionError:
        raise ValueError(f"Permission denied reading YAML file: {path}")
    except yaml.YAMLError as e:
        raise ValueError(f"Invalid YAML syntax in {path}: {e}")
    except UnicodeDecodeError as e:
        raise ValueError(f"File encoding error in {path}: {e}")

    if data is None:
        raise ValueError(f"YAML file is empty or contains only null: {path}")
    if not isinstance(data, dict):
        raise ValueError(
            f"YAML file must contain a mapping/dict at root level, "
            f"got {type(data).__name__}: {path}"
        )
    return data


# Custom string class to force quoted output in YAML
class QuotedString(str):
    """String that will be quoted when serialized to YAML."""
    pass


def _represent_quoted_string(dumper: yaml.Dumper, data: QuotedString) -> yaml.Node:
    """YAML representer for QuotedString - always uses quotes."""
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(data), style='"')


def save_yaml(data: dict, path: Path) -> None:
    """
    Save data to a YAML file with consistent formatting.

    Args:
        path: Output file path

    Raises:
        ValueError: If directory cannot be created or file cannot be written
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except (PermissionError, OSError) as e:
        raise ValueError(f"Cannot create output directory {path.parent}: {e}")

    # Custom representer to handle None values as 'null'
    def represent_none(dumper: yaml.Dumper, _: Any) -> yaml.Node:
        return dumper.represent_scalar("tag:yaml.org,2002:null", "null")

    yaml.add_representer(type(None), represent_none)
    yaml.add_representer(QuotedString, _represent_quoted_string)

    try:
        with open(path, "w") as f:
            yaml.dump(
                data,
                f,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
                width=120,
            )
    except (PermissionError, OSError) as e:
        raise ValueError(f"Cannot write output file {path}: {e}")


def load_vendors(models_dir: Path) -> dict:
    """Load vendors from vendors.yaml file."""
    vendors_path = models_dir / "vendors.yaml"
    if not vendors_path.exists():
        print(
            f"Warning: {vendors_path} not found. "
            f"Vendor resolution will use vendor IDs as literal HuggingFace orgs.",
            file=sys.stderr,
        )
        return {}

    data = load_yaml(vendors_path)
    vendors = data.get("vendors", {})

    # Validate required fields
    for vendor_id, vendor_info in vendors.items():
        if "huggingface_org" not in vendor_info:
            raise ValueError(
                f"Vendor '{vendor_id}' missing required 'huggingface_org' field"
            )

    return vendors


def compile_file(
    input_path: Path,
    output_path: Path,
    vendors: dict,
    check_only: bool = False,
    target_version: tuple[int, ...] | None = None,
    verbose: bool = False,
) -> bool:
    """
    Compile a single file.

    Args:
        input_path: Source YAML file path
        output_path: Destination YAML file path
        vendors: Vendor lookup dict
        check_only: If True, only check if output is up to date
        target_version: Target version tuple for filtering, or None to include all
        verbose: If True, log filtered items

    Returns True if successful (or if check passes), False otherwise.
    """
    print(f"Compiling {input_path.name}...")

    try:
        source = load_yaml(input_path)
        compiled = compile_config(source, vendors, target_version=target_version, verbose=verbose)
    except ValueError as e:
        print(f"  ERROR: {e}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"  ERROR: Unexpected error: {e}", file=sys.stderr)
        return False

    if check_only:
        try:
            if output_path.exists():
                existing = load_yaml(output_path)
                if existing == compiled:
                    print(f"  OK: {output_path.name} is up to date")
                    return True
                else:
                    print(f"  FAIL: {output_path.name} is out of date")
                    return False
            else:
                print(f"  FAIL: {output_path.name} does not exist")
                return False
        except ValueError as e:
            print(f"  ERROR reading existing file: {e}", file=sys.stderr)
            return False

    try:
        save_yaml(compiled, output_path)
    except ValueError as e:
        print(f"  ERROR writing file: {e}", file=sys.stderr)
        return False

    print(f"  Wrote {output_path}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compile simplified model configs to full schema format"
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path(__file__).parent.parent / "models" / "src",
        help="Directory containing simplified YAML files",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).parent.parent / "models" / "generated",
        help="Directory for generated YAML files",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check if generated files are up to date without writing",
    )
    parser.add_argument(
        "--version",
        type=str,
        default=None,
        help="Target version to compile for (e.g., '0.5.8'). "
             "Only configurations within version range will be included. "
             "If not specified, all configurations are included.",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show which configurations/hardware/models are filtered out by version",
    )
    parser.add_argument(
        "files",
        nargs="*",
        help="Specific files to compile (default: all .yaml files in input-dir)",
    )

    args = parser.parse_args()

    # Parse target version for filtering
    target_version = None
    if args.version:
        try:
            target_version = parse_version(args.version)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        print(f"Compiling for version: {args.version}")

    # Load vendors from models directory (parent of input-dir)
    models_dir = args.input_dir.parent
    vendors = load_vendors(models_dir)

    # Find input files
    if args.files:
        input_files = [Path(f) for f in args.files]
    else:
        # Search for YAML files in input-dir and all version subdirectories
        input_files = list(args.input_dir.glob("*.yaml"))
        input_files.extend(args.input_dir.glob("*/*.yaml"))

    if not input_files:
        print(f"No YAML files found in {args.input_dir}")
        return 1

    # Compile each file
    all_ok = True
    for input_path in input_files:
        # Preserve version subdirectory structure in output
        relative_path = input_path.relative_to(args.input_dir)
        output_path = args.output_dir / relative_path
        if not compile_file(
            input_path, output_path, vendors, args.check,
            target_version=target_version, verbose=args.verbose
        ):
            all_ok = False

    if args.check and not all_ok:
        print("\nSome files are out of date. Run without --check to regenerate.")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
