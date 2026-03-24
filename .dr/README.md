# Documentation Robotics - Spec Reference

This directory is managed by the Documentation Robotics CLI.

**Spec Version:** 0.8.3
**Installed:** 2026-03-24T10:31:20.485Z

## Structure

- `spec/`          — Compiled spec files (14 JSON files: manifest, base, one per layer)
- `manifest.json`  — Spec version information
- `changesets/`    — Model changesets (active and saved)

## Spec Files

The `spec/` directory contains the complete compiled specification:

- `manifest.json`  — Layer index with node type and relationship counts
- `base.json`      — Base schemas and predicate definitions
- `{layer}.json`   — One file per layer (12 total), each containing:
  - `layer`               — Layer metadata (id, name, description, node_types)
  - `nodeSchemas`         — JSON Schema definitions for all node types in the layer
  - `relationshipSchemas` — Flat relationship definitions for all relationships in the layer

These files are regenerated on `dr init` and `dr upgrade` to match the
current CLI version. Do not manually edit them.

This directory should be git-ignored.
